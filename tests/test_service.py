"""决策服务测试：归属、版本锁、超配、幂等、隔离、紧急改线、失约与恢复。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tempfile
from datetime import datetime

from venue_mobility.app import Hub
from venue_mobility.service import ServiceError


def t(h: int, m: int = 0) -> str:
    return f"2026-09-26T{h:02d}:{m:02d}:00+08:00"


def seed(hub: Hub, corridor: str = "north", vehicle: int = 2, driver: int = 2,
         aseat: int = 4, transit_status: str = "running") -> None:
    s = hub.service
    now = hub.clock.now

    def fact(receipt, ft, ref, version, owner, data):
        s.record_fact({"receipt": receipt, "fact_type": ft, "ref": ref,
                       "version": version, "owner_role": owner, "data": data}, now)

    fact(f"f-leg-{corridor}", "leg", corridor, 1, "transport",
         {"corridor": corridor, "travel_minutes": 40, "bus_seats": 40})
    fact(f"f-metro-{corridor}", "transit", f"metro-{corridor}", 1, "transport",
         {"corridor": corridor, "status": transit_status,
          "departs_at": t(11, 20), "arrives_at": t(12, 30),
          "categories_allowed": ["athlete", "media"], "accessible": True})
    for kind, cap in (("vehicle", vehicle), ("driver", driver), ("aseat", aseat)):
        fact(f"f-res-{kind}-{corridor}", "resource", f"{kind}:{corridor}", 1,
             "transport", {"capacity": cap})
    fact("f-gate-ath", "screening", "gate-athlete", 1, "security",
         {"venue": "gym", "open_from": t(9, 0), "open_to": t(14, 0), "lead_minutes": 15,
          "categories_allowed": ["athlete"], "athlete_only": True})
    fact("f-gate-med", "screening", "gate-media", 1, "security",
         {"venue": "gym", "open_from": t(9, 0), "open_to": t(14, 30), "lead_minutes": 15,
          "categories_allowed": ["media"]})
    fact("f-fixture", "fixture", "match-1", 1, "team", {"venue": "gym", "deadline": t(14, 0)})


def shuttle_body(receipt: str, ref: str, **over):
    body = {"receipt": receipt, "request_ref": ref, "team_ref": "team-a",
            "venue": "gym", "corridor": "north", "category": "athlete",
            "headcount": 38, "accessible_seats": 1, "deadline": t(14, 0)}
    body.update(over)
    return body


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = Hub()
        seed(self.hub)

    def test_fact_ownership_and_version_rules(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.hub.service.record_fact(
                {"receipt": "x1", "fact_type": "fixture", "ref": "m", "version": 1,
                 "owner_role": "security", "data": {}}, self.hub.clock.now)
        self.assertEqual("owner_mismatch", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx2:
            self.hub.service.record_fact(
                {"receipt": "x2", "fact_type": "fixture", "ref": "match-1", "version": 1,
                 "owner_role": "team", "data": {}}, self.hub.clock.now)
        self.assertEqual("stale_fact", ctx2.exception.code)

    def test_clearance_locks_versions(self) -> None:
        r = self.hub.service.submit_request(
            shuttle_body("q1", "REQ-1", category="media", accessible_seats=0,
                         team_ref="press"), self.hub.clock.now)
        self.assertTrue(r["approved"])
        self.assertEqual({"schedule": 1, "network": 7}, r["basis"])

    def test_media_cannot_use_athlete_gate_or_view(self) -> None:
        self.hub.service.submit_request(shuttle_body("q1", "REQ-ATH"), self.hub.clock.now)
        with self.assertRaises(ServiceError) as ctx:
            self.hub.service.itinerary("REQ-ATH", "media")
        self.assertEqual(403, ctx.exception.status)
        press = self.hub.service.submit_request(
            shuttle_body("q2", "REQ-PRESS", category="media", team_ref="press",
                         accessible_seats=0, deadline=t(14, 0)), self.hub.clock.now)
        self.assertEqual("gate-media", press["plan"]["screening_ref"])
        view = self.hub.service.itinerary("REQ-PRESS", "media")
        self.assertNotIn("resources", view)

    def test_overcapacity_is_denied(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        body = shuttle_body("qa", "REQ-A", corridor="south")
        first = self.hub.service.submit_request(body, self.hub.clock.now)
        self.assertTrue(first["approved"])
        second = self.hub.service.submit_request(shuttle_body("qb", "REQ-B", corridor="south"),
                                                 self.hub.clock.now)
        self.assertFalse(second["approved"])
        self.assertEqual("overcapacity", second["reason_code"])

    def test_accessible_seats_are_separate_capacity(self) -> None:
        seed(self.hub, corridor="south", vehicle=5, driver=5, aseat=2,
             transit_status="suspended")
        first = self.hub.service.submit_request(
            shuttle_body("qa", "REQ-A", corridor="south", accessible_seats=2), self.hub.clock.now)
        self.assertTrue(first["approved"])
        second = self.hub.service.submit_request(
            shuttle_body("qb", "REQ-B", corridor="south", accessible_seats=2), self.hub.clock.now)
        self.assertFalse(second["approved"])
        self.assertEqual("aseat:south", second["blocking_resource"])

    def test_duplicate_receipt_does_not_double_debit(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        body = shuttle_body("dup", "REQ-A", corridor="south")
        first = self.hub.service.submit_request(body, self.hub.clock.now)
        again = self.hub.service.submit_request(body, self.hub.clock.now)
        self.assertTrue(first["approved"])
        self.assertTrue(again["idempotent"])
        holds = self.hub.service.ledger.holds_for("REQ-A")
        self.assertEqual(3, len(holds))
        self.assertEqual(1, sum(h.quantity for h in holds if h.resource_ref == "vehicle:south"))

    def test_same_identity_same_fingerprint_new_receipt_is_replayed(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        self.hub.service.submit_request(shuttle_body("r1", "REQ-A", corridor="south"),
                                        self.hub.clock.now)
        again = self.hub.service.submit_request(shuttle_body("r2", "REQ-A", corridor="south"),
                                                self.hub.clock.now)
        self.assertTrue(again["idempotent"])
        self.assertEqual(3, len(self.hub.service.ledger.holds_for("REQ-A")))

    def test_identity_conflict_is_isolated(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        self.hub.service.submit_request(shuttle_body("r1", "REQ-A", corridor="south", headcount=20),
                                        self.hub.clock.now)
        clash = self.hub.service.submit_request(
            shuttle_body("r2", "REQ-A", corridor="south", headcount=30), self.hub.clock.now)
        self.assertFalse(clash["approved"])
        self.assertEqual("identity_conflict", clash["reason_code"])
        # 原申请与占用保留不变。
        rec = self.hub.service.requests["REQ-A"]
        self.assertEqual(20, rec.headcount)
        self.assertEqual(3, len([h for h in self.hub.service.ledger.holds_for("REQ-A") if not h.released]))

    def test_emergency_then_ratify_and_dispatch(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        self.hub.service.submit_request(shuttle_body("q0", "REQ-OTHER", corridor="south"),
                                        self.hub.clock.now)
        urgent = self.hub.service.submit_request(
            shuttle_body("q1", "REQ-URGENT", corridor="south", emergency=True,
                         reason="事故改线"), self.hub.clock.now)
        self.assertTrue(urgent["approved"])
        self.assertIsNotNone(urgent["review_due_at"])
        with self.assertRaises(ServiceError) as ctx:
            self.hub.service.confirm_dispatch("REQ-URGENT", {"receipt": "d1"}, self.hub.clock.now)
        self.assertEqual("ratification_required", ctx.exception.code)
        self.hub.service.ratify("REQ-URGENT", {"receipt": "a1", "approver": "boss"}, self.hub.clock.now)
        ok = self.hub.service.confirm_dispatch("REQ-URGENT", {"receipt": "d2"}, self.hub.clock.now)
        self.assertTrue(ok["dispatched"])

    def test_emergency_without_ratification_is_escalated_on_clock_advance(self) -> None:
        seed(self.hub, corridor="south", vehicle=1, driver=1, aseat=2,
             transit_status="suspended")
        urgent = self.hub.service.submit_request(
            shuttle_body("q1", "REQ-URGENT", corridor="south", emergency=True), self.hub.clock.now)
        due = urgent["review_due_at"]
        holds_before = [h for h in self.hub.service.ledger.holds_for("REQ-URGENT") if not h.released]
        self.hub.clock.advance(until=due)
        outcome = self.hub.service.advance(self.hub.clock.now)
        self.assertEqual("escalated_and_released", outcome["actions"][0]["action"])
        rec = self.hub.service.requests["REQ-URGENT"]
        self.assertTrue(rec.escalated)
        self.assertTrue(all(h.released for h in holds_before))
        # 再次推进不产生重复升级或重复释放。
        again = self.hub.service.advance(self.hub.clock.now)
        self.assertEqual([], again["actions"])

    def test_no_show_release_on_clock_advance(self) -> None:
        seed(self.hub, corridor="south", transit_status="suspended")
        r = self.hub.service.submit_request(shuttle_body("q1", "REQ-A", corridor="south"),
                                            self.hub.clock.now)
        pickup = datetime.fromisoformat(r["plan"]["pickup_at"])
        self.hub.clock.advance(minutes=(pickup - self.hub.clock.now).total_seconds() / 60 + 11)
        outcome = self.hub.service.advance(self.hub.clock.now)
        self.assertEqual("no_show_released", outcome["actions"][0]["action"])
        self.assertEqual(0, sum(p["active"] for p in self.hub.service.ledger.snapshot().values()))

    def test_arrival_releases_resources(self) -> None:
        seed(self.hub, corridor="south", transit_status="suspended")
        self.hub.service.submit_request(shuttle_body("q1", "REQ-A", corridor="south"),
                                        self.hub.clock.now)
        self.hub.service.confirm_dispatch("REQ-A", {"receipt": "d1"}, self.hub.clock.now)
        self.hub.service.confirm_arrival("REQ-A", {"receipt": "v1", "arrived_at": t(12, 0)},
                                         self.hub.clock.now)
        self.assertTrue(self.hub.service.requests["REQ-A"].arrived)
        self.assertEqual(0, sum(p["active"] for p in self.hub.service.ledger.snapshot().values()))

    def test_operator_view_sees_full_basis_media_does_not(self) -> None:
        self.hub.service.submit_request(shuttle_body("q1", "REQ-ATH"), self.hub.clock.now)
        op = self.hub.service.itinerary("REQ-ATH", "operator")
        self.assertIn("basis", op)
        self.assertIn("plan", op)
        with self.assertRaises(ServiceError):
            self.hub.service.itinerary("REQ-ATH", "media")

    def test_ratification_after_escalation_is_rejected(self) -> None:
        seed(self.hub, corridor="south", transit_status="suspended")
        urgent = self.hub.service.submit_request(
            shuttle_body("q1", "REQ-URGENT", corridor="south", emergency=True), self.hub.clock.now)
        self.hub.clock.advance(until=urgent["review_due_at"])
        self.hub.service.advance(self.hub.clock.now)
        with self.assertRaises(ServiceError) as ctx:
            self.hub.service.ratify("REQ-URGENT", {"receipt": "late", "approver": "boss"},
                                    self.hub.clock.now)
        self.assertEqual("ratification_window_closed", ctx.exception.code)


class RecoveryTests(unittest.TestCase):
    def test_restart_keeps_original_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hub = Hub(state)
            seed(hub, corridor="south", vehicle=1, driver=1, aseat=2, transit_status="suspended")
            urgent = hub.service.submit_request(
                shuttle_body("q1", "REQ-URGENT", corridor="south", emergency=True), hub.clock.now)
            due = urgent["review_due_at"]

            # 模拟进程重启：从磁盘重建时钟与日志，截止时间不重新计算。
            hub2 = Hub(state)
            self.assertEqual(due, hub2.service.requests["REQ-URGENT"].review_due_at.isoformat())
            hub2.clock.advance(until=due)
            outcome = hub2.service.advance(hub2.clock.now)
            self.assertEqual("escalated_and_released", outcome["actions"][0]["action"])


if __name__ == "__main__":
    unittest.main()
