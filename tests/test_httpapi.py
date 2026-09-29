import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.httpapi import serve


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="vm-http-"))
        cls.httpd = serve(cls.dir, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _request(self, method: str, path: str, body=None, role: str | None = None,
                 identity: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if role:
            headers["X-Role"] = role
        if identity:
            headers["X-Identity"] = identity
        raw = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_flow_with_role_based_views(self) -> None:
        # 登记事实（ops）。
        for kind, key, snap in [
            ("resource_pool", "primary", {"seats_per_vehicle": 15, "pools": {
                "vehicle:shuttle": {"capacity": 4}, "driver:shuttle": {"capacity": 4},
                "accessibility_seat:shuttle": {"capacity": 6}}}),
            ("lodging", "hotel-a", {"lodging_ref": "hotel-a", "road_node": "A"}),
            ("road_network", "primary", {"edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20}},
             "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]}},
             "closed_edges": [], "incidents": {}}),
            ("schedule", "V1", {"venue_ref": "V1", "road_node": "V1"}),
            ("security_window", "gate-ath", {"gate_ref": "gate-ath", "venue_ref": "V1",
             "allowed_categories": ["athlete"], "open_from": "06:00", "open_to": "22:00",
             "priority": 1}),
        ]:
            status, data = self._request("POST", "/admin/facts",
                                         {"kind": kind, "key": key, "snapshot": snap},
                                         role="ops")
            self.assertEqual(200, status, data)

        # 非 ops 不能登记事实。
        status, data = self._request("POST", "/admin/facts",
                                     {"kind": "schedule", "key": "V1", "snapshot": {}},
                                     role="transport")
        self.assertEqual(403, status)

        # 提交申请。
        body = {"request_key": f"RK-HTTP-{id(self)}", "requester": "team-alpha",
                "category": "athlete", "headcount": 20, "accessibility_seats": 1,
                "origin_ref": "hotel-a", "venue_ref": "V1",
                "needed_at": "2026-09-26T14:00:00+08:00"}
        status, data = self._request("POST", "/demands", body)
        self.assertEqual(200, status, data)
        demand_id = data["demand_id"]

        # 三方确认（角色头必须与确认角色一致）。
        status, _ = self._request("POST", f"/demands/{demand_id}/confirmations",
                                  {"role": "team", "confirmation_key": "t1",
                                   "claims": {"headcount": 20, "accessibility_seats": 1,
                                              "origin_ref": "hotel-a", "category": "athlete"}},
                                  role="team")
        self.assertEqual(200, status)
        status, _ = self._request("POST", f"/demands/{demand_id}/confirmations",
                                  {"role": "transport", "confirmation_key": "r1",
                                   "claims": {"road_version": 1, "vehicle_ready": True}},
                                  role="transport")
        self.assertEqual(200, status)
        # 伪装成 transport 确认安保事实 -> 403。
        status, data = self._request("POST", f"/demands/{demand_id}/confirmations",
                                     {"role": "security", "confirmation_key": "s1",
                                      "claims": {"gate_ref": "gate-ath"}},
                                     role="transport")
        self.assertEqual(403, status)
        status, _ = self._request("POST", f"/demands/{demand_id}/confirmations",
                                  {"role": "security", "confirmation_key": "s1",
                                   "claims": {"gate_ref": "gate-ath",
                                              "access_category": "athlete"}},
                                  role="security")
        self.assertEqual(200, status)

        status, data = self._request("POST", f"/demands/{demand_id}/freeze", {}, role="ops")
        self.assertEqual(200, status, data)
        status, data = self._request("POST", f"/demands/{demand_id}/clearance", {}, role="ops")
        self.assertEqual(200, status, data)
        self.assertEqual((1, 1), (data["schedule_version"], data["road_version"]))

        # 角色视图：team 可看，其他队不可看，transport 看路线，ops 看依据。
        status, team_view = self._request("GET", f"/demands/{demand_id}",
                                          role="team", identity="team-alpha")
        self.assertEqual(200, status)
        self.assertIn("checkpoints", team_view["trip"])
        self.assertNotIn("resource_manifest", json.dumps(team_view, ensure_ascii=False))

        status, denied = self._request("GET", f"/demands/{demand_id}",
                                       role="team", identity="team-other")
        self.assertEqual(403, status)

        status, transport_view = self._request("GET", f"/demands/{demand_id}", role="transport")
        self.assertEqual(200, status)
        self.assertIn("resource_manifest", transport_view["trip"])

        status, no_role = self._request("GET", f"/demands/{demand_id}")
        self.assertEqual(403, status)

        # 发车 -> 到场。
        status, _ = self._request("POST", f"/demands/{demand_id}/depart", {}, role="transport")
        self.assertEqual(200, status)
        status, data = self._request("POST", f"/demands/{demand_id}/arrive", {}, role="ops")
        self.assertEqual(200, status, data)

        # ops 追踪完整依据。
        status, trace = self._request("GET", f"/demands/{demand_id}/trace", role="ops")
        self.assertEqual(200, status)
        kinds = {e["event_type"] for e in trace["events"]}
        self.assertIn("CLEARANCE_ISSUED", kinds)
        self.assertIn("ARRIVAL_CONFIRMED", kinds)

        # 重启进程后数据仍在（同一数据目录）。
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.__class__.httpd = serve(self.dir, "127.0.0.1", 0)
        self.__class__.port = self.httpd.server_address[1]
        self.__class__.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        status, data = self._request("GET", f"/demands/{demand_id}", role="ops")
        self.assertEqual(200, status)
        self.assertEqual("arrived", data["trip"]["status"])


if __name__ == "__main__":
    unittest.main()
