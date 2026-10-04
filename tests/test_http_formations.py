import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import build_service
from src.http_api import create_server

FAULT = {"cable": "SEA-1", "segment": "S3", "start_km": 120, "end_km": 135, "depth_m": 1800,
         "sea_state": 3, "vessel_available": True, "spare_length_km": 20,
         "permit_valid": True, "capacity_gbps": 400}


class HttpFormationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / "http.db")
        service = build_service(db_path)
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parent.parent / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def call(self, method, path, body=None, user="disp", role="dispatcher"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-User-Id", user)
        request.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_is_open(self):
        status, body = self.call("GET", "/health", user="", role="")
        self.assertEqual(status, 200)
        self.assertTrue(body["database"])

    def test_full_formation_flow_over_http(self):
        status, _ = self.call("POST", "/api/resources",
                              {"data": {"rtype": "vessel", "code": "CS-1", "name": "船1"}},
                              user="rm", role="repair_manager")
        self.assertEqual(status, 201)
        self.call("POST", "/api/resources", {"data": {"rtype": "crew", "code": "SP-1", "name": "班组1"}},
                  user="rm", role="repair_manager")
        self.call("POST", "/api/spare-batches", {"data": {"code": "B-1", "name": "批1", "total_km": 100}},
                  user="rm", role="repair_manager")

        status, f1 = self.call("POST", "/api/records", {"reference": "F-1", "data": FAULT},
                               user="noc", role="noc_operator")
        self.assertEqual(status, 201)

        # 标识字段走顶层，业务窗口/故障走data，与 /api/records 约定一致
        submit_body = {"reference": "FM-1", "client_key": "K-1",
                       "data": {"fault_ids": [f1["id"]],
                                "window_start": "2026-10-05T02:00:00Z",
                                "window_end": "2026-10-06T10:00:00Z"}}
        status, result = self.call("POST", "/api/formations", submit_body)
        self.assertEqual(status, 201)
        self.assertFalse(result["deduplicated"])
        formation_id = result["formation"]["id"]

        # 重复提交：200 + 去重标记
        status, duplicate = self.call("POST", "/api/formations",
                                      dict(submit_body, reference="FM-DUP"),
                                      user="disp2")
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["formation"]["id"], formation_id)

        status, confirmed = self.call("POST", "/api/formations/%d/confirm" % formation_id,
                                      {"data": {"idempotency_key": "CK-1"}})
        self.assertEqual(status, 200)
        self.assertFalse(confirmed["replayed"])
        status, replay = self.call("POST", "/api/formations/%d/confirm" % formation_id,
                                   {"data": {"idempotency_key": "CK-1"}})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        status, batches = self.call("GET", "/api/spare-batches")
        self.assertEqual(batches["items"][0]["available_km"], round(100.0 - 15.75, 6))

    def test_missing_identity_is_rejected(self):
        status, body = self.call("GET", "/api/formations", user="", role="")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

    def test_unknown_route(self):
        status, body = self.call("GET", "/api/nope", user="disp", role="dispatcher")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
