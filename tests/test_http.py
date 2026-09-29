"""HTTP 接口测试：放行、角色最小披露、时钟推进与拒绝码。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from venue_mobility.app import make_server
from venue_mobility.clock import Clock
from venue_mobility.journal import EventStore
from venue_mobility.service import MobilityService


def t(h: int, m: int = 0) -> str:
    return f"2026-09-26T{h:02d}:{m:02d}:00+08:00"


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server: ThreadingHTTPServer = make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        hub = self.server.hub  # type: ignore[attr-defined]
        s = hub.service

        def fact(receipt, ft, ref, owner, data, version=1):
            s.record_fact({"receipt": receipt, "fact_type": ft, "ref": ref,
                           "version": version, "owner_role": owner, "data": data}, hub.clock.now)

        fact("l1", "leg", "north", "transport",
             {"corridor": "north", "travel_minutes": 40, "bus_seats": 40})
        fact("m1", "transit", "metro-north", "transport",
             {"corridor": "north", "status": "suspended", "departs_at": t(11),
              "arrives_at": t(12), "categories_allowed": ["athlete", "media"],
              "accessible": True})
        for kind, cap in (("vehicle", 2), ("driver", 2), ("aseat", 4)):
            fact(f"r-{kind}", "resource", f"{kind}:north", "transport", {"capacity": cap})
        fact("g-ath", "screening", "gate-athlete", "security",
             {"venue": "gym", "open_from": t(9), "open_to": t(14), "lead_minutes": 15,
              "categories_allowed": ["athlete"], "athlete_only": True})
        fact("g-med", "screening", "gate-media", "security",
             {"venue": "gym", "open_from": t(9), "open_to": t(14, 30), "lead_minutes": 15,
              "categories_allowed": ["media"]})
        fact("fx", "fixture", "match-1", "team", {"venue": "gym", "deadline": t(14)})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _call(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_and_role_projections(self) -> None:
        status, body = self._call("POST", "/requests", {
            "receipt": "q1", "request_ref": "REQ-A", "team_ref": "team-a",
            "venue": "gym", "corridor": "north", "category": "athlete",
            "headcount": 30, "accessible_seats": 1, "deadline": t(14)})
        self.assertEqual(200, status)
        self.assertTrue(body["approved"])
        self.assertEqual("shuttle", body["mode"])

        status, team = self._call("GET", "/requests/REQ-A/itinerary?role=team")
        self.assertEqual(200, status)
        self.assertIn("pickup_at", team)
        self.assertNotIn("resources", team)

        status, trans = self._call("GET", "/requests/REQ-A/itinerary?role=transport")
        self.assertEqual(200, status)
        self.assertIn("resources", trans)

        status, sec = self._call("GET", "/requests/REQ-A/itinerary?role=security")
        self.assertEqual(200, status)
        self.assertNotIn("resources", sec)
        self.assertIn("screening_ref", sec)

        status, _ = self._call("GET", "/requests/REQ-A/itinerary?role=media")
        self.assertEqual(403, status)

    def test_overcapacity_release_and_clock_advance(self) -> None:
        for ref, receipt in (("REQ-A", "qa"), ("REQ-B", "qb"), ("REQ-C", "qc")):
            status, body = self._call("POST", "/requests", {
                "receipt": receipt, "request_ref": ref, "team_ref": "t",
                "venue": "gym", "corridor": "north", "category": "athlete",
                "headcount": 30, "accessible_seats": 2, "deadline": t(14)})
            if ref == "REQ-C":
                self.assertFalse(body["approved"])
                self.assertEqual("overcapacity", body["reason_code"])
            else:
                self.assertTrue(body["approved"])

        # A 队提前发车并到场，释放占用后第三队可以使用运力。
        self.assertEqual(200, self._call("POST", "/requests/REQ-A/dispatch",
                                         {"receipt": "da"})[0])
        self.assertEqual(200, self._call("POST", "/clock/advance", {"until": t(12)})[0])
        status, body = self._call("POST", "/requests/REQ-A/arrival",
                                  {"receipt": "va", "arrived_at": t(12)})
        self.assertEqual(200, status)
        status, body = self._call("POST", "/requests", {
            "receipt": "qd", "request_ref": "REQ-D", "team_ref": "t",
            "venue": "gym", "corridor": "north", "category": "athlete",
            "headcount": 30, "accessible_seats": 2, "deadline": t(14)})
        self.assertTrue(body["approved"])

        # 未发车的 B/D 在发车宽限（13:15）过后于 13:20 被失约释放。
        status, body = self._call("POST", "/clock/advance", {"until": t(13, 20)})
        self.assertEqual(200, status)
        actions = {a["request_ref"]: a["action"] for a in body["actions"]}
        self.assertEqual("no_show_released", actions.get("REQ-B"))

    def test_media_list_is_scoped(self) -> None:
        self._call("POST", "/requests", {
            "receipt": "q1", "request_ref": "REQ-A", "team_ref": "ta",
            "venue": "gym", "corridor": "north", "category": "athlete",
            "headcount": 5, "accessible_seats": 0, "deadline": t(14)})
        self._call("POST", "/requests", {
            "receipt": "q2", "request_ref": "REQ-M", "team_ref": "press",
            "venue": "gym", "corridor": "north", "category": "media",
            "headcount": 3, "accessible_seats": 0, "deadline": t(14, 0)})
        status, body = self._call("GET", "/requests?role=media")
        refs = {r["request_ref"] for r in body["requests"]}
        self.assertEqual({"REQ-M"}, refs)


class ConstructionSanity(unittest.TestCase):
    def test_components_wire_together(self) -> None:
        svc = MobilityService(EventStore())
        self.assertIsNotNone(svc.ledger)
        clock = Clock(start=None)
        self.assertIsNotNone(clock.now.tzinfo)


if __name__ == "__main__":
    unittest.main()
