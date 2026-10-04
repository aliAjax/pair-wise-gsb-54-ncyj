"""业务用例编排、权限检查与审计。"""
import uuid
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .formation_rules import (
    ADVISORY_ROLES,
    FAULT_EXCLUDED_STATES,
    FORMATION_ACTION_ROLES,
    FORMATION_CREATE_ROLES,
    RESOURCE_ROLES,
    FormationRules,
)
from .repository import Repository
from .rules import ACTION_ROLES, CREATE_ROLES, DomainRules

BACKFILL_BATCH = 200


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.formation_rules = FormationRules()
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role) and actor.role != "dispatcher":
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, allowed: set) -> None:
        if actor.role != "admin" and actor.role not in allowed:
            raise PermissionDenied("角色无权执行该操作")

    # ------------------------------------------------------------ 故障单（既有）
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------ 升级回填
    def backfill_status(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.backfill_counts()

    def run_backfill(self, actor: Actor, batch_size: int = BACKFILL_BATCH) -> Dict[str, int]:
        actor = self._actor(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可执行编队数据回填")
        return self.repository.backfill_step(batch_size)

    def _require_backfill_done(self) -> None:
        status = self.repository.backfill_counts()
        if status["status"] != "done" or status["pending"]:
            raise Conflict("编队数据回填尚未完成，暂不能创建或重算编队（进度%s/%s）" % (status["ready"], status["total"]))

    # ------------------------------------------------------------ 资源台账
    def register_resource(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, RESOURCE_ROLES)
        resource = self.formation_rules.validate_resource(payload or {})
        return self.repository.upsert_resource(resource, actor.user_id)

    def list_resources(self, actor: Actor, resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_resources(resource_type)

    # ------------------------------------------------------------ 建议出海时段
    def publish_advisory(self, actor: Actor, area: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, ADVISORY_ROLES)
        area = text({"area": area}, "area")
        data = dict(payload or {})
        data["area"] = area
        advisory = self.formation_rules.validate_advisory(data)
        existing = self.repository.get_advisory(area)
        bump = existing is not None and (
            existing["window_start"] != advisory["window_start"] or existing["window_end"] != advisory["window_end"]
        )
        return self.repository.upsert_advisory(
            area, advisory["window_start"], advisory["window_end"], actor.user_id, bump
        )

    def list_advisories(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_advisories()

    # ------------------------------------------------------------ 编队
    def _formation_detail(self, formation: Dict[str, Any]) -> Dict[str, Any]:
        formation = dict(formation)
        formation["record_ids"] = self.repository.formation_record_ids(int(formation["id"]))
        return formation

    def _load_eligible_records(self, record_ids: List[int]) -> List[Dict[str, Any]]:
        records = self.repository.get_many(record_ids)
        if len(records) != len(set(record_ids)):
            found = {int(r["id"]) for r in records}
            missing = [rid for rid in record_ids if rid not in found]
            raise ValidationError("故障单不存在：%s" % ",".join(map(str, missing)))
        not_ready = [r["reference"] for r in records if not r.get("formation_ready")]
        if not_ready:
            raise Conflict("以下故障单编队数据回填未完成：%s" % ",".join(not_ready))
        closed = [r["reference"] for r in records if r["state"] in FAULT_EXCLUDED_STATES]
        if closed:
            raise Conflict("以下故障单已结束，不能入编：%s" % ",".join(closed))
        return records

    def _check_resource_codes(self, plan: Dict[str, Any]) -> None:
        wanted = (
            ("vessel", plan.get("vessel_code")),
            ("crew", plan.get("crew_code")),
            ("cable_batch", plan.get("cable_batch_code")),
        )
        unknown = []
        for rtype, code in wanted:
            if code and self.repository.get_resource(rtype, code) is None:
                unknown.append("%s:%s" % (rtype, code))
        if unknown:
            raise ValidationError("资源台账中不存在：%s" % ",".join(unknown))

    def _evaluate(self, plan: Dict[str, Any], window_start: str, window_end: str, demand_km: float, exclude_formation_id: Optional[int] = None):
        assignments = {
            "vessel_code": plan.get("vessel_code"),
            "crew_code": plan.get("crew_code"),
            "cable_batch_code": plan.get("cable_batch_code"),
        }
        occupiers = self.repository.booking_occupiers(assignments, window_start, window_end, exclude_formation_id)
        remaining = self.repository.batch_remaining(plan.get("cable_batch_code"), exclude_formation_id)
        gaps = self.formation_rules.build_gaps(assignments, occupiers, remaining, demand_km)
        return gaps, remaining

    def plan_formation(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, FORMATION_CREATE_ROLES)
        self._require_backfill_done()
        reference = text({"reference": reference}, "reference")
        plan = self.formation_rules.validate_plan(payload or {})
        advisory = self.repository.get_advisory(plan["area"])
        if advisory is None:
            raise ValidationError("海域%s尚未发布建议出海时段" % plan["area"])
        records = self._load_eligible_records(plan["record_ids"])
        self._check_resource_codes(plan)
        busy = self.repository.find_open_formation_for_records(plan["record_ids"])
        if busy:
            raise Conflict("所选故障单已在未结束编队%s中" % busy)
        demand_km = self.formation_rules.demand(records)
        gaps, remaining = self._evaluate(plan, advisory["window_start"], advisory["window_end"], demand_km)
        state = self.formation_rules.decide(gaps)
        formation = {
            "reference": reference,
            "state": state,
            "area": plan["area"],
            "window_start": advisory["window_start"],
            "window_end": advisory["window_end"],
            "advisory_revision": int(advisory["revision"]),
            "vessel_code": plan.get("vessel_code"),
            "crew_code": plan.get("crew_code"),
            "cable_batch_code": plan.get("cable_batch_code"),
            "spare_demand_km": demand_km,
            "gaps": gaps,
        }
        return self._formation_detail(self.repository.insert_formation(formation, plan["record_ids"], actor.user_id))

    def get_formation(self, actor: Actor, formation_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._formation_detail(self.repository.get_formation(formation_id))

    def list_formations(self, actor: Actor, state: Optional[str] = None, area: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.list_formations(state=state, area=area, limit=limit)
        return [self._formation_detail(item) for item in items]

    def formation_timeline(self, actor: Actor, formation_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.formation_events(formation_id)

    def replan_formation(self, actor: Actor, formation_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, FORMATION_ACTION_ROLES)
        self._require_backfill_done()
        formation = self.repository.get_formation(formation_id)
        if formation["state"] not in ("proposed", "standby", "invalidated"):
            raise Conflict("当前编队状态不允许重算：%s" % formation["state"])
        overrides = self.formation_rules.parse_replan(data or {}, formation)
        advisory = self.repository.get_advisory(formation["area"])
        if advisory is None:
            raise ValidationError("海域%s尚未发布建议出海时段" % formation["area"])
        record_ids = self.repository.formation_record_ids(formation_id)
        records = self._load_eligible_records(record_ids)
        plan = {"area": formation["area"], **overrides}
        self._check_resource_codes(plan)
        demand_km = self.formation_rules.demand(records)
        gaps, remaining = self._evaluate(
            plan, advisory["window_start"], advisory["window_end"], demand_km, exclude_formation_id=formation_id
        )
        state = self.formation_rules.decide(gaps)
        fields = {
            "window_start": advisory["window_start"],
            "window_end": advisory["window_end"],
            "advisory_revision": int(advisory["revision"]),
            "vessel_code": overrides.get("vessel_code"),
            "crew_code": overrides.get("crew_code"),
            "cable_batch_code": overrides.get("cable_batch_code"),
            "spare_demand_km": demand_km,
            "gaps": gaps,
        }
        saved = self.repository.save_formation_draft(
            formation_id,
            state,
            fields,
            actor.user_id,
            "replanned",
            {"from_state": formation["state"], "gaps": gaps, "advisory_revision": int(advisory["revision"])},
            expected_version=int(expected_version),
        )
        return self._formation_detail(saved)

    def confirm_formation(self, actor: Actor, formation_id: int, expected_version: int, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, FORMATION_ACTION_ROLES)
        formation = self.repository.get_formation(formation_id)
        key = (idempotency_key or "").strip() or ("confirm-%s-v%s" % (formation["reference"], expected_version))
        # 已确认编队：同一确认（同幂等键）重放直接返回，不重复占用或扣减
        if formation["state"] == "confirmed":
            if formation.get("idempotency_key") == key:
                return self._formation_detail(formation)
            raise Conflict("编队已确认")
        if formation["state"] in ("cancelled", "completed"):
            raise Conflict("编队已结束，不能确认")
        if formation["state"] == "invalidated":
            raise Conflict("建议时段已变更，未确认方案已失效，请先重算")
        advisory = self.repository.get_advisory(formation["area"])
        if advisory is None:
            raise Conflict("建议时段缺失，无法确认")
        if int(advisory["revision"]) != int(formation["advisory_revision"]):
            raise Conflict("建议时段已变更，未确认方案已失效，请先重算")
        record_ids = self.repository.formation_record_ids(formation_id)
        self._load_eligible_records(record_ids)
        assignments = {
            "vessel_code": formation["vessel_code"],
            "crew_code": formation["crew_code"],
            "cable_batch_code": formation["cable_batch_code"],
        }
        demand_km = float(formation["spare_demand_km"])
        occupiers = self.repository.booking_occupiers(
            assignments, formation["window_start"], formation["window_end"], exclude_formation_id=formation_id
        )
        remaining = self.repository.batch_remaining(formation["cable_batch_code"], exclude_formation_id=formation_id)
        gaps = self.formation_rules.build_gaps(assignments, occupiers, remaining, demand_km)
        result = self.repository.confirm_formation(
            formation_id,
            expected_version=int(expected_version),
            idempotency_key=key,
            gaps=gaps,
            actor_id=actor.user_id,
            details={"from": formation["state"], "to": "confirmed" if not gaps else "standby"},
        )
        return self._formation_detail(result)

    def cancel_formation(self, actor: Actor, formation_id: int, reason: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, FORMATION_ACTION_ROLES)
        formation = self.repository.get_formation(formation_id)
        if formation["state"] in ("cancelled", "completed"):
            raise Conflict("编队已结束")
        reason = text({"reason": reason or ""}, "reason")
        saved = self.repository.set_formation_state(
            formation_id, "cancelled", actor.user_id, "cancelled", {"from": formation["state"], "reason": reason}
        )
        return self._formation_detail(saved)

    def complete_formation(self, actor: Actor, formation_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, FORMATION_ACTION_ROLES)
        formation = self.repository.get_formation(formation_id)
        if formation["state"] != "confirmed":
            raise Conflict("仅已确认编队可标记完成")
        saved = self.repository.set_formation_state(
            formation_id, "completed", actor.user_id, "completed", {"from": "confirmed"}
        )
        return self._formation_detail(saved)
