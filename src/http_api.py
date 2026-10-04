"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
FORMATION_RE = re.compile(r"^/api/formations/(\d+)$")
FORMATION_ACTION_RE = re.compile(r"^/api/formations/(\d+)/actions/([a-z_]+)$")
FORMATION_EVENTS_RE = re.compile(r"^/api/formations/(\d+)/events$")
ADVISORY_RE = re.compile(r"^/api/advisories/(.+)$")
FORMATION_ACTIONS = {"replan", "confirm", "cancel", "complete"}


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "subsea-cable-repair/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def _query(self, parsed, name: str, default: str = None) -> str:
            return parse_qs(parsed.query).get(name, [default])[0]

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "subsea-cable-repair", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=self._query(parsed, "state"), limit=int(self._query(parsed, "limit", "100")))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if parsed.path == "/api/resources":
                    self._send(200, {"items": service.list_resources(self._actor(), self._query(parsed, "type"))})
                    return
                if parsed.path == "/api/advisories":
                    self._send(200, {"items": service.list_advisories(self._actor())})
                    return
                if parsed.path == "/api/backfill/status":
                    self._send(200, service.backfill_status(self._actor()))
                    return
                if parsed.path == "/api/formations":
                    formations = service.list_formations(
                        self._actor(),
                        state=self._query(parsed, "state"),
                        area=self._query(parsed, "area"),
                        limit=int(self._query(parsed, "limit", "100")),
                    )
                    self._send(200, {"items": formations})
                    return
                match = FORMATION_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_formation(self._actor(), int(match.group(1))))
                    return
                match = FORMATION_EVENTS_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.formation_timeline(self._actor(), int(match.group(1)))})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if parsed.path == "/api/resources":
                    self._send(201, service.register_resource(self._actor(), body.get("data", {})))
                    return
                match = ADVISORY_RE.match(parsed.path)
                if match:
                    self._send(200, service.publish_advisory(self._actor(), unquote(match.group(1)), body.get("data", {})))
                    return
                if parsed.path == "/api/backfill/run":
                    batch_size = body.get("batch_size", 200)
                    if not isinstance(batch_size, int) or batch_size <= 0:
                        raise ValidationError("batch_size必须是正整数")
                    self._send(200, service.run_backfill(self._actor(), batch_size))
                    return
                if parsed.path == "/api/formations":
                    formation = service.plan_formation(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, formation)
                    return
                match = FORMATION_ACTION_RE.match(parsed.path)
                if match:
                    action = match.group(2)
                    if action not in FORMATION_ACTIONS:
                        raise ValidationError("未知编队动作：%s" % action)
                    formation_id = int(match.group(1))
                    actor = self._actor()
                    data = body.get("data", {}) or {}
                    if action == "replan":
                        version = body.get("expected_version")
                        if not isinstance(version, int):
                            raise ValidationError("expected_version必须是整数")
                        result = service.replan_formation(actor, formation_id, version, data)
                    elif action == "confirm":
                        version = body.get("expected_version")
                        if not isinstance(version, int):
                            raise ValidationError("expected_version必须是整数")
                        key = body.get("idempotency_key")
                        if key is not None and (not isinstance(key, str) or not key.strip()):
                            raise ValidationError("idempotency_key必须是非空字符串")
                        result = service.confirm_formation(actor, formation_id, version, key)
                    elif action == "cancel":
                        result = service.cancel_formation(actor, formation_id, data.get("reason", ""))
                    else:
                        result = service.complete_formation(actor, formation_id)
                    self._send(200, result)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
