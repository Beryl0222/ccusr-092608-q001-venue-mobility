import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.clock import ControllableClock
from venue_mobility.service import MobilityService
from venue_mobility.store import EventStore
from venue_mobility.projections import list_demands, project_demand
from venue_mobility.errors import RoleForbidden
from tests._support import confirm_and_freeze, register_world, request_body


def cleared_service():
    svc = MobilityService(EventStore(), ControllableClock("2026-09-26T08:00:00+08:00"))
    register_world(svc)
    state, _, _ = svc.submit_demand(request_body())
    confirm_and_freeze(svc, state.demand_id)
    state, _ = svc.issue_clearance(state.demand_id)
    return svc, state


class ProjectionTests(unittest.TestCase):
    def test_team_view_is_actionable_but_hides_ledger(self) -> None:
        svc, state = cleared_service()
        view = project_demand(svc, state.demand_id, "team", identity="team-alpha")
        self.assertIn("checkpoints", view)
        self.assertEqual("gate-ath", view["access"]["gate_ref"])
        self.assertIn("depart_at", view["itinerary"])
        # 不披露车辆/司机台账、路线边、回执、指纹。
        self.assertNotIn("resource_manifest", view)
        self.assertNotIn("route_edges", json_safe(view))
        self.assertNotIn("receipt_key", view)
        self.assertNotIn("fingerprint", view)
        self.assertNotIn("confirmations", view)

    def test_team_cannot_read_other_team_trip(self) -> None:
        svc, state = cleared_service()
        with self.assertRaises(RoleForbidden):
            project_demand(svc, state.demand_id, "team", identity="team-other")
        rows = list_demands(svc, "team", identity="team-other")
        self.assertEqual([], rows)

    def test_transport_view_shares_route_and_manifest_but_not_other_gates(self) -> None:
        svc, state = cleared_service()
        view = project_demand(svc, state.demand_id, "transport")
        self.assertEqual(["e1", "e2"], view["itinerary"]["route_edges"])
        refs = {r["resource_ref"] for r in view["resource_manifest"]}
        self.assertIn("vehicle:shuttle", refs)
        self.assertNotIn("allowed_categories", str(view))

    def test_security_view_focuses_on_gate_and_load(self) -> None:
        svc, state = cleared_service()
        view = project_demand(svc, state.demand_id, "security")
        self.assertEqual("gate-ath", view["gate_ref"])
        self.assertEqual(30, view["screening_load"])
        self.assertNotIn("origin_ref", view)
        self.assertNotIn("depart_at", json_safe(view))
        self.assertNotIn("resource_manifest", view)

    def test_ops_view_carries_full_decision_basis(self) -> None:
        svc, state = cleared_service()
        view = project_demand(svc, state.demand_id, "ops")
        self.assertEqual(state.receipt_key, view["receipt_key"])
        self.assertIn("confirmations", view)
        self.assertIn("clearance", view)
        self.assertIn("fingerprint", view)

    def test_unknown_role_rejected(self) -> None:
        svc, state = cleared_service()
        with self.assertRaises(RoleForbidden):
            project_demand(svc, state.demand_id, "spectator")


def json_safe(view):
    import json
    return json.dumps(view, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
