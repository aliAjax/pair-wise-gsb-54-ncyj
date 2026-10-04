"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
FORMATION_RE = re.compile(r"^/api/formations/(\d+)$")
FORMATION_ACTION_RE = re.compile(r"^/api/formations/(\d+)/(confirm|replan|cancel|complete)$")
FORMATION_AUDIT_RE = re.compile(r"^/api/formations/(\d+)/audit$")


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
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
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
                if parsed.path == "/api/formations":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.list_formations(self._actor(), state=query.get("state", [None])[0])})
                    return
                match = FORMATION_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_formation(self._actor(), int(match.group(1))))
                    return
                match = FORMATION_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.formation_timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/resources":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.list_resources(self._actor(), query.get("type", [None])[0])})
                    return
                if parsed.path == "/api/spare-batches":
                    self._send(200, {"items": service.list_batches(self._actor())})
                    return
                if parsed.path == "/api/allocations":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.busy_allocations(self._actor(), query.get("type", [None])[0])})
                    return
                if parsed.path == "/api/schedule":
                    self._send(200, service.schedule(self._actor()))
                    return
                if parsed.path == "/api/migration":
                    self._send(200, service.migration_status(self._actor()))
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
                if parsed.path == "/api/resources":
                    self._send(201, service.register_resource(self._actor(), body.get("data", body)))
                    return
                if parsed.path == "/api/spare-batches":
                    self._send(201, service.register_batch(self._actor(), body.get("data", body)))
                    return
                if parsed.path == "/api/formations":
                    data = dict(body.get("data") or body)
                    data.setdefault("reference", body.get("reference", ""))
                    data.setdefault("client_key", body.get("client_key", ""))
                    result = service.submit_formation(self._actor(), data)
                    self._send(201 if not result.get("deduplicated") else 200, result)
                    return
                if parsed.path == "/api/schedule/rebuild":
                    self._send(200, service.bump_schedule(self._actor()))
                    return
                if parsed.path == "/api/admin/backfill":
                    self._send(200, service.run_backfill(self._actor(), int((body.get("data") or {}).get("batch_size", 200))))
                    return
                if parsed.path == "/api/admin/recover":
                    self._send(200, service.recover(self._actor()))
                    return
                match = FORMATION_ACTION_RE.match(parsed.path)
                if match:
                    result = None
                    formation_id = int(match.group(1))
                    action = match.group(2)
                    if action == "confirm":
                        result = service.confirm_formation(self._actor(), formation_id, body.get("data", {}))
                        self._send(200, result)
                    elif action == "replan":
                        result = service.replan_formation(self._actor(), formation_id, body.get("data", {}))
                        self._send(201, result)
                    elif action == "cancel":
                        result = service.cancel_formation(self._actor(), formation_id, body.get("data", {}))
                        self._send(200, result)
                    elif action == "complete":
                        result = service.complete_formation(self._actor(), formation_id)
                        self._send(200, result)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
