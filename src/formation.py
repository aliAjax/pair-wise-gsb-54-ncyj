"""抢修编队领域规则：出海窗口、资源互斥与待命缺口计算。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError


PROPOSED = "proposed"
STANDBY = "standby"
CONFIRMED = "confirmed"
COMPLETED = "completed"
CANCELLED = "cancelled"
INVALIDATED = "invalidated"

# 仍可被调表作废、尚未锁定资源的方案状态
OPEN_STATES = (PROPOSED, STANDBY)
# 资源占用仍然有效的编队状态
RESERVED_STATES = (CONFIRMED, COMPLETED)
FORMATION_STATES = (PROPOSED, STANDBY, CONFIRMED, COMPLETED, CANCELLED, INVALIDATED)

VESSEL = "vessel"
CREW = "crew"
SPARE_BATCH = "spare_batch"
RESOURCE_TYPES = (VESSEL, CREW)


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO时间" % field)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO时间" % field) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_window(payload: Dict[str, Any]) -> Tuple[str, str]:
    start = parse_timestamp(payload.get("window_start"), "window_start")
    end = parse_timestamp(payload.get("window_end"), "window_end")
    if end <= start:
        raise ValidationError("出海结束时间必须晚于开始时间")
    return start.isoformat(), end.isoformat()


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """半开区间重叠：端点相接不算撞车，也不留空档。"""
    return start_a < end_b and start_b < end_a


def required_spare(faults: List[Dict[str, Any]]) -> float:
    total = 0.0
    for fault in faults:
        payload = fault.get("payload", {})
        total += float(payload.get("required_spare_km", 0.0))
    return round(total, 2)


def _is_free(alloc_type: str, resource_id: int, start: str, end: str, allocations: List[Dict[str, Any]]) -> bool:
    for item in allocations:
        if item["alloc_type"] != alloc_type or int(item["resource_id"]) != int(resource_id):
            continue
        if overlaps(start, end, item["window_start"], item["window_end"]):
            return False
    return True


def _pick_free(resources: List[Dict[str, Any]], alloc_type: str, start: str, end: str, allocations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for resource in resources:
        if _is_free(alloc_type, resource["id"], start, end, allocations):
            return resource
    return None


def plan_formation(
    faults: List[Dict[str, Any]],
    window_start: str,
    window_end: str,
    vessels: List[Dict[str, Any]],
    crews: List[Dict[str, Any]],
    batches: List[Dict[str, Any]],
    allocations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """根据资源占用快照规划编队：三类资源齐备为proposed，否则保留standby并列缺口。"""
    spare_km = required_spare(faults)
    assignments: Dict[str, Any] = {}
    gaps: List[Dict[str, Any]] = []

    vessel = _pick_free(vessels, VESSEL, window_start, window_end, allocations)
    if vessel is None:
        gaps.append({"gap_type": VESSEL, "reason": "出海窗口内没有空闲船机"})
    else:
        assignments.update({"vessel_id": vessel["id"], "vessel_code": vessel["code"], "vessel_name": vessel["name"]})

    crew = _pick_free(crews, CREW, window_start, window_end, allocations)
    if crew is None:
        gaps.append({"gap_type": CREW, "reason": "出海窗口内没有空闲接续班组"})
    else:
        assignments.update({"crew_id": crew["id"], "crew_code": crew["code"], "crew_name": crew["name"]})

    free_batches = [batch for batch in batches if _is_free(SPARE_BATCH, batch["id"], window_start, window_end, allocations)]
    batch = next((item for item in free_batches if float(item["available_km"]) >= spare_km), None)
    if batch is None:
        if not free_batches:
            gaps.append({"gap_type": SPARE_BATCH, "reason": "出海窗口内没有空闲备缆批次"})
        else:
            best = max(float(item["available_km"]) for item in free_batches)
            gaps.append({"gap_type": SPARE_BATCH, "reason": "空闲批次备缆不足，需要%s公里，最多可用%s公里" % (spare_km, round(best, 2))})
    else:
        assignments.update({
            "batch_id": batch["id"],
            "batch_code": batch["code"],
            "batch_name": batch["name"],
            "batch_available_km": float(batch["available_km"]),
        })

    assignments["spare_required_km"] = spare_km
    return {
        "state": PROPOSED if not gaps else STANDBY,
        "spare_required_km": spare_km,
        "assignments": assignments,
        "gaps": gaps,
    }
