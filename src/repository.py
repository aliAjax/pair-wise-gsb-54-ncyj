"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, DuplicateSubmission, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any) -> Any:
    return json.loads(value) if value is not None else None


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    formation_ready INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS formations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    client_key TEXT,
                    state TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    spare_required_km REAL NOT NULL,
                    assignments TEXT NOT NULL,
                    gaps TEXT NOT NULL,
                    superseded_by INTEGER,
                    idempotency_key TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_formations_client_key
                    ON formations(client_key) WHERE client_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_formations_idempotency_key
                    ON formations(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_formations_state ON formations(state);

                CREATE TABLE IF NOT EXISTS formation_faults (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formation_id INTEGER NOT NULL REFERENCES formations(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_fault_active
                    ON formation_faults(record_id) WHERE active = 1;
                CREATE INDEX IF NOT EXISTS idx_fault_formation ON formation_faults(formation_id);

                CREATE TABLE IF NOT EXISTS formation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formation_id INTEGER NOT NULL REFERENCES formations(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_formation_events ON formation_events(formation_id, id);

                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rtype TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(rtype, code)
                );

                CREATE TABLE IF NOT EXISTS spare_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    initial_km REAL NOT NULL,
                    available_km REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formation_id INTEGER NOT NULL REFERENCES formations(id),
                    alloc_type TEXT NOT NULL,
                    resource_id INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    spare_km REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_allocations_lookup
                    ON resource_allocations(alloc_type, resource_id, window_start, window_end);
                CREATE INDEX IF NOT EXISTS idx_allocations_formation ON resource_allocations(formation_id);

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    formation_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedule_windows (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    epoch INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            if columns and "formation_ready" not in columns:
                # 存量库升级：旧故障默认未回填，由管理员执行回填后才允许参加编队
                connection.execute("ALTER TABLE records ADD COLUMN formation_ready INTEGER NOT NULL DEFAULT 0")
            else:
                connection.execute("UPDATE records SET formation_ready=1 WHERE formation_ready IS NULL")
            self._seed(connection)

    @staticmethod
    def _seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO schedule_windows(id,epoch,updated_by,updated_at) VALUES(1,1,'system',?)",
            (_now(),),
        )
        connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('backfill_status','pending')")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ---------------------------------------------------------------- records
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,formation_ready) VALUES(?,?,?,?,?,?,?,?,1)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def get_many(self, record_ids: List[int]) -> List[Dict[str, Any]]:
        if not record_ids:
            return []
        marks = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records WHERE id IN (%s)" % marks, tuple(record_ids)).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        return [self._row(by_id[record_id]) for record_id in record_ids if record_id in by_id]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------- backfill
    def backfill_pending_total(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM records WHERE formation_ready=0").fetchone()
        return int(row["total"])

    def backfill_next_batch(self, actor_id: str, batch_size: int) -> int:
        """把一批存量故障标记为已回填，并逐条补审计；返回本批处理数量。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, version FROM records WHERE formation_ready=0 ORDER BY id LIMIT ?",
                (max(1, int(batch_size)),),
            ).fetchall()
            for row in rows:
                record_id = int(row["id"])
                connection.execute("UPDATE records SET formation_ready=1 WHERE id=?", (record_id,))
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "backfilled", actor_id, int(row["version"]),
                     json.dumps({"summary": "存量故障已回填，可参加抢修编队"}, ensure_ascii=False), now),
                )
            remaining = connection.execute("SELECT COUNT(*) AS total FROM records WHERE formation_ready=0").fetchone()["total"]
            status = "completed" if int(remaining) == 0 else "running"
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('backfill_status',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (status,),
            )
            connection.commit()
        return len(rows)

    def migration_status(self) -> Dict[str, Any]:
        with self._connect() as connection:
            pending = connection.execute("SELECT COUNT(*) AS total FROM records WHERE formation_ready=0").fetchone()["total"]
            total = connection.execute("SELECT COUNT(*) AS total FROM records").fetchone()["total"]
            row = connection.execute("SELECT value FROM meta WHERE key='backfill_status'").fetchone()
            status = row["value"] if row else "completed"
            if status in ("pending", "running") and int(pending) == 0:
                status = "completed"
            if status == "completed" and int(pending) > 0:
                status = "pending"
        return {"status": status, "pending_records": int(pending), "total_records": int(total)}

    # -------------------------------------------------------------- schedule
    def current_epoch(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT epoch FROM schedule_windows WHERE id=1").fetchone()
        return int(row["epoch"])

    # -------------------------------------------------------------- resources
    def register_resource(self, rtype: str, code: str, name: str, details: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO resources(rtype,code,name,details,active,created_at) VALUES(?,?,?,?,1,?)",
                    (rtype, code, name, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                resource_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源编码已存在") from exc
        return self._resource_row(row)

    @staticmethod
    def _resource_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def list_resources(self, rtype: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM resources WHERE rtype=? AND active=1 ORDER BY id", (rtype,)
            ).fetchall()
        return [self._resource_row(row) for row in rows]

    def register_batch(self, code: str, name: str, total_km: float) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO spare_batches(code,name,initial_km,available_km,active,created_at) VALUES(?,?,?,?,1,?)",
                    (code, name, total_km, total_km, now),
                )
                batch_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM spare_batches WHERE id=?", (batch_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("备缆批次编码已存在") from exc
        return dict(row)

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM spare_batches WHERE active=1 ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def active_allocations(self) -> List[Dict[str, Any]]:
        """资源占用快照，只统计仍生效编队（confirmed/completed）的占用。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ra.* FROM resource_allocations ra "
                "JOIN formations f ON f.id = ra.formation_id "
                "WHERE f.state IN ('confirmed','completed') ORDER BY ra.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def busy_allocations(self, alloc_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT ra.*, f.reference AS formation_reference FROM resource_allocations ra "
            "JOIN formations f ON f.id = ra.formation_id "
            "WHERE f.state IN ('confirmed','completed')"
        )
        params: List[Any] = []
        if alloc_type:
            sql += " AND ra.alloc_type=?"
            params.append(alloc_type)
        sql += " ORDER BY ra.id"
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- formations
    @staticmethod
    def _formation_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["assignments"] = json.loads(item["assignments"])
        item["gaps"] = json.loads(item["gaps"])
        return item

    def get_formation(self, formation_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
        if row is None:
            raise NotFound("编队不存在")
        return self._formation_row(row)

    def list_formations(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM formations WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM formations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._formation_row(row) for row in rows]

    def formation_faults(self, formation_id: int, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = (
            "SELECT r.* FROM formation_faults ff JOIN records r ON r.id = ff.record_id "
            "WHERE ff.formation_id=?"
        )
        if active_only:
            sql += " AND ff.active=1"
        sql += " ORDER BY r.id"
        with self._connect() as connection:
            rows = connection.execute(sql, (formation_id,)).fetchall()
        return [self._row(row) for row in rows]

    def find_formation_by_client_key(self, client_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM formations WHERE client_key=?", (client_key,)).fetchone()
        return self._formation_row(row) if row is not None else None

    def find_formation_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM formations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._formation_row(row) if row is not None else None

    def submit_formation(self, formation: Dict[str, Any], fault_ids: List[int], actor_id: str) -> Dict[str, Any]:
        """提交编队方案。client_key 唯一保证两名调度员同时提交只有一版生效。"""
        now = _now()
        assignments = formation["assignments"]
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # 同一编队的并发提交以client_key为准：先到的一版生效，后到者取回该版本
                key_row = connection.execute(
                    "SELECT * FROM formations WHERE client_key=?", (formation["client_key"],)
                ).fetchone()
                if key_row is not None:
                    connection.rollback()
                    raise DuplicateSubmission(self._formation_row(key_row))
                # 二次核对：故障未回填或已在其他生效编队中则拒绝
                placeholders = ",".join("?" for _ in fault_ids)
                not_ready = connection.execute(
                    "SELECT COUNT(*) AS total FROM records WHERE id IN (%s) AND formation_ready=0" % placeholders,
                    tuple(fault_ids),
                ).fetchone()["total"]
                if int(not_ready):
                    connection.rollback()
                    raise Conflict("存在尚未完成回填的故障单，不能参加新编队")
                active = connection.execute(
                    "SELECT COUNT(*) AS total FROM formation_faults WHERE record_id IN (%s) AND active=1" % placeholders,
                    tuple(fault_ids),
                ).fetchone()["total"]
                if int(active):
                    connection.rollback()
                    raise Conflict("存在故障单已编入其他生效编队")
                cursor = connection.execute(
                    "INSERT INTO formations(reference,client_key,state,epoch,window_start,window_end,"
                    "spare_required_km,assignments,gaps,version,created_by,updated_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                    (formation["reference"], formation["client_key"], formation["state"], formation["epoch"],
                     formation["window_start"], formation["window_end"], formation["spare_required_km"],
                     json.dumps(assignments, ensure_ascii=False, sort_keys=True),
                     json.dumps(formation["gaps"], ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now),
                )
                formation_id = int(cursor.lastrowid)
                for record_id in fault_ids:
                    connection.execute(
                        "INSERT INTO formation_faults(formation_id,record_id,active) VALUES(?,?,1)",
                        (formation_id, record_id),
                    )
                self._insert_formation_event(connection, formation_id, "submitted", actor_id, 1,
                                             {"state": formation["state"], "gaps": formation["gaps"]}, now)
                row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            # 并发提交同一编队：让先落库的一版生效，后到者取回该版本
            existing = self.find_formation_by_client_key(formation["client_key"])
            if existing is not None:
                raise DuplicateSubmission(existing) from exc
            raise Conflict("编队或故障单约束冲突") from exc
        return self._formation_row(row)

    def confirm_formation(self, formation_id: int, idempotency_key: str, actor_id: str, spare_required_km: float) -> Dict[str, Any]:
        """单事务幂等确认：占用三类资源并扣减备缆。重放同一key不重复占用或扣减。"""
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("编队不存在")
                formation = self._formation_row(row)

                replay = connection.execute(
                    "SELECT formation_id FROM idempotency_keys WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if replay is not None:
                    connection.rollback()
                    if int(replay["formation_id"]) == formation_id:
                        return {"replayed": True, "formation": formation}
                    raise Conflict("确认键已用于其他编队")

                if formation["state"] != "proposed":
                    connection.rollback()
                    raise Conflict("只有proposed方案可以确认，当前状态为%s" % formation["state"])

                assignments = formation["assignments"]
                self._assert_resource_free(connection, "vessel", int(assignments["vessel_id"]), formation)
                self._assert_resource_free(connection, "crew", int(assignments["crew_id"]), formation)
                self._assert_resource_free(connection, "spare_batch", int(assignments["batch_id"]), formation)

                batch = connection.execute(
                    "SELECT * FROM spare_batches WHERE id=? AND active=1",
                    (int(assignments["batch_id"]),),
                ).fetchone()
                if batch is None:
                    connection.rollback()
                    raise Conflict("备缆批次不可用")
                if float(batch["available_km"]) + 1e-9 < spare_required_km:
                    connection.rollback()
                    raise Conflict("备缆批次库存不足")

                connection.execute(
                    "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (formation_id, "vessel", int(assignments["vessel_id"]),
                     formation["window_start"], formation["window_end"], 0, now),
                )
                connection.execute(
                    "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (formation_id, "crew", int(assignments["crew_id"]),
                     formation["window_start"], formation["window_end"], 0, now),
                )
                connection.execute(
                    "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (formation_id, "spare_batch", int(assignments["batch_id"]),
                     formation["window_start"], formation["window_end"], spare_required_km, now),
                )
                connection.execute(
                    "UPDATE spare_batches SET available_km = available_km - ? WHERE id=?",
                    (spare_required_km, int(assignments["batch_id"])),
                )
                connection.execute(
                    "INSERT INTO idempotency_keys(idempotency_key,formation_id,created_at) VALUES(?,?,?)",
                    (idempotency_key, formation_id, now),
                )
                self._update_formation_state(connection, formation_id, "confirmed", idempotency_key, actor_id, now)
                result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            # 幂等键并发落库竞态：已存在则按重放处理，不重复占用或扣减
            replay = self.find_formation_by_idempotency_key(idempotency_key)
            if replay is not None:
                return {"replayed": True, "formation": replay}
            raise Conflict("确认约束冲突") from exc
        return {"replayed": False, "formation": self._formation_row(result)}

    @staticmethod
    def _assert_resource_free(connection: sqlite3.Connection, alloc_type: str, resource_id: int, formation: Dict[str, Any]) -> None:
        rows = connection.execute(
            "SELECT f.id FROM resource_allocations ra JOIN formations f ON f.id = ra.formation_id "
            "WHERE ra.alloc_type=? AND ra.resource_id=? AND f.state IN ('confirmed','completed') "
            "AND ra.window_start < ? AND ? < ra.window_end",
            (alloc_type, resource_id, formation["window_end"], formation["window_start"]),
        ).fetchall()
        if rows:
            raise Conflict("%s在该出海窗口已被编队#%s占用" % (alloc_type, int(rows[0]["id"])))

    @staticmethod
    def _insert_formation_event(connection: sqlite3.Connection, formation_id: int, action: str,
                                actor_id: str, version: int, details: Dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (formation_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _update_formation_state(connection: sqlite3.Connection, formation_id: int, state: str,
                                idempotency_key: Optional[str], actor_id: str, now: str) -> None:
        connection.execute(
            "UPDATE formations SET state=?, idempotency_key=COALESCE(?, idempotency_key), "
            "version=version+1, updated_by=?, updated_at=? WHERE id=?",
            (state, idempotency_key, actor_id, now, formation_id),
        )
        version_row = connection.execute("SELECT version FROM formations WHERE id=?", (formation_id,)).fetchone()
        Repository._insert_formation_event(connection, formation_id, state, actor_id,
                                           int(version_row["version"]), {"state": state}, now)

    def cancel_formation(self, formation_id: int, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("编队不存在")
            state = row["state"]
            if state not in ("proposed", "standby", "confirmed"):
                connection.rollback()
                raise Conflict("当前状态%s不允许取消" % state)
            # confirmed取消视为返航退编：释放船/班组/批次占用并回补尚未使用的备缆
            if state == "confirmed":
                formation = self._formation_row(row)
                batch_id = formation["assignments"].get("batch_id")
                if batch_id is not None:
                    connection.execute(
                        "UPDATE spare_batches SET available_km = available_km + ? WHERE id=?",
                        (float(formation["spare_required_km"]), int(batch_id)),
                    )
                connection.execute("DELETE FROM resource_allocations WHERE formation_id=?", (formation_id,))
            connection.execute("UPDATE formation_faults SET active=0 WHERE formation_id=?", (formation_id,))
            self._update_formation_state(connection, formation_id, "cancelled", None, actor_id, now)
            connection.execute(
                "UPDATE formation_events SET details=? WHERE formation_id=? AND action='cancelled'",
                (json.dumps({"state": "cancelled", "reason": reason,
                             "released_resources": state == "confirmed"}, ensure_ascii=False), formation_id),
            )
            result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
            connection.commit()
        return self._formation_row(result)

    def complete_formation(self, formation_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM formations WHERE id=?", (formation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("编队不存在")
            if row["state"] != "confirmed":
                connection.rollback()
                raise Conflict("只有confirmed编队可以完工")
            self._update_formation_state(connection, formation_id, "completed", None, actor_id, now)
            result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
            connection.commit()
        return self._formation_row(result)

    def invalidate_open_formations(self, actor_id: str) -> int:
        """建议时段调整：epoch递增，所有未确认方案失效并释放其故障绑定。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE schedule_windows SET epoch=epoch+1, updated_by=?, updated_at=? WHERE id=1",
                (actor_id, now),
            )
            rows = connection.execute(
                "SELECT id FROM formations WHERE state IN ('proposed','standby')"
            ).fetchall()
            for row in rows:
                formation_id = int(row["id"])
                connection.execute("UPDATE formation_faults SET active=0 WHERE formation_id=?", (formation_id,))
                connection.execute(
                    "UPDATE formations SET state='invalidated', version=version+1, updated_by=?, updated_at=? WHERE id=?",
                    (actor_id, now, formation_id),
                )
                version_row = connection.execute("SELECT version FROM formations WHERE id=?", (formation_id,)).fetchone()
                self._insert_formation_event(connection, formation_id, "invalidated", actor_id,
                                             int(version_row["version"]),
                                             {"reason": "建议出海时段调整，方案失效需重算"}, now)
            epoch_row = connection.execute("SELECT epoch FROM schedule_windows WHERE id=1").fetchone()
            connection.commit()
        return int(epoch_row["epoch"])

    def invalidate_for_replan(self, formation_id: int, actor_id: str) -> None:
        """重算前置：校验状态、释放旧方案的故障绑定并把旧方案置为失效。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM formations WHERE id=?", (formation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("编队不存在")
            if row["state"] not in ("proposed", "standby", "invalidated"):
                connection.rollback()
                raise Conflict("只有未确认或已失效方案可以重算替代")
            connection.execute("UPDATE formation_faults SET active=0 WHERE formation_id=?", (formation_id,))
            connection.execute(
                "UPDATE formations SET state='invalidated', version=version+1, updated_by=?, updated_at=? WHERE id=?",
                (actor_id, now, formation_id),
            )
            version_row = connection.execute("SELECT version FROM formations WHERE id=?", (formation_id,)).fetchone()
            self._insert_formation_event(connection, formation_id, "superseded", actor_id,
                                         int(version_row["version"]),
                                         {"reason": "按新出海时段重算"}, now)
            connection.commit()

    def link_successor(self, formation_id: int, successor_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE formations SET superseded_by=? WHERE id=?",
                (successor_id, formation_id),
            )
            event = connection.execute(
                "SELECT id FROM formation_events WHERE formation_id=? AND action='superseded' ORDER BY id DESC LIMIT 1",
                (formation_id,),
            ).fetchone()
            if event is not None:
                connection.execute(
                    "UPDATE formation_events SET details=? WHERE id=?",
                    (json.dumps({"reason": "按新出海时段重算", "successor_id": successor_id}, ensure_ascii=False),
                     int(event["id"])),
                )

    def formation_timeline(self, formation_id: int) -> List[Dict[str, Any]]:
        self.get_formation(formation_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM formation_events WHERE formation_id=? ORDER BY id", (formation_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # --------------------------------------------------------------- recovery
    def reconcile(self) -> Dict[str, Any]:
        """服务崩溃后从完整编队恢复：以confirmed/completed编队为准重建占用与备缆余量。

        编队状态、分配、扣减全部在同一事务落库，正常崩溃不会留下半成品；
        本函数彻底以编队完整记录为唯一事实源，删除任何游离/漂移的占用行，
        并按编队扣减重算批次余量，保证重放确认不会重复占用或扣减。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = int(connection.execute("SELECT COUNT(*) AS total FROM resource_allocations").fetchone()["total"])
            rows = connection.execute(
                "SELECT * FROM formations WHERE state IN ('confirmed','completed') ORDER BY id"
            ).fetchall()
            connection.execute("DELETE FROM resource_allocations")
            connection.execute(
                "DELETE FROM idempotency_keys WHERE formation_id NOT IN (SELECT id FROM formations)"
            )
            rebuilt = 0
            for row in rows:
                formation = self._formation_row(row)
                assignments = formation["assignments"]
                if "vessel_id" in assignments:
                    connection.execute(
                        "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                        "VALUES(?,?,?,?,?,0,?)",
                        (formation["id"], "vessel", int(assignments["vessel_id"]),
                         formation["window_start"], formation["window_end"], now),
                    )
                    rebuilt += 1
                if "crew_id" in assignments:
                    connection.execute(
                        "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                        "VALUES(?,?,?,?,?,0,?)",
                        (formation["id"], "crew", int(assignments["crew_id"]),
                         formation["window_start"], formation["window_end"], now),
                    )
                    rebuilt += 1
                if "batch_id" in assignments:
                    connection.execute(
                        "INSERT INTO resource_allocations(formation_id,alloc_type,resource_id,window_start,window_end,spare_km,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (formation["id"], "spare_batch", int(assignments["batch_id"]),
                         formation["window_start"], formation["window_end"], float(formation["spare_required_km"]), now),
                    )
                    rebuilt += 1
            batches = connection.execute("SELECT * FROM spare_batches").fetchall()
            for batch in batches:
                used_row = connection.execute(
                    "SELECT COALESCE(SUM(ra.spare_km),0) AS used FROM resource_allocations ra "
                    "WHERE ra.alloc_type='spare_batch' AND ra.resource_id=?",
                    (int(batch["id"]),),
                ).fetchone()
                connection.execute(
                    "UPDATE spare_batches SET available_km=? WHERE id=?",
                    (round(float(batch["initial_km"]) - float(used_row["used"]), 6), int(batch["id"])),
                )
            allocations = connection.execute("SELECT COUNT(*) AS total FROM resource_allocations").fetchone()["total"]
            connection.commit()
        return {"removed_orphan_allocations": max(0, before - rebuilt), "rebuilt_allocations": rebuilt,
                "confirmed_formations": len(rows), "active_allocations": int(allocations), "recovered_at": now}
