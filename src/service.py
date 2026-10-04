"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from . import formation as formation_rules
from .audit import AuditRecorder
from .domain import Actor, Conflict, DuplicateSubmission, NotFound, PermissionDenied, ValidationError, choice, integer, number, text
from .formation import CREW, SPARE_BATCH, VESSEL, parse_window
from .repository import Repository
from .rules import DomainRules


BACKFILL_BATCH_SIZE = 200
CONFIRM_ROLES = {'dispatcher', 'repair_manager'}
SCHEDULE_ROLES = {'repair_manager'}
ADMIN_ROLES = {'admin', 'repair_manager'}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _is(actor: Actor, roles: set) -> bool:
        return actor.role == "admin" or actor.role in roles

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

    # ------------------------------------------------------- 资源与备缆批次
    def register_resource(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, {'repair_manager'}):
            raise PermissionDenied("角色无权登记资源")
        rtype = choice(payload or {}, "rtype", [VESSEL, CREW])
        code = text(payload, "code")
        name = text(payload, "name")
        details = payload.get("details", {})
        if not isinstance(details, dict):
            raise ValidationError("details必须是对象")
        return self.repository.register_resource(rtype, code, name, details, actor.user_id)

    def list_resources(self, actor: Actor, rtype: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if rtype:
            choice({"rtype": rtype}, "rtype", [VESSEL, CREW])
            return self.repository.list_resources(rtype)
        return self.repository.list_resources(VESSEL) + self.repository.list_resources(CREW)

    def register_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, {'repair_manager'}):
            raise PermissionDenied("角色无权登记备缆批次")
        payload = payload or {}
        code = text(payload, "code")
        name = text(payload, "name")
        total_km = number(payload, "total_km", 0.01)
        return self.repository.register_batch(code, name, total_km)

    def list_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches()

    def busy_allocations(self, actor: Actor, alloc_type: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if alloc_type:
            choice({"alloc_type": alloc_type}, "alloc_type", [VESSEL, CREW, SPARE_BATCH])
        return self.repository.busy_allocations(alloc_type)

    # ------------------------------------------------------------- 回填升级
    def migration_status(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.migration_status()

    def run_backfill(self, actor: Actor, batch_size: int = BACKFILL_BATCH_SIZE) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, ADMIN_ROLES):
            raise PermissionDenied("角色无权执行数据回填")
        size = integer({"batch_size": batch_size}, "batch_size", 1, 1000)
        processed = self.repository.backfill_next_batch(actor.user_id, size)
        status = self.repository.migration_status()
        return {"processed": processed, **status}

    def recover(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, ADMIN_ROLES):
            raise PermissionDenied("角色无权执行恢复")
        return self.repository.reconcile()

    # ----------------------------------------------------------------- 编队
    def _build_plan(self, fault_ids: List[int], start: str, end: str) -> Dict[str, Any]:
        faults = self.repository.get_many(fault_ids)
        missing = set(fault_ids) - {int(fault["id"]) for fault in faults}
        if missing:
            raise NotFound("故障单不存在: %s" % ",".join(str(item) for item in sorted(missing)))
        not_ready = [int(fault["id"]) for fault in faults if not int(fault.get("formation_ready", 1))]
        if not_ready:
            raise Conflict("故障单%s尚未完成回填，不能参加新编队" % ",".join(str(item) for item in sorted(not_ready)))
        plan = formation_rules.plan_formation(
            faults=faults,
            window_start=start,
            window_end=end,
            vessels=self.repository.list_resources(VESSEL),
            crews=self.repository.list_resources(CREW),
            batches=self.repository.list_batches(),
            allocations=self.repository.active_allocations(),
        )
        return plan

    def submit_formation(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, CONFIRM_ROLES):
            raise PermissionDenied("角色无权提交编队方案")
        payload = payload or {}
        reference = text(payload, "reference")
        client_key = text(payload, "client_key")
        start, end = parse_window(payload)
        raw_ids = payload.get("fault_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_ids):
            raise ValidationError("fault_ids必须是非空整数列表")
        fault_ids = sorted({int(item) for item in raw_ids})

        existing = self.repository.find_formation_by_client_key(client_key)
        deduplicated = False
        if existing is not None:
            # 两名调度员同时提交同一个编队：client_key去重，只保留先到的一版
            return {"deduplicated": True, "formation": existing}

        plan = self._build_plan(fault_ids, start, end)
        epoch = self.repository.current_epoch()
        formation = {
            "reference": reference,
            "client_key": client_key,
            "state": plan["state"],
            "epoch": epoch,
            "window_start": start,
            "window_end": end,
            "spare_required_km": plan["spare_required_km"],
            "assignments": plan["assignments"],
            "gaps": plan["gaps"],
        }
        saved = None
        try:
            saved = self.repository.submit_formation(formation, fault_ids, actor.user_id)
        except DuplicateSubmission as exc:
            return {"deduplicated": True, "formation": exc.existing}
        return {"deduplicated": False, "formation": saved}

    def list_formations(self, actor: Actor, state: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if state:
            choice({"state": state}, "state", list(formation_rules.FORMATION_STATES))
        return self.repository.list_formations(state=state)

    def get_formation(self, actor: Actor, formation_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        formation = self.repository.get_formation(formation_id)
        formation["faults"] = self.repository.formation_faults(formation_id)
        formation["epoch_current"] = self.repository.current_epoch()
        return formation

    def formation_timeline(self, actor: Actor, formation_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.formation_timeline(formation_id)

    def confirm_formation(self, actor: Actor, formation_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, CONFIRM_ROLES):
            raise PermissionDenied("角色无权确认编队")
        payload = payload or {}
        idempotency_key = text(payload, "idempotency_key")
        existing = self.repository.find_formation_by_idempotency_key(idempotency_key)
        if existing is not None:
            # 崩溃恢复后重放同一确认：直接返回首次结果，不重复占用或扣减
            return {"replayed": True, "formation": existing}
        formation = self.repository.get_formation(formation_id)
        return self.repository.confirm_formation(
            formation_id, idempotency_key, actor.user_id, float(formation["spare_required_km"])
        )

    def replan_formation(self, actor: Actor, formation_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """建议时段变化后，未确认方案失效并按新窗口重算成新编队。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, CONFIRM_ROLES):
            raise PermissionDenied("角色无权重算编队")
        payload = payload or {}
        old = self.repository.get_formation(formation_id)
        if old["state"] not in formation_rules.OPEN_STATES + (formation_rules.INVALIDATED,):
            raise Conflict("只有未确认方案可以重算，当前状态为%s" % old["state"])
        start, end = parse_window(payload)
        reference = text(payload, "reference")
        client_key = text(payload, "client_key")
        if self.repository.find_formation_by_client_key(client_key) is not None:
            raise Conflict("client_key已被使用")
        fault_ids = sorted({int(item["id"]) for item in self.repository.formation_faults(formation_id, active_only=False)})
        if not fault_ids:
            raise Conflict("原编队没有可重算的故障单")
        # 先释放旧方案对故障单的active绑定，再按新窗口重算提交
        self.repository.invalidate_for_replan(formation_id, actor.user_id)
        try:
            plan = self._build_plan(fault_ids, start, end)
            epoch = self.repository.current_epoch()
            successor = {
                "reference": reference,
                "client_key": client_key,
                "state": plan["state"],
                "epoch": epoch,
                "window_start": start,
                "window_end": end,
                "spare_required_km": plan["spare_required_km"],
                "assignments": plan["assignments"],
                "gaps": plan["gaps"],
            }
            saved = self.repository.submit_formation(successor, fault_ids, actor.user_id)
        except Exception:
            # 新方案提交失败时旧方案已失效，但故障单已释放，可再次replan
            raise
        self.repository.link_successor(formation_id, int(saved["id"]))
        return {"deduplicated": False, "formation": saved, "superseded_id": formation_id}

    def cancel_formation(self, actor: Actor, formation_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, CONFIRM_ROLES):
            raise PermissionDenied("角色无权取消编队")
        reason = text(payload or {}, "reason")
        return self.repository.cancel_formation(formation_id, reason, actor.user_id)

    def complete_formation(self, actor: Actor, formation_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, {'repair_manager'}):
            raise PermissionDenied("角色无权完工编队")
        return self.repository.complete_formation(formation_id, actor.user_id)

    def schedule(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"epoch": self.repository.current_epoch()}

    def bump_schedule(self, actor: Actor) -> Dict[str, Any]:
        """建议出海时段整体调整：epoch递增，未确认方案全部失效待重算。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is(actor, SCHEDULE_ROLES):
            raise PermissionDenied("角色无权调整建议出海时段")
        epoch = self.repository.invalidate_open_formations(actor.user_id)
        return {"epoch": epoch, "invalidated": True}
