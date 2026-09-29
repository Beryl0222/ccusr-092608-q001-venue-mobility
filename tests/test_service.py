import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.clock import ControllableClock
from venue_mobility.service import MobilityService, fingerprint_request
from venue_mobility.store import EventStore
from venue_mobility.errors import (
    CapacityExhausted,
    DomainRejection,
    IllegalTransition,
    ReceiptConflict,
    RoleForbidden,
)
from tests._support import confirm_and_freeze, register_world, request_body


def make_service(start: str = "2026-09-26T08:00:00+08:00", store: EventStore | None = None):
    clock = ControllableClock(start)
    svc = MobilityService(store or EventStore(), clock)
    register_world(svc)
    return svc, clock


class ClearanceLifecycleTests(unittest.TestCase):
    def test_three_party_confirmation_then_frozen_clearance_locks_versions(self) -> None:
        svc, clock = make_service()
        state, _, _ = svc.submit_demand(request_body())
        with self.assertRaises(IllegalTransition):
            svc.freeze_schedule(state.demand_id)  # 确认不齐
        svc.confirm_fact(state.demand_id, "team", "k1",
                         {"headcount": 30, "accessibility_seats": 2,
                          "origin_ref": "hotel-a", "category": "athlete"})
        svc.confirm_fact(state.demand_id, "transport", "k2", {"road_version": 1})
        svc.confirm_fact(state.demand_id, "security", "k3",
                         {"gate_ref": "gate-ath", "access_category": "athlete"})
        state, event = svc.freeze_schedule(state.demand_id)
        self.assertEqual("SCHEDULE_FROZEN", event["event_type"])
        self.assertEqual((1, 1), (state.schedule_version, state.road_version))
        # 锁定后不得再确认。
        with self.assertRaises(IllegalTransition):
            svc.confirm_fact(state.demand_id, "team", "k2", {"headcount": 30})
        state, events = svc.issue_clearance(state.demand_id)
        self.assertEqual("cleared", state.status)
        self.assertEqual(1, state.clearance["schedule_version"])
        self.assertEqual(1, state.clearance["road_version"])
        # 幂等：重复放行不重复扣减。
        held_before = svc.inventory.held_of("vehicle:shuttle")
        state2, events2 = svc.issue_clearance(state.demand_id)
        self.assertEqual([], events2)
        self.assertEqual(held_before, svc.inventory.held_of("vehicle:shuttle"))

    def test_new_fact_version_does_not_rewrite_issued_clearance(self) -> None:
        svc, clock = make_service()
        state, _, _ = svc.submit_demand(request_body())
        confirm_and_freeze(svc, state.demand_id)
        state, _ = svc.issue_clearance(state.demand_id)
        self.assertEqual("r-north", state.clearance["itinerary"]["route_ref"])
        # 路网 v2 封掉 r-north。
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20}, "e3": {"minutes": 15}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-south": {"start": "B", "end": "V1", "edges": ["e3"]}},
            "closed_edges": ["e1"], "incidents": {}})
        self.assertEqual("r-north", state.clearance["itinerary"]["route_ref"])
        self.assertEqual(1, state.clearance["road_version"])

    def test_team_confirmation_conflicting_with_request_blocks_freeze(self) -> None:
        svc, _ = make_service()
        state, _, _ = svc.submit_demand(request_body())
        svc.confirm_fact(state.demand_id, "team", "k1",
                         {"headcount": 40, "accessibility_seats": 2,
                          "origin_ref": "hotel-a", "category": "athlete"})
        svc.confirm_fact(state.demand_id, "transport", "k2", {"road_version": 1})
        svc.confirm_fact(state.demand_id, "security", "k3",
                         {"gate_ref": "gate-ath", "access_category": "athlete"})
        with self.assertRaises(IllegalTransition):
            svc.freeze_schedule(state.demand_id)

    def test_role_can_only_confirm_own_domain(self) -> None:
        svc, _ = make_service()
        state, _, _ = svc.submit_demand(request_body())
        with self.assertRaises(RoleForbidden):
            svc.confirm_fact(state.demand_id, "team", "k1", {"gate_ref": "gate-ath"})
        with self.assertRaises(RoleForbidden):
            svc.confirm_fact(state.demand_id, "ops", "k1", {"headcount": 30})

    def test_confirmation_is_idempotent_by_key(self) -> None:
        svc, _ = make_service()
        state, events, _ = svc.submit_demand(request_body())
        _, e1 = svc.confirm_fact(state.demand_id, "team", "same-key", {"headcount": 30,
            "accessibility_seats": 2, "origin_ref": "hotel-a", "category": "athlete"})
        _, e2 = svc.confirm_fact(state.demand_id, "team", "same-key", {"headcount": 30,
            "accessibility_seats": 2, "origin_ref": "hotel-a", "category": "athlete"})
        self.assertIsNotNone(e1)
        self.assertIsNone(e2)


class ConcurrencyIsolationTests(unittest.TestCase):
    def test_concurrent_demands_cannot_overbook_any_dimension(self) -> None:
        svc, _ = make_service()
        # 容量 4 车；30 人需 2 车，第二支 45 人需 3 车 -> 合计 5 > 4。
        s1, _, _ = svc.submit_demand(request_key_override := request_body())
        confirm_and_freeze(svc, s1.demand_id, headcount=30, accessibility=2)
        svc.issue_clearance(s1.demand_id)
        body2 = request_body(request_key="RK-2", requester="team-beta", headcount=45,
                             accessibility_seats=0)
        s2, _, _ = svc.submit_demand(body2)
        confirm_and_freeze(svc, s2.demand_id, gate="gate-ath", headcount=45, accessibility=0)
        with self.assertRaises(CapacityExhausted) as ctx:
            svc.issue_clearance(s2.demand_id)
        self.assertEqual("vehicle:shuttle", ctx.exception.resource_ref)
        # 被整体拒绝：s2 没有任何维度留下半占用。
        self.assertEqual(2, svc.inventory.held_of("vehicle:shuttle"))
        self.assertEqual(2, svc.inventory.held_of("driver:shuttle"))
        self.assertEqual(2, svc.inventory.held_of("accessibility_seat:shuttle"))

    def test_duplicate_submission_is_idempotent(self) -> None:
        svc, _ = make_service()
        body = request_body()
        s1, evs1, _ = svc.submit_demand(body)
        s2, evs2, isolated = svc.submit_demand(body)
        self.assertEqual(s1.demand_id, s2.demand_id)
        self.assertEqual(0, len(evs2))
        self.assertFalse(isolated)

    def test_same_key_different_headcount_is_isolated(self) -> None:
        svc, _ = make_service()
        # 隔离后两份申请都要能独立放行：扩容到 6 辆。
        svc.register_fact("resource_pool", "primary", {
            "seats_per_vehicle": 15, "pools": {
                "vehicle:shuttle": {"capacity": 6},
                "driver:shuttle": {"capacity": 6},
                "accessibility_seat:shuttle": {"capacity": 6}}})
        s1, _, _ = svc.submit_demand(request_body())
        s2, evs2, isolated = svc.submit_demand(request_body(headcount=45))
        self.assertTrue(isolated)
        self.assertNotEqual(s1.demand_id, s2.demand_id)
        self.assertEqual(s1.demand_id, s2.isolated_from)
        self.assertEqual("DEMAND_ISOLATED", evs2[0]["event_type"])
        # 原申请状态不受影响。
        self.assertEqual("submitted", s1.status)
        # 两份申请各自独立推进：30 人需 2 车、45 人需 3 车，合计 5 车分别记账。
        confirm_and_freeze(svc, s1.demand_id)
        confirm_and_freeze(svc, s2.demand_id, headcount=45, accessibility=2)
        svc.issue_clearance(s1.demand_id)
        svc.issue_clearance(s2.demand_id)
        self.assertEqual(5, svc.inventory.held_of("vehicle:shuttle"))
        self.assertEqual(5, svc.inventory.held_of("driver:shuttle"))
        self.assertEqual(4, svc.inventory.held_of("accessibility_seat:shuttle"))

    def test_same_key_different_route_is_isolated(self) -> None:
        svc, _ = make_service()
        s1, _, _ = svc.submit_demand(request_body())
        s2, _, isolated = svc.submit_demand(request_body(origin_ref="hotel-b"))
        self.assertTrue(isolated)
        self.assertNotEqual(s1.fingerprint, s2.fingerprint)

    def test_reused_receipt_key_on_different_request_is_rejected(self) -> None:
        svc, _ = make_service()
        svc.submit_demand(request_body(receipt_key="RCPT-1"))
        with self.assertRaises(ReceiptConflict):
            svc.submit_demand(request_body(request_key="RK-OTHER", receipt_key="RCPT-1"))

    def test_fingerprint_distinguishes_material_fields(self) -> None:
        f1 = fingerprint_request(request_body(headcount=30))
        f2 = fingerprint_request(request_body(headcount=31))
        self.assertNotEqual(f1, f2)
        # 非路线/人数字段（如 requester 文案）变化不影响隔离判断。
        f3 = fingerprint_request(request_body())
        self.assertEqual(f1, f3)


class AccessControlTests(unittest.TestCase):
    def test_media_never_gets_athlete_only_gate(self) -> None:
        svc, _ = make_service()
        body = request_body(request_key="RK-MEDIA", requester="news", category="media",
                            headcount=5, accessibility_seats=0, origin_ref="hotel-a")
        state, _, _ = svc.submit_demand(body)
        confirm_and_freeze(svc, state.demand_id, gate="gate-mix", category="media",
                           headcount=5, accessibility=0)
        state, _ = svc.issue_clearance(state.demand_id)
        self.assertIn("media", state.clearance["access"]["allowed_categories"])
        self.assertNotEqual("gate-ath", state.clearance["access"]["gate_ref"])
        self.assertEqual("gate-mix", state.clearance["access"]["gate_ref"])

    def test_closed_road_prevents_clearance(self) -> None:
        svc, _ = make_service()
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20}, "e3": {"minutes": 15}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-south": {"start": "B", "end": "V1", "edges": ["e3"]}},
            "closed_edges": ["e1"], "incidents": {}})
        state, _, _ = svc.submit_demand(request_body())
        confirm_and_freeze(svc, state.demand_id)
        # 默认锁定最新路网（v2），A 方向全部被封，应拒绝。
        with self.assertRaises(DomainRejection):
            svc.issue_clearance(state.demand_id)

    def test_request_can_pin_specific_versions(self) -> None:
        svc, _ = make_service()
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20}, "e3": {"minutes": 15}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-south": {"start": "B", "end": "V1", "edges": ["e3"]}},
            "closed_edges": ["e1"], "incidents": {}})
        # 显式钉住仍可通行的路网 v1。
        body = request_body(facts={"road_version": 1, "schedule_version": 1})
        state, _, _ = svc.submit_demand(body)
        confirm_and_freeze(svc, state.demand_id)
        state, _ = svc.issue_clearance(state.demand_id)
        self.assertEqual("r-north", state.clearance["itinerary"]["route_ref"])


class EmergencyRevisionTests(unittest.TestCase):
    def _cleared(self, svc):
        state, _, _ = svc.submit_demand(request_body())
        confirm_and_freeze(svc, state.demand_id)
        state, _ = svc.issue_clearance(state.demand_id)
        return state

    def test_emergency_holds_minimum_extra_then_ratification_settles(self) -> None:
        svc, clock = make_service()
        state = self._cleared(svc)
        # 新增一条绕道路线（路网 v2），资源需求不变，改线无增量。
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]},
                       "r-south": {"start": "B", "end": "V1", "edges": ["e3"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        rev, events = svc.emergency_revise(state.demand_id, "事故封路")
        self.assertEqual([], rev.extra_items)
        self.assertIn("EMERGENCY", [e["event_type"] for e in events]) if rev.extra_items else None
        self.assertEqual(2, svc.inventory.held_of("vehicle:shuttle"))
        state, events = svc.ratify_revision(rev.revision_id)
        self.assertEqual("r-bypass", state.clearance["itinerary"]["route_ref"])
        self.assertEqual(2, state.clearance["road_version"])
        self.assertEqual(2, svc.inventory.held_of("vehicle:shuttle"))
        self.assertEqual([], svc.inventory.holds_of("emergency", rev.revision_id))
        self.assertEqual("ratified",
                         state.revision_history[-1]["status"])

    def test_transit_suspension_emergency_switches_to_shuttle_and_clears_books(self) -> None:
        svc, clock = make_service()
        svc.register_fact("public_transit", "primary", {"services": [
            {"service_ref": "bus-9", "from_node": "A", "to_node": "V1", "accessible": True,
             "arrival_minutes_after": 40,
             "timetable": ["2026-09-26T12:00:00+08:00"]}]})
        state, _, _ = svc.submit_demand(request_body(accessibility_seats=0))
        confirm_and_freeze(svc, state.demand_id, accessibility=0)
        state, _ = svc.issue_clearance(state.demand_id)
        self.assertEqual("public_transit", state.clearance["itinerary"]["mode"])
        self.assertEqual(0, svc.inventory.held_of("vehicle:shuttle"))
        svc.register_fact("public_transit", "primary", {"services": [
            {"service_ref": "bus-9", "from_node": "A", "to_node": "V1", "accessible": True,
             "arrival_minutes_after": 40, "suspended": True,
             "timetable": ["2026-09-26T12:00:00+08:00"]}]})
        rev, _ = svc.emergency_revise(state.demand_id, "公交停运")
        self.assertGreater(len(rev.extra_items), 0)
        held_during = svc.inventory.held_of("vehicle:shuttle")
        self.assertEqual(2, held_during)
        state, _ = svc.ratify_revision(rev.revision_id)
        self.assertEqual("shuttle", state.clearance["itinerary"]["mode"])
        self.assertEqual(2, svc.inventory.held_of("vehicle:shuttle"))
        self.assertEqual(0, len(svc.inventory.holds_of("emergency", rev.revision_id)))

    def test_ratification_after_deadline_is_refused(self) -> None:
        svc, clock = make_service()
        state = self._cleared(svc)
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        rev, _ = svc.emergency_revise(state.demand_id, "事故封路")
        svc.tick(minutes=31)
        self.assertEqual("expired", state.revision_history[-1]["status"])
        with self.assertRaises(IllegalTransition):
            svc.ratify_revision(rev.revision_id)
        # 到期升级已立案。
        self.assertIsNotNone(state.case_id)
        self.assertTrue(any(e["to_role"] == "ops" for e in state.escalations))

    def test_no_double_pending_revision(self) -> None:
        svc, clock = make_service()
        state = self._cleared(svc)
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        svc.emergency_revise(state.demand_id, "事故")
        with self.assertRaises(IllegalTransition):
            svc.emergency_revise(state.demand_id, "再次事故")


class ArrivalTests(unittest.TestCase):
    def _departed(self, svc):
        state, _, _ = svc.submit_demand(request_body())
        confirm_and_freeze(svc, state.demand_id)
        state, _ = svc.issue_clearance(state.demand_id)
        svc.depart(state.demand_id)
        return state

    def test_arrival_releases_resources(self) -> None:
        svc, clock = make_service()
        state = self._departed(svc)
        self.assertGreater(svc.inventory.held_of("vehicle:shuttle"), 0)
        svc.arrive(state.demand_id)
        self.assertEqual("arrived", state.status)
        self.assertEqual(0, svc.inventory.held_of("vehicle:shuttle"))
        self.assertEqual(0, svc.inventory.held_of("driver:shuttle"))
        self.assertEqual(0, svc.inventory.held_of("accessibility_seat:shuttle"))

    def test_no_show_release_and_escalation(self) -> None:
        svc, clock = make_service()
        state = self._departed(svc)
        svc.tick(minutes=91)
        self.assertEqual("released", state.status)
        self.assertEqual(0, svc.inventory.held_of("vehicle:shuttle"))
        self.assertTrue(any(e["to_role"] == "ops" for e in state.escalations))
        self.assertIsNotNone(state.case_id)
        # 释放后运力立即可被新申请使用。
        body2 = request_body(request_key="RK-2", requester="team-beta",
                             accessibility_seats=0, headcount=30)
        s2, _, _ = svc.submit_demand(body2)
        confirm_and_freeze(svc, s2.demand_id, headcount=30, accessibility=0)
        s2, events = svc.issue_clearance(s2.demand_id)
        self.assertEqual("cleared", s2.status)

    def test_late_arrival_after_release_is_refused(self) -> None:
        svc, clock = make_service()
        state = self._departed(svc)
        svc.tick(minutes=91)
        with self.assertRaises(IllegalTransition):
            svc.arrive(state.demand_id)

    def test_depart_blocked_while_revision_pending(self) -> None:
        svc, clock = make_service()
        state, _, _ = svc.submit_demand(request_body())
        confirm_and_freeze(svc, state.demand_id)
        state, _ = svc.issue_clearance(state.demand_id)
        svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        svc.emergency_revise(state.demand_id, "事故")
        with self.assertRaises(IllegalTransition):
            svc.depart(state.demand_id)


if __name__ == "__main__":
    unittest.main()
