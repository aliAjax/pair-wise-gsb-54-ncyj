import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


DISPATCHER = Actor("disp-1", "dispatcher")
MANAGER = Actor("rm-1", "repair_manager")
ADMIN = Actor("adm-1", "admin")
NOC = Actor("noc-1", "noc_operator")

FAULT = {"cable": "SEA-1", "segment": "S3", "start_km": 120.0, "end_km": 135.0, "depth_m": 1800.0,
         "sea_state": 3, "vessel_available": True, "spare_length_km": 20.0,
         "permit_valid": True, "capacity_gbps": 400}

W_START = "2026-10-05T02:00:00Z"
W_END = "2026-10-06T10:00:00Z"


class FormationBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_fault(self, reference, **overrides):
        data = dict(FAULT)
        data.update(overrides)
        return self.service.create(NOC, reference, data)

    def seed_resources(self, vessels=("CS-1",), crews=("SP-1",), batches=(("B-1", 100.0),)):
        for code in vessels:
            self.service.register_resource(MANAGER, {"rtype": "vessel", "code": code, "name": "船%s" % code})
        for code in crews:
            self.service.register_resource(MANAGER, {"rtype": "crew", "code": code, "name": "班组%s" % code})
        for code, km in batches:
            self.service.register_batch(MANAGER, {"code": code, "name": "批%s" % code, "total_km": km})

    def submit(self, client_key, fault_ids, start=W_START, end=W_END, reference=None):
        return self.service.submit_formation(
            DISPATCHER,
            {"reference": reference or "FM-" + client_key, "client_key": client_key,
             "fault_ids": fault_ids, "window_start": start, "window_end": end},
        )

    def detail(self, formation_id):
        return self.service.get_formation(DISPATCHER, formation_id)


class FormationWorkflowTest(FormationBase):
    def test_submit_groups_faults_window_and_spare(self):
        f1 = self.create_fault("F-1")
        f2 = self.create_fault("F-2", segment="S4", cable="SEA-2", start_km=0.0, end_km=10.0)
        self.seed_resources()
        result = self.submit("K-1", [f1["id"], f2["id"]])
        formation = result["formation"]
        self.assertFalse(result["deduplicated"])
        self.assertEqual(formation["state"], "proposed")
        # 15.75 + 10.5
        self.assertEqual(formation["spare_required_km"], 26.25)
        self.assertEqual([item["id"] for item in self.detail(formation["id"])["faults"]], [f1["id"], f2["id"]])

        confirmed = self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})
        self.assertFalse(confirmed["replayed"])
        self.assertEqual(confirmed["formation"]["state"], "confirmed")
        batch = self.service.list_batches(DISPATCHER)[0]
        self.assertEqual(batch["available_km"], round(100.0 - 26.25, 6))

        busy = self.service.busy_allocations(DISPATCHER)
        self.assertEqual({item["alloc_type"] for item in busy}, {"vessel", "crew", "spare_batch"})

        done = self.service.complete_formation(MANAGER, formation["id"])
        self.assertEqual(done["state"], "completed")

    def test_standby_plan_kept_with_gap_list_when_resources_short(self):
        f1 = self.create_fault("F-1")
        # 只登记船，缺班组和备缆
        self.seed_resources(vessels=("CS-1",), crews=(), batches=())
        result = self.submit("K-1", [f1["id"]])
        formation = result["formation"]
        self.assertEqual(formation["state"], "standby")
        gap_types = {item["gap_type"] for item in formation["gaps"]}
        self.assertEqual(gap_types, {"crew", "spare_batch"})
        with self.assertRaises(Conflict):
            self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-x"})

    def test_same_window_one_ship_one_crew_one_batch(self):
        f1 = self.create_fault("F-1")
        f2 = self.create_fault("F-2", segment="S4", cable="SEA-2")
        self.seed_resources()
        first = self.submit("K-1", [f1["id"]])["formation"]
        self.service.confirm_formation(DISPATCHER, first["id"], {"idempotency_key": "CK-1"})
        # 重叠窗口：船/班组/批次均被占，只能待命并列缺口
        second = self.submit("K-2", [f2["id"]])["formation"]
        self.assertEqual(second["state"], "standby")
        self.assertEqual({item["gap_type"] for item in second["gaps"]}, {"vessel", "crew", "spare_batch"})
        # 相接窗口（不算重叠）可以排进同一套资源
        f3 = self.create_fault("F-3", cable="SEA-3")
        touch = self.submit("K-3", [f3["id"]], start=W_END, end="2026-10-07T00:00:00Z")["formation"]
        self.assertEqual(touch["state"], "proposed")

    def test_concurrent_same_client_key_only_one_version(self):
        f1 = self.create_fault("F-1")
        self.seed_resources()
        results = {}
        total = 8
        barrier = threading.Barrier(total)

        def run(i):
            barrier.wait()
            try:
                results[i] = self.service.submit_formation(
                    Actor("disp-%d" % i, "dispatcher"),
                    {"reference": "FM-SAME", "client_key": "SAME-KEY", "fault_ids": [f1["id"]],
                     "window_start": W_START, "window_end": W_END},
                )
            except Exception as exc:
                results[i] = exc

        threads = [threading.Thread(target=run, args=(i,)) for i in range(total)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        ids = set()
        for value in results.values():
            self.assertNotIsInstance(value, Exception, "并发提交不应报错，应返回已生效版本")
            ids.add(value["formation"]["id"])
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.service.list_formations(DISPATCHER)), 1)

    def test_concurrent_confirm_same_window_only_one_wins(self):
        faults = [self.create_fault("F-%d" % i, segment="S%d" % i, cable="C-%d" % i) for i in range(6)]
        self.seed_resources()
        formations = [self.submit("K-%d" % i, [faults[i]["id"]])["formation"] for i in range(len(faults))]
        results = {}
        barrier = threading.Barrier(len(formations))

        def run(i):
            barrier.wait()
            try:
                results[i] = self.service.confirm_formation(
                    Actor("disp-%d" % i, "dispatcher"), formations[i]["id"],
                    {"idempotency_key": "CK-%d" % i})
            except Exception as exc:
                results[i] = exc

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(formations))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        won = [value for value in results.values()
               if not isinstance(value, Exception) and not value.get("replayed")
               and value["formation"]["state"] == "confirmed"]
        lost = [value for value in results.values() if isinstance(value, Conflict)]
        self.assertEqual(len(won), 1)
        self.assertEqual(len(lost), len(formations) - 1)
        # 只有赢的那个扣了一次备缆 15.75
        self.assertEqual(self.service.list_batches(DISPATCHER)[0]["available_km"], round(100.0 - 15.75, 6))

    def test_confirm_replay_is_idempotent_after_restart(self):
        f1 = self.create_fault("F-1")
        self.seed_resources()
        formation = self.submit("K-1", [f1["id"]])["formation"]
        first = self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})
        self.assertFalse(first["replayed"])
        # 模拟服务重启：重新构建Service实例（同一数据库文件），重放同一确认
        restarted = build_service(self.service.repository.db_path)
        replay = restarted.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["formation"]["state"], "confirmed")
        allocations = restarted.busy_allocations(DISPATCHER)
        self.assertEqual(len(allocations), 3)
        self.assertEqual(restarted.list_batches(DISPATCHER)[0]["available_km"], round(100.0 - 15.75, 6))

    def test_schedule_change_invalidates_open_plans_and_replan_rebuilds(self):
        f1 = self.create_fault("F-1")
        self.seed_resources()
        first = self.submit("K-0", [f1["id"]],
                            start="2026-10-01T00:00:00Z", end="2026-10-02T00:00:00Z")["formation"]
        self.service.confirm_formation(DISPATCHER, first["id"], {"idempotency_key": "CK-0"})
        # 与已确认编队同窗口重叠：只能待命
        f2 = self.create_fault("F-2", cable="SEA-2")
        standby = self.service.submit_formation(
            DISPATCHER, {"reference": "FM-K-2", "client_key": "K-2", "fault_ids": [f2["id"]],
                         "window_start": "2026-10-01T06:00:00Z", "window_end": "2026-10-01T12:00:00Z"})
        self.assertEqual(standby["formation"]["state"], "standby")

        # 另一个未确认的齐套方案
        f4 = self.create_fault("F-4", cable="SEA-4")
        proposed = self.submit("K-1", [f4["id"]],
                               start="2026-10-20T00:00:00Z", end="2026-10-21T00:00:00Z")["formation"]
        self.assertEqual(proposed["state"], "proposed")

        bumped = self.service.bump_schedule(MANAGER)
        self.assertEqual(bumped["epoch"], 2)
        self.assertEqual(self.detail(proposed["id"])["state"], "invalidated")
        self.assertEqual(self.detail(standby["formation"]["id"])["state"], "invalidated")
        # 已确认方案不受调表影响
        self.assertEqual(self.detail(first["id"])["state"], "confirmed")

        # 失效方案按新窗口重算，旧方案记录后继，故障单转移到新方案
        replan = self.service.replan_formation(
            DISPATCHER, proposed["id"],
            {"reference": "FM-R1", "client_key": "K-1R",
             "window_start": "2026-11-10T00:00:00Z", "window_end": "2026-11-11T00:00:00Z"})
        successor = replan["formation"]
        self.assertEqual(successor["state"], "proposed")
        self.assertEqual(successor["epoch"], 2)
        old = self.detail(proposed["id"])
        self.assertEqual(old["state"], "invalidated")
        self.assertEqual(old["superseded_by"], successor["id"])
        self.assertEqual([item["id"] for item in self.detail(successor["id"])["faults"]], [f4["id"]])

    def test_standby_can_be_fulfilled_after_resources_freed(self):
        f1 = self.create_fault("F-1")
        f2 = self.create_fault("F-2", cable="SEA-2")
        self.seed_resources()
        first = self.submit("K-1", [f1["id"]])["formation"]
        self.service.confirm_formation(DISPATCHER, first["id"], {"idempotency_key": "CK-1"})
        standby = self.submit("K-2", [f2["id"]])["formation"]
        self.assertEqual(standby["state"], "standby")
        # 新增一套资源后，待命方案重算即可齐套
        self.service.register_resource(MANAGER, {"rtype": "vessel", "code": "CS-2", "name": "船2"})
        self.service.register_resource(MANAGER, {"rtype": "crew", "code": "SP-2", "name": "班组2"})
        self.service.register_batch(MANAGER, {"code": "B-2", "name": "批2", "total_km": 80.0})
        replan = self.service.replan_formation(
            DISPATCHER, standby["id"],
            {"reference": "FM-R2", "client_key": "K-2R",
             "window_start": W_START, "window_end": W_END})
        self.assertEqual(replan["formation"]["state"], "proposed")
        self.assertEqual(replan["formation"]["assignments"]["vessel_code"], "CS-2")

    def test_cancel_confirmed_releases_resources_and_restores_batch(self):
        f1 = self.create_fault("F-1")
        f2 = self.create_fault("F-2", cable="SEA-2")
        self.seed_resources()
        first = self.submit("K-1", [f1["id"]])["formation"]
        self.service.confirm_formation(DISPATCHER, first["id"], {"idempotency_key": "CK-1"})
        self.assertEqual(self.service.list_batches(DISPATCHER)[0]["available_km"], round(100.0 - 15.75, 6))
        # 同窗口的第二个编队只能待命
        standby = self.submit("K-2", [f2["id"]])["formation"]
        self.assertEqual(standby["state"], "standby")
        # 返航退编：释放资源、回补备缆，幂等键重放不再变成已确认
        self.service.cancel_formation(DISPATCHER, first["id"], {"reason": "台风二次转向"})
        self.assertEqual(self.service.list_batches(DISPATCHER)[0]["available_km"], 100.0)
        self.assertEqual(self.service.busy_allocations(DISPATCHER), [])
        replay = self.service.confirm_formation(DISPATCHER, first["id"], {"idempotency_key": "CK-1"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["formation"]["state"], "cancelled")
        # 待命方案重算后可在原窗口落地
        replan = self.service.replan_formation(
            DISPATCHER, standby["id"],
            {"reference": "FM-R", "client_key": "K-2R", "window_start": W_START, "window_end": W_END})
        self.assertEqual(replan["formation"]["state"], "proposed")

    def test_roles_are_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_resource(NOC, {"rtype": "vessel", "code": "X", "name": "x"})
        with self.assertRaises(PermissionDenied):
            self.service.bump_schedule(DISPATCHER)
        with self.assertRaises(PermissionDenied):
            self.service.run_backfill(DISPATCHER)
        with self.assertRaises(PermissionDenied):
            self.service.complete_formation(DISPATCHER, 1)

    def test_timeline_records_lifecycle(self):
        f1 = self.create_fault("F-1")
        self.seed_resources()
        formation = self.submit("K-1", [f1["id"]])["formation"]
        self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})
        actions = [event["action"] for event in self.service.formation_timeline(DISPATCHER, formation["id"])]
        self.assertEqual(actions, ["submitted", "confirmed"])


class BackfillTest(FormationBase):
    def _legacy_db(self):
        import json
        import sqlite3
        connection = sqlite3.connect(self.service.repository.db_path)
        # 全新库直接带有新表，模拟存量只需把记录重置为未回填
        payload = {"cable": "SEA-9", "segment": "S1", "start_km": 0.0, "end_km": 5.0,
                   "required_spare_km": 5.25}
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,formation_ready)"
            " VALUES('OLD-1','detected',1,?,'old','old','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00',0)",
            (json.dumps(payload),),
        )
        connection.commit()
        connection.close()

    def test_pending_record_queryable_but_excluded_from_new_formation(self):
        self._legacy_db()
        status = self.service.migration_status(ADMIN)
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["pending_records"], 1)
        # 故障详情与列表继续可查
        record = self.service.get_record(NOC, 1)
        self.assertEqual(record["reference"], "OLD-1")
        self.assertEqual(len(self.service.list_records(NOC)), 1)

        self.seed_resources()
        with self.assertRaises(Conflict):
            self.submit("K-1", [1])

    def test_backfill_unlocks_formation_and_audits(self):
        self._legacy_db()
        self.seed_resources()
        result = self.service.run_backfill(ADMIN, batch_size=10)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["status"], "completed")
        timeline = self.service.timeline(NOC, 1)
        self.assertIn("backfilled", [event["action"] for event in timeline])
        formation = self.submit("K-1", [1])["formation"]
        self.assertEqual(formation["state"], "proposed")

    def test_new_records_after_upgrade_are_ready_immediately(self):
        f1 = self.create_fault("F-1")
        self.assertEqual(f1["formation_ready"], 1)


class RecoveryTest(FormationBase):
    def test_reconcile_rebuilds_from_confirmed_formations(self):
        import sqlite3
        f1 = self.create_fault("F-1")
        self.seed_resources()
        formation = self.submit("K-1", [f1["id"]])["formation"]
        self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})

        # 模拟崩溃后漂移：游离占用行、备缆余量被改坏、游离幂等键
        connection = sqlite3.connect(self.service.repository.db_path, timeout=15)
        connection.execute(
            "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (formation["id"], "vessel", 1, "2026-12-01T00:00:00+00:00",
             "2026-12-02T00:00:00+00:00", 0, "2026-10-04T00:00:00+00:00"),
        )
        connection.execute("UPDATE spare_batches SET available_km=999.0")
        connection.execute(
            "INSERT INTO idempotency_keys(idempotency_key,formation_id,created_at) VALUES('ghost',99999,?)",
            ("2026-10-04T00:00:00+00:00",),
        )
        connection.commit()
        connection.close()

        report = self.service.recover(ADMIN)
        self.assertEqual(report["confirmed_formations"], 1)
        self.assertEqual(report["active_allocations"], 3)
        allocations = self.service.busy_allocations(DISPATCHER)
        self.assertEqual(len(allocations), 3)
        self.assertFalse(any(item["window_start"].startswith("2026-12-01") for item in allocations))
        self.assertEqual(self.service.list_batches(DISPATCHER)[0]["available_km"], round(100.0 - 15.75, 6))

        # 恢复后重放确认仍然幂等
        replay = self.service.confirm_formation(DISPATCHER, formation["id"], {"idempotency_key": "CK-1"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.service.busy_allocations(DISPATCHER)), 3)
        self.assertEqual(self.service.list_batches(DISPATCHER)[0]["available_km"], round(100.0 - 15.75, 6))


if __name__ == "__main__":
    unittest.main()
