"""抢修编队规则：资源校验、用缆量汇总、缺口评估与状态决策。

编队把故障单、出海时段和用缆量收进同一聚合；一艘船、一个接续班组、
一批备缆在同一时间窗内只能服务一个已确认编队。资源不足时编队保留为
待命方案并列出缺口。
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, number, optional_text, text


RESOURCE_TYPES = ["vessel", "crew", "cable_batch"]
TYPE_LABELS = {"vessel": "抢修船", "crew": "接续班组", "cable_batch": "备缆批次"}

# 编队状态：proposed 待确认方案 / standby 待命（有缺口）/ invalidated 时段变更失效
# confirmed 已确认并占用资源 / completed 作业完成 / cancelled 取消
OPEN_STATES = ("proposed", "standby", "invalidated")
DEAD_STATES = ("completed", "cancelled")
# 只有已确认且未结束的编队占用资源
BOOKING_ACTIVE_STATES = ("confirmed",)

FORMATION_CREATE_ROLES = {"dispatcher"}
FORMATION_ACTION_ROLES = {"dispatcher"}
RESOURCE_ROLES = {"repair_manager"}
ADVISORY_ROLES = {"dispatcher"}
BACKFILL_ROLES = {"admin"}

FAULT_EXCLUDED_STATES = {"restored", "cancelled"}


def parse_dt(value: Any, field: str) -> str:
    """解析ISO8601时间并归一化为UTC，时间窗比较依赖归一化后的字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO8601时间" % field)
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO8601时间" % field) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class FormationRules:
    def validate_resource(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        rtype = text(p, "type")
        if rtype not in RESOURCE_TYPES:
            raise ValidationError("type只能是%s" % "/".join(RESOURCE_TYPES))
        result = {
            "resource_type": rtype,
            "code": text(p, "code"),
            "name": text(p, "name"),
            "length_km": None,
        }
        if rtype == "cable_batch":
            result["length_km"] = round(number(p, "length_km", 0.01), 2)
        return result

    def validate_advisory(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        area = text(p, "area")
        start = parse_dt(p.get("window_start"), "window_start")
        end = parse_dt(p.get("window_end"), "window_end")
        if not end > start:
            raise ValidationError("window_end必须晚于window_start")
        return {"area": area, "window_start": start, "window_end": end}

    def validate_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        area = text(p, "area")
        raw_ids = p.get("record_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValidationError("record_ids必须是非空整数列表")
        record_ids: List[int] = []
        for item in raw_ids:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValidationError("record_ids必须是正整数列表")
            if item not in record_ids:
                record_ids.append(item)

        def code(key: str) -> Optional[str]:
            value = optional_text(p, key)
            return value or None

        return {
            "area": area,
            "record_ids": record_ids,
            "vessel_code": code("vessel_code"),
            "crew_code": code("crew_code"),
            "cable_batch_code": code("cable_batch_code"),
        }

    def parse_replan(self, payload: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})

        def code(key: str, current_value: Optional[str]) -> Optional[str]:
            if key not in p:
                return current_value
            value = p.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValidationError("%s必须是文本" % key)
            value = value.strip()
            return value or None

        return {
            "vessel_code": code("vessel_code", current.get("vessel_code")),
            "crew_code": code("crew_code", current.get("crew_code")),
            "cable_batch_code": code("cable_batch_code", current.get("cable_batch_code")),
        }

    def demand(self, records: List[Dict[str, Any]]) -> float:
        total = 0.0
        for record in records:
            total += float(record["payload"].get("required_spare_km", 0.0))
        return round(total, 2)

    def build_gaps(
        self,
        assignments: Dict[str, Optional[str]],
        occupiers: Dict[str, Optional[str]],
        batch_remaining: Optional[float],
        demand_km: float,
    ) -> List[Dict[str, str]]:
        """汇总缺口：未指派、时间窗被占、备缆库存不足。occupiers为占用方编队reference。"""
        gaps: List[Dict[str, str]] = []
        for rtype, code in (
            ("vessel", assignments.get("vessel_code")),
            ("crew", assignments.get("crew_code")),
            ("cable_batch", assignments.get("cable_batch_code")),
        ):
            label = TYPE_LABELS[rtype]
            if not code:
                gaps.append({"resource_type": rtype, "resource_code": "", "reason": "未指派%s" % label})
                continue
            occupier = occupiers.get(rtype)
            if occupier:
                gaps.append(
                    {
                        "resource_type": rtype,
                        "resource_code": code,
                        "reason": "%s(%s)在该时段已服务编队%s" % (label, code, occupier),
                    }
                )
            if rtype == "cable_batch" and batch_remaining is not None and batch_remaining < demand_km:
                shortfall = round(demand_km - batch_remaining, 2)
                gaps.append(
                    {
                        "resource_type": "cable_batch",
                        "resource_code": code,
                        "reason": "备缆余量不足：需要%.2fkm，剩余%.2fkm，缺口%.2fkm"
                        % (demand_km, batch_remaining, shortfall),
                    }
                )
        return gaps

    def decide(self, gaps: List[Dict[str, str]]) -> str:
        return "standby" if gaps else "proposed"
