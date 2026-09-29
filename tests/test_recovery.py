import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.clock import ControllableClock
from venue_mobility.service import MobilityService
from venue_mobility.store import EventStore
from tests._support import confirm_and_freeze, register_world, request_body


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vm-recovery-"))
        self.store = EventStore(self.dir / "events.jsonl")
        self.clock = ControllableClock("2026-09-26T08:00:00+08:00")
        self.svc = MobilityService(self.store, self.clock)
        register_world(self.svc)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _reopen(self):
        store = EventStore(self.dir / "events.jsonl")
        clock = ControllableClock(store.load_clock())
        return MobilityService(store, clock), clock

    def test_state_and_inventory_rebuild_identically(self) -> None:
        state, _, _ = self.svc.submit_demand(request_body())
        confirm_and_freeze(self.svc, state.demand_id)
        state, _ = self.svc.issue_clearance(state.demand_id)
        held = [(u["resource_ref"], u["held"]) for u in self.svc.inventory.usage()]
        svc2, clock2 = self._reopen()
        self.assertEqual(self.clock.iso(), clock2.iso())
        recovered = svc2.demands[state.demand_id]
        self.assertEqual("cleared", recovered.status)
        self.assertEqual(held, [(u["resource_ref"], u["held"]) for u in svc2.inventory.usage()])
        self.assertEqual(
            state.clearance["itinerary"]["route_ref"],
            recovered.clearance["itinerary"]["route_ref"])

    def test_deadlines_are_absolute_and_survive_restart(self) -> None:
        state, _, _ = self.svc.submit_demand(request_body())
        confirm_and_freeze(self.svc, state.demand_id)
        state, _ = self.svc.issue_clearance(state.demand_id)
        self.svc.depart(state.demand_id)
        original_due = state.no_show_due_at
        # 进程恢复：新进程的时钟停在最后事件时刻，继续按原截止时间推进。
        svc2, clock2 = self._reopen()
        recovered = svc2.demands[state.demand_id]
        self.assertEqual(original_due, recovered.no_show_due_at)
        svc2.tick(minutes=91)
        self.assertEqual("released", svc2.demands[state.demand_id].status)

    def test_pending_emergency_revision_can_be_ratified_after_restart(self) -> None:
        state, _, _ = self.svc.submit_demand(request_body(accessibility_seats=0))
        confirm_and_freeze(self.svc, state.demand_id, accessibility=0)
        state, _ = self.svc.issue_clearance(state.demand_id)
        self.svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        revision, _ = self.svc.emergency_revise(state.demand_id, "事故封路")
        # 恢复后找到待批准改线并批准。
        svc2, _ = self._reopen()
        recovered = svc2.demands[state.demand_id]
        self.assertIsNotNone(recovered.revision)
        self.assertEqual(revision.revision_id, recovered.revision.revision_id)
        recovered2, events = svc2.ratify_revision(revision.revision_id)
        self.assertEqual("r-bypass", recovered2.clearance["itinerary"]["route_ref"])
        self.assertEqual("ratified", recovered2.revision_history[-1]["status"])
        self.assertEqual(0, len(svc2.inventory.holds_of("emergency", revision.revision_id)))

    def test_pending_revision_expires_after_restart_on_original_deadline(self) -> None:
        state, _, _ = self.svc.submit_demand(request_body(accessibility_seats=0))
        confirm_and_freeze(self.svc, state.demand_id, accessibility=0)
        state, _ = self.svc.issue_clearance(state.demand_id)
        self.svc.register_fact("road_network", "primary", {
            "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20},
                      "e3": {"minutes": 15}, "e4": {"minutes": 30}},
            "routes": {"r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
                       "r-bypass": {"start": "A", "end": "V1", "edges": ["e4", "e2"]}},
            "closed_edges": ["e1"], "incidents": {"e1": {"blocked": True}}})
        revision, _ = self.svc.emergency_revise(state.demand_id, "事故封路")
        due = revision.review_due_at
        svc2, _ = self._reopen()
        svc2.tick(to=due)  # 恰好到期，按原截止时间结算，不平移。
        self.assertEqual("expired",
                         svc2.demands[state.demand_id].revision_history[-1]["status"])

    def test_double_replay_is_idempotent(self) -> None:
        state, _, _ = self.svc.submit_demand(request_body())
        confirm_and_freeze(self.svc, state.demand_id)
        self.svc.issue_clearance(state.demand_id)
        # 对同一日志再次构建服务（相当于重放两遍同一批事件），库存不应翻倍。
        svc2, _ = self._reopen()
        svc3, _ = self._reopen()
        self.assertEqual(
            [(u["resource_ref"], u["held"]) for u in svc2.inventory.usage()],
            [(u["resource_ref"], u["held"]) for u in svc3.inventory.usage()])


if __name__ == "__main__":
    unittest.main()
