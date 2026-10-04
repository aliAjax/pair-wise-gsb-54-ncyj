import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service
from src.audit import AuditRecorder
from src.formation_rules import FormationRules


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
DISPATCHER = Actor("disp1", "dispatcher")
MANAGER = Actor("rm1", "repair_manager")
ADMIN = Actor("admin1", "admin")
W1 = {"window_start": "2026-10-05T06:00:00+08:00", "window_end": "2026-10-07T18:00:00+08:00"}


def make_fault(service, ref, cable="SEA-1", segment="S3", start=120.0, end=135.0):
    data = dict(CREATE_DATA)
    data.update(cable=cable, segment=segment, start_km=start, end_km=end)
    return service.create(Actor("creator", "noc_operator"), ref, data)


class FormationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self._seed()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        self.r1 = make_fault(self.service, "FAULT-1")
        self.r2 = make_fault(self.service, "FAULT-2", segment="S4", start=140.0, end=150.0)
        self.service.publish_advisory(DISPATCHER, "东海", W1)
        self.service.register_resource(MANAGER, {"type": "vessel", "code": "CS-1", "name": "海缆船一号"})
        self.service.register_resource(MANAGER, {"type": "vessel", "code": "CS-2", "name": "海缆船二号"})
        self.service.register_resource(MANAGER, {"type": "crew", "code": "TEAM-A", "name": "接续班组甲"})
        self.service.register_resource(MANAGER, {"type": "crew", "code": "TEAM-B", "name": "接续班组乙"})
        # r1 需求 15.75km, r2 需求 10.5km
        self.service.register_resource(MANAGER, {"type": "cable_batch", "code": "BATCH-X", "name": "备缆X批", "length_km": 30.0})
        self.service.register_resource(MANAGER, {"type": "cable_batch", "code": "BATCH-Y", "name": "备缆Y批", "length_km": 30.0})

    def _plan(self, ref="F-1", records=None, vessel="CS-1", crew="TEAM-A", batch="BATCH-X", area="东海"):
        return self.service.plan_formation(
            DISPATCHER,
            ref,
            {
                "area": area,
                "record_ids": records if records is not None else [self.r1["id"], self.r2["id"]],
                "vessel_code": vessel,
                "crew_code": crew,
                "cable_batch_code": batch,
            },
        )

    def test_plan_and_confirm_formation_consumes_resources_once(self):
        formation = self._plan()
        self.assertEqual(formation["state"], "proposed")
        self.assertEqual(formation["spare_demand_km"], 26.25)
        self.assertEqual(formation["advisory_revision"], 1)
        confirmed = self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "key-1")
        self.assertEqual(confirmed["state"], "confirmed")

        resources = {r["code"]: r for r in self.service.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-X"]["allocated_km"], 26.25)
        self.assertEqual(resources["BATCH-X"]["remaining_km"], 3.75)

        # 同一确认重放：不重复占用、不重复扣减
        replay = self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "key-1")
        self.assertEqual(replay["state"], "confirmed")
        resources = {r["code"]: r for r in self.service.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-X"]["allocated_km"], 26.25)

    def test_replay_after_restart_is_idempotent(self):
        formation = self._plan()
        self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "crash-key")
        # 模拟服务崩溃后重建（同一完整编队持久化）
        rebuilt = build_service(self.db)
        replay = rebuilt.confirm_formation(DISPATCHER, formation["id"], formation["version"], "crash-key")
        self.assertEqual(replay["state"], "confirmed")
        resources = {r["code"]: r for r in rebuilt.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-X"]["allocated_km"], 26.25)
        events = [e["action"] for e in rebuilt.formation_timeline(DISPATCHER, formation["id"])]
        self.assertEqual(events.count("confirmed"), 1)

    def test_time_window_exclusive_vessel_and_crew(self):
        first = self._plan("F-1", records=[self.r1["id"]])
        self.service.confirm_formation(DISPATCHER, first["id"], first["version"], "k1")
        # 同一时间窗，第二编队换备缆但沿用同一艘船/班组 -> 待命并列缺口
        second = self.service.plan_formation(
            DISPATCHER, "F-2", {"area": "东海", "record_ids": [self.r2["id"]], "vessel_code": "CS-1", "crew_code": "TEAM-A"}
        )
        self.assertEqual(second["state"], "standby")
        reasons = " ".join(g["reason"] for g in second["gaps"])
        self.assertIn("CS-1", reasons)
        self.assertIn("TEAM-A", reasons)
        self.assertIn("未指派备缆批次", reasons)

        # 换上另一艘船、另一班组和另一批备缆 -> 可行
        fixed = self.service.replan_formation(
            DISPATCHER, second["id"], second["version"], {"vessel_code": "CS-2", "crew_code": "TEAM-B", "cable_batch_code": "BATCH-Y"}
        )
        self.assertEqual(fixed["state"], "proposed")
        self.assertEqual(fixed["gaps"], [])

    def test_adjacent_windows_do_not_conflict(self):
        first = self._plan("F-1", records=[self.r1["id"]])
        self.service.confirm_formation(DISPATCHER, first["id"], first["version"], "k1")
        self.service.publish_advisory(
            DISPATCHER, "南海",
            {"window_start": W1["window_end"], "window_end": "2026-10-09T18:00:00+08:00"},
        )
        second = self._plan("F-2", records=[self.r2["id"]], area="南海")
        self.assertEqual(second["state"], "proposed")
        confirmed = self.service.confirm_formation(DISPATCHER, second["id"], second["version"], "k2")
        self.assertEqual(confirmed["state"], "confirmed")

    def test_insufficient_cable_keeps_standby_with_gap(self):
        self.service.register_resource(MANAGER, {"type": "cable_batch", "code": "BATCH-S", "name": "小批量", "length_km": 20.0})
        formation = self._plan("F-1", batch="BATCH-S")
        self.assertEqual(formation["state"], "standby")
        gap_reasons = [g["reason"] for g in formation["gaps"]]
        self.assertTrue(any("备缆余量不足" in r and "缺口6.25km" in r for r in gap_reasons))
        # 待命方案确认时仍不占资源，继续待命
        blocked = self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "kb")
        self.assertEqual(blocked["state"], "standby")
        resources = {r["code"]: r for r in self.service.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-S"]["allocated_km"], 0.0)

    def test_advisory_change_invalidates_unconfirmed_and_replan(self):
        formation = self._plan()
        self.service.publish_advisory(
            DISPATCHER, "东海",
            {"window_start": "2026-10-06T06:00:00+08:00", "window_end": "2026-10-08T18:00:00+08:00"},
        )
        stale = self.service.get_formation(DISPATCHER, formation["id"])
        self.assertEqual(stale["state"], "invalidated")
        self.assertEqual(stale["advisory_revision"], 1)
        with self.assertRaises(Conflict):
            self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "kx")
        replanned = self.service.replan_formation(DISPATCHER, formation["id"], formation["version"] + 1, {})
        self.assertEqual(replanned["state"], "proposed")
        self.assertEqual(replanned["advisory_revision"], 2)
        confirmed = self.service.confirm_formation(DISPATCHER, formation["id"], replanned["version"], "kx")
        self.assertEqual(confirmed["state"], "confirmed")

    def test_confirmed_formation_survives_advisory_change(self):
        formation = self._plan()
        self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "keep")
        self.service.publish_advisory(
            DISPATCHER, "东海",
            {"window_start": "2026-10-09T06:00:00+08:00", "window_end": "2026-10-10T18:00:00+08:00"},
        )
        kept = self.service.get_formation(DISPATCHER, formation["id"])
        self.assertEqual(kept["state"], "confirmed")

    def test_concurrent_confirm_same_version_only_one_wins(self):
        formation = self._plan()
        errors = []

        def confirm(key):
            try:
                self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], key)
            except Exception as exc:  # noqa: BLE001 - 断言在主线程汇总
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=("cc-1",)), threading.Thread(target=confirm, args=("cc-2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        resources = {r["code"]: r for r in self.service.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-X"]["allocated_km"], 26.25)

    def test_concurrent_plan_same_reference_single_winner(self):
        results = []

        def plan():
            try:
                results.append(self._plan("DUP-1", records=[self.r1["id"]]))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=plan), threading.Thread(target=plan)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(wins), 1)

    def test_duplicate_formation_submission_reference_unique(self):
        self._plan("F-1")
        with self.assertRaises(Conflict):
            self._plan("F-1")

    def test_unknown_resource_and_closed_fault_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.plan_formation(
                DISPATCHER, "F-X",
                {"area": "东海", "record_ids": [self.r1["id"]], "vessel_code": "NO-SHIP"},
            )
        closed = make_fault(self.service, "FAULT-DONE", segment="S9", start=200.0, end=210.0)
        self.service.repository.mutate(closed["id"], closed["version"], "cancelled", closed["payload"], "rm1", "cancel", {"summary": "x"})
        with self.assertRaises(Conflict):
            self._plan("F-X2", records=[closed["id"]])

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.plan_formation(Actor("u", "noc_operator"), "F-P", {"area": "东海", "record_ids": [self.r1["id"]]})
        with self.assertRaises(PermissionDenied):
            self.service.register_resource(DISPATCHER, {"type": "vessel", "code": "X", "name": "x"})
        with self.assertRaises(PermissionDenied):
            self.service.run_backfill(DISPATCHER)

    def test_cancel_releases_booking_then_complete_flow(self):
        formation = self._plan()
        self.service.confirm_formation(DISPATCHER, formation["id"], formation["version"], "kc")
        cancelled = self.service.cancel_formation(DISPATCHER, formation["id"], "台风转向")
        self.assertEqual(cancelled["state"], "cancelled")
        resources = {r["code"]: r for r in self.service.list_resources(ADMIN, "cable_batch")}
        self.assertEqual(resources["BATCH-X"]["allocated_km"], 0.0)
        # 释放后同船同窗口新编队可确认
        second = self._plan("F-2", records=[self.r1["id"]])
        self.assertEqual(second["state"], "proposed")
        self.service.confirm_formation(DISPATCHER, second["id"], second["version"], "kc2")
        done = self.service.complete_formation(DISPATCHER, second["id"])
        self.assertEqual(done["state"], "completed")


class BackfillTest(unittest.TestCase):
    def _legacy_db(self, path):
        """构造只有旧表结构、含历史故障单的库，模拟升级前数据。"""
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL,
                created_by TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        rules = DomainRules()
        payload = rules.prepare_create(CREATE_DATA)
        for i in range(3):
            connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?)",
                ("OLD-%s" % i, "detected", json.dumps(payload), "c", "c", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
            )
        connection.commit()
        connection.close()

    def test_legacy_records_need_backfill_before_formation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = str(Path(temp.name) / "legacy.db")
        self._legacy_db(db)

        service = build_service(db)
        status = service.backfill_status(ADMIN)
        self.assertEqual(status["total"], 3)
        self.assertEqual(status["ready"], 0)
        self.assertEqual(status["status"], "pending")

        # 回填前故障详情继续可查
        detail = service.list_records(ADMIN)[0]
        self.assertFalse(detail["formation_ready"])
        self.assertIn("required_spare_km", detail["payload"])

        service.publish_advisory(DISPATCHER, "东海", W1)
        service.register_resource(MANAGER, {"type": "vessel", "code": "CS-1", "name": "船"})
        service.register_resource(MANAGER, {"type": "crew", "code": "TEAM-A", "name": "班"})
        service.register_resource(MANAGER, {"type": "cable_batch", "code": "BX", "name": "缆", "length_km": 99.0})
        with self.assertRaises(Conflict):
            service.plan_formation(DISPATCHER, "F-OLD", {"area": "东海", "record_ids": [1], "vessel_code": "CS-1", "crew_code": "TEAM-A", "cable_batch_code": "BX"})

        # 分批回填完成后可入编；非管理员不能回填
        with self.assertRaises(PermissionDenied):
            service.run_backfill(DISPATCHER, 2)
        progress = service.run_backfill(ADMIN, 2)
        self.assertEqual(progress["ready"], 2)
        self.assertEqual(progress["status"], "pending")
        progress = service.run_backfill(ADMIN, 2)
        self.assertEqual(progress["ready"], 3)
        self.assertEqual(progress["status"], "done")

        formation = service.plan_formation(DISPATCHER, "F-OLD", {"area": "东海", "record_ids": [1], "vessel_code": "CS-1", "crew_code": "TEAM-A", "cable_batch_code": "BX"})
        self.assertEqual(formation["state"], "proposed")

    def test_fresh_database_backfill_already_done(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = Repository(str(Path(temp.name) / "fresh.db"))
        service = Service(repo, DomainRules(), AuditRecorder(repo))
        self.assertEqual(service.backfill_status(ADMIN)["status"], "done")


if __name__ == "__main__":
    unittest.main()
