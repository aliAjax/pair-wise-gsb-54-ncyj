"""SQLite 表结构与事务访问。

包含两类聚合：
- records：海缆故障单（既有）
- formations：抢修编队，含资源台账、出海建议时段、占用预订与事件流
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .formation_rules import BOOKING_ACTIVE_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS meta_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_type TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    length_km REAL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(resource_type, code)
                );
                CREATE TABLE IF NOT EXISTS advisories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS formations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    area TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    advisory_revision INTEGER NOT NULL,
                    vessel_code TEXT,
                    crew_code TEXT,
                    cable_batch_code TEXT,
                    spare_demand_km REAL NOT NULL DEFAULT 0,
                    gaps TEXT NOT NULL DEFAULT '[]',
                    idempotency_key TEXT UNIQUE,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS formation_records (
                    formation_id INTEGER NOT NULL REFERENCES formations(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    PRIMARY KEY (formation_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS resource_bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formation_id INTEGER NOT NULL REFERENCES formations(id) ON DELETE CASCADE,
                    resource_type TEXT NOT NULL,
                    resource_code TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    spare_allocated_km REAL NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS formation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formation_id INTEGER NOT NULL REFERENCES formations(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_formations_state ON formations(state);
                CREATE INDEX IF NOT EXISTS idx_formations_area ON formations(area);
                CREATE INDEX IF NOT EXISTS idx_frecords_record ON formation_records(record_id);
                CREATE INDEX IF NOT EXISTS idx_bookings_lookup
                    ON resource_bookings(resource_type, resource_code, window_start, window_end);
                CREATE INDEX IF NOT EXISTS idx_bookings_key ON resource_bookings(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_fevents_formation ON formation_events(formation_id, id);
                """
            )
            # 既有数据库升级：回填标记，回填完成前老故障单不参加新编队
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            if "formation_ready" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN formation_ready INTEGER NOT NULL DEFAULT 0")
            self._migrate_legacy_marker(connection)
            connection.commit()

    @staticmethod
    def _migrate_legacy_marker(connection: sqlite3.Connection) -> None:
        """新装库（没有历史故障单）直接完成回填；老库保持未回填等待显式回填。"""
        total = int(connection.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"])
        if total == 0:
            connection.execute(
                "INSERT OR IGNORE INTO meta_kv(key,value) VALUES('formation_backfill_status','done')"
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if "formation_ready" in item:
            item["formation_ready"] = bool(item["formation_ready"])
        return item

    @staticmethod
    def _formation_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["gaps"] = json.loads(item["gaps"])
        return item

    # ------------------------------------------------------------------ records
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

    def get_many(self, record_ids: List[int]) -> List[Dict[str, Any]]:
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE id IN (%s)" % placeholders, record_ids
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

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

    # ---------------------------------------------------------------- backfill
    def backfill_counts(self) -> Dict[str, int]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"])
            ready = int(connection.execute("SELECT COUNT(*) AS c FROM records WHERE formation_ready=1").fetchone()["c"])
            row = connection.execute("SELECT value FROM meta_kv WHERE key='formation_backfill_status'").fetchone()
        return {"total": total, "ready": ready, "pending": total - ready, "status": row["value"] if row else "pending"}

    def backfill_step(self, batch_size: int) -> Dict[str, int]:
        """分批回填，完成后写入done；回填本身幂等，可在崩溃后继续。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE records SET formation_ready=1 WHERE id IN (SELECT id FROM records WHERE formation_ready=0 ORDER BY id LIMIT ?)",
                (max(1, int(batch_size)),),
            )
            remaining = int(connection.execute("SELECT COUNT(*) AS c FROM records WHERE formation_ready=0").fetchone()["c"])
            if remaining == 0:
                connection.execute(
                    "INSERT INTO meta_kv(key,value) VALUES('formation_backfill_status','done') "
                    "ON CONFLICT(key) DO UPDATE SET value='done'"
                )
            result = self.backfill_counts_in_tx(connection)
            connection.commit()
        return result

    @staticmethod
    def backfill_counts_in_tx(connection: sqlite3.Connection) -> Dict[str, int]:
        total = int(connection.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"])
        ready = int(connection.execute("SELECT COUNT(*) AS c FROM records WHERE formation_ready=1").fetchone()["c"])
        row = connection.execute("SELECT value FROM meta_kv WHERE key='formation_backfill_status'").fetchone()
        return {"total": total, "ready": ready, "pending": total - ready, "status": row["value"] if row else "pending"}

    # --------------------------------------------------------------- resources
    def upsert_resource(self, resource: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO resources(resource_type,code,name,length_km,created_by,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(resource_type,code) DO UPDATE SET name=excluded.name, length_km=excluded.length_km",
                (resource["resource_type"], resource["code"], resource["name"], resource["length_km"], actor_id, now),
            )
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_type=? AND code=?",
                (resource["resource_type"], resource["code"]),
            ).fetchone()
        return dict(row)

    def get_resource(self, resource_type: str, code: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_type=? AND code=?", (resource_type, code)
            ).fetchone()
        return dict(row) if row else None

    def list_resources(self, resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if resource_type:
                rows = connection.execute(
                    "SELECT * FROM resources WHERE resource_type=? ORDER BY id", (resource_type,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM resources ORDER BY resource_type, id").fetchall()
        result = []
        active = ",".join("?" for _ in BOOKING_ACTIVE_STATES) or "''"
        for row in rows:
            item = dict(row)
            rtype, code = item["resource_type"], item["code"]
            if rtype == "cable_batch":
                alloc = connection.execute(
                    "SELECT COALESCE(SUM(b.spare_allocated_km),0) AS a FROM resource_bookings b "
                    "JOIN formations f ON f.id=b.formation_id "
                    "WHERE b.resource_type='cable_batch' AND b.resource_code=? AND f.state IN (%s)" % active,
                    (code, *BOOKING_ACTIVE_STATES),
                ).fetchone()["a"]
                item["allocated_km"] = round(float(alloc), 2)
                item["remaining_km"] = round(float(item["length_km"] or 0) - float(alloc), 2)
            result.append(item)
        return result

    # -------------------------------------------------------------- advisories
    def get_advisory(self, area: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM advisories WHERE area=?", (area,)).fetchone()
        return dict(row) if row else None

    def upsert_advisory(self, area: str, window_start: str, window_end: str, actor_id: str, bump: bool) -> Dict[str, Any]:
        """发布/更新建议时段。bump时revision+1，并在同一事务使未确认编队失效。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM advisories WHERE area=?", (area,)).fetchone()
            if row is None:
                revision = 1
                connection.execute(
                    "INSERT INTO advisories(area,revision,window_start,window_end,updated_by,updated_at) VALUES(?,?,?,?,?,?)",
                    (area, revision, window_start, window_end, actor_id, now),
                )
                invalidated: List[int] = []
            else:
                revision = int(row["revision"]) + 1 if bump else int(row["revision"])
                connection.execute(
                    "UPDATE advisories SET revision=?,window_start=?,window_end=?,updated_by=?,updated_at=? WHERE area=?",
                    (revision, window_start, window_end, actor_id, now, area),
                )
                invalidated = []
                if bump:
                    affected = connection.execute(
                        "SELECT id, reference, version FROM formations WHERE area=? AND state IN ('proposed','standby','invalidated')",
                        (area,),
                    ).fetchall()
                    for item in affected:
                        fid = int(item["id"])
                        invalidated.append(fid)
                        connection.execute(
                            "UPDATE formations SET state='invalidated', version=version+1, updated_by=?, updated_at=? WHERE id=?",
                            (actor_id, now, fid),
                        )
                        connection.execute(
                            "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                            (
                                fid,
                                "advisory_invalidated",
                                actor_id,
                                int(item["version"]) + 1,
                                json.dumps(
                                    {"area": area, "revision": revision, "window_start": window_start, "window_end": window_end},
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                now,
                            ),
                        )
            result = connection.execute("SELECT * FROM advisories WHERE area=?", (area,)).fetchone()
            connection.commit()
        advisory = dict(result)
        advisory["invalidated_formation_ids"] = invalidated
        return advisory

    def list_advisories(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM advisories ORDER BY area").fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- formations
    def find_formation_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM formations WHERE reference=?", (reference,)).fetchone()
        return self._formation_row(row) if row else None

    def find_open_formation_for_records(self, record_ids: List[int], exclude_id: Optional[int] = None) -> Optional[str]:
        """任一故障单已挂在未结束编队（未完成/未取消）上则返回其reference。"""
        if not record_ids:
            return None
        placeholders = ",".join("?" for _ in record_ids)
        sql = (
            "SELECT f.reference FROM formations f "
            "JOIN formation_records fr ON fr.formation_id=f.id "
            "WHERE fr.record_id IN (%s) AND f.state NOT IN ('completed','cancelled')" % placeholders
        )
        params: List[Any] = list(record_ids)
        if exclude_id is not None:
            sql += " AND f.id<>?"
            params.append(exclude_id)
        sql += " LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return str(row["reference"]) if row else None

    def insert_formation(self, formation: Dict[str, Any], record_ids: List[int], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO formations(reference,state,version,area,window_start,window_end,advisory_revision,"
                    "vessel_code,crew_code,cable_batch_code,spare_demand_km,gaps,created_by,updated_by,created_at,updated_at) "
                    "VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        formation["reference"],
                        formation["state"],
                        formation["area"],
                        formation["window_start"],
                        formation["window_end"],
                        formation["advisory_revision"],
                        formation["vessel_code"],
                        formation["crew_code"],
                        formation["cable_batch_code"],
                        formation["spare_demand_km"],
                        json.dumps(formation.get("gaps", []), ensure_ascii=False, sort_keys=True),
                        actor_id,
                        actor_id,
                        now,
                        now,
                    ),
                )
                formation_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO formation_records(formation_id,record_id) VALUES(?,?)",
                    [(formation_id, rid) for rid in record_ids],
                )
                connection.execute(
                    "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        formation_id,
                        "planned",
                        actor_id,
                        1,
                        json.dumps(
                            {
                                "state": formation["state"],
                                "record_ids": record_ids,
                                "gaps": formation.get("gaps", []),
                                "advisory_revision": formation["advisory_revision"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("编队reference已存在") from exc
        return self._formation_row(row)

    def get_formation(self, formation_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
        if row is None:
            raise NotFound("编队不存在")
        return self._formation_row(row)

    def formation_record_ids(self, formation_id: int) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_id FROM formation_records WHERE formation_id=? ORDER BY record_id", (formation_id,)
            ).fetchall()
        return [int(row["record_id"]) for row in rows]

    def list_formations(self, state: Optional[str] = None, area: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM formations"
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if area:
            clauses.append("area=?")
            params.append(area)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._formation_row(row) for row in rows]

    def formation_events(self, formation_id: int) -> List[Dict[str, Any]]:
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

    def save_formation_draft(self, formation_id: int, state: str, fields: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], expected_version: Optional[int] = None) -> Dict[str, Any]:
        """更新编队方案（replan等），支持乐观版本。不产生资源预订。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM formations WHERE id=?", (formation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("编队不存在")
            if expected_version is not None and int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE formations SET state=?,version=?,window_start=?,window_end=?,advisory_revision=?,"
                "vessel_code=?,crew_code=?,cable_batch_code=?,spare_demand_km=?,gaps=?,updated_by=?,updated_at=? WHERE id=?",
                (
                    state,
                    version,
                    fields["window_start"],
                    fields["window_end"],
                    fields["advisory_revision"],
                    fields.get("vessel_code"),
                    fields.get("crew_code"),
                    fields.get("cable_batch_code"),
                    fields["spare_demand_km"],
                    json.dumps(fields.get("gaps", []), ensure_ascii=False, sort_keys=True),
                    actor_id,
                    now,
                    formation_id,
                ),
            )
            connection.execute(
                "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (formation_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
            connection.commit()
        return self._formation_row(result)

    def set_formation_state(self, formation_id: int, state: str, actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM formations WHERE id=?", (formation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("编队不存在")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE formations SET state=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                (state, actor_id, now, formation_id),
            )
            connection.execute(
                "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (formation_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
            connection.commit()
        return self._formation_row(result)

    def booking_occupiers(self, assignments: Dict[str, Optional[str]], window_start: str, window_end: str, exclude_formation_id: Optional[int] = None) -> Dict[str, Optional[str]]:
        """时间窗重叠的占用方（半开区间）。只统计已确认且未结束的编队。"""
        result: Dict[str, Optional[str]] = {"vessel": None, "crew": None, "cable_batch": None}
        with self._connect() as connection:
            for rtype, code in (
                ("vessel", assignments.get("vessel_code")),
                ("crew", assignments.get("crew_code")),
                ("cable_batch", assignments.get("cable_batch_code")),
            ):
                if not code:
                    continue
                sql = (
                    "SELECT f.reference FROM resource_bookings b JOIN formations f ON f.id=b.formation_id "
                    "WHERE b.resource_type=? AND b.resource_code=? AND b.window_start<? AND b.window_end>? "
                    "AND f.state IN ('confirmed')"
                )
                params: List[Any] = [rtype, code, window_end, window_start]
                if exclude_formation_id is not None:
                    sql += " AND b.formation_id<>?"
                    params.append(exclude_formation_id)
                row = connection.execute(sql + " LIMIT 1", params).fetchone()
                if row:
                    result[rtype] = str(row["reference"])
        return result

    def batch_remaining(self, code: Optional[str], exclude_formation_id: Optional[int] = None) -> Optional[float]:
        if not code:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT length_km FROM resources WHERE resource_type='cable_batch' AND code=?", (code,)
            ).fetchone()
            if row is None:
                return None
            sql = (
                "SELECT COALESCE(SUM(b.spare_allocated_km),0) AS a FROM resource_bookings b "
                "JOIN formations f ON f.id=b.formation_id "
                "WHERE b.resource_type='cable_batch' AND b.resource_code=? AND f.state IN ('confirmed')"
            )
            params: List[Any] = [code]
            if exclude_formation_id is not None:
                sql += " AND b.formation_id<>?"
                params.append(exclude_formation_id)
            allocated = float(connection.execute(sql, params).fetchone()["a"])
        return round(float(row["length_km"]) - allocated, 2)

    def confirm_formation(
        self,
        formation_id: int,
        expected_version: int,
        idempotency_key: str,
        gaps: List[Dict[str, str]],
        actor_id: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """单事务确认：乐观锁 + 幂等键 + 三项预订 + 备缆扣减，崩溃后重放不重复占用。

        仍有缺口时不占资源，转为待命方案。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("编队不存在")
                if int(row["version"]) != int(expected_version):
                    connection.rollback()
                    raise Conflict("版本冲突，请刷新后重试")

                version = int(expected_version) + 1
                if gaps:
                    # 资源仍不够：保留待命方案，不占资源
                    connection.execute(
                        "UPDATE formations SET state='standby',version=?,gaps=?,updated_by=?,updated_at=? WHERE id=?",
                        (version, json.dumps(gaps, ensure_ascii=False, sort_keys=True), actor_id, now, formation_id),
                    )
                    connection.execute(
                        "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            formation_id,
                            "confirm_blocked",
                            actor_id,
                            version,
                            json.dumps({**details, "gaps": gaps}, ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
                    result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                    connection.commit()
                    return self._formation_row(result)

                bookings = [
                    ("vessel", row["vessel_code"], 0.0),
                    ("crew", row["crew_code"], 0.0),
                    ("cable_batch", row["cable_batch_code"], float(row["spare_demand_km"])),
                ]
                for rtype, code, allocated in bookings:
                    connection.execute(
                        "INSERT INTO resource_bookings(formation_id,resource_type,resource_code,window_start,window_end,spare_allocated_km,idempotency_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            formation_id,
                            rtype,
                            code,
                            row["window_start"],
                            row["window_end"],
                            allocated,
                            idempotency_key,
                            now,
                        ),
                    )
                connection.execute(
                    "UPDATE formations SET state='confirmed',version=?,gaps='[]',idempotency_key=?,updated_by=?,updated_at=? WHERE id=?",
                    (version, idempotency_key, actor_id, now, formation_id),
                )
                connection.execute(
                    "INSERT INTO formation_events(formation_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        formation_id,
                        "confirmed",
                        actor_id,
                        version,
                        json.dumps(
                            {**details, "idempotency_key": idempotency_key, "spare_allocated_km": float(row["spare_demand_km"])},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                result = connection.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("确认冲突（版本已变化或确认令牌被占用），请刷新后重试") from exc
        return self._formation_row(result)
