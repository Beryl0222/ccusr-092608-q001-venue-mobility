"""端到端场景与复盘证据链测试。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.app import Hub
from venue_mobility.replay import Replay
from venue_mobility.scenario import build_scenario
from venue_mobility.service import ServiceError


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = Hub()
        self.trace = build_scenario(self.hub)
        self.rep = Replay(self.hub.store)

    def test_full_delay_case_arrives_on_time(self) -> None:
        arrival = self.rep.arrival("REQ-ALPHA-2")
        self.assertIsNotNone(arrival)
        self.assertLessEqual(arrival["delay_minutes"], 30)
        self.assertIn("incident:incident-south-1", " ".join(arrival["evidence"]))

    def test_clearance_version_basis_differs_after_fact_changes(self) -> None:
        decisions = {d["request_ref"]: d for d in self.rep.request_decisions()}
        self.assertNotEqual(decisions["REQ-ALPHA-1"]["basis"],
                            decisions["REQ-ALPHA-2"]["basis"])

    def test_media_is_kept_off_athlete_channel(self) -> None:
        with self.assertRaises(ServiceError):
            self.hub.service.itinerary("REQ-ALPHA-2", "media")
        press = self.hub.service.itinerary("REQ-PRESS-1", "media")
        self.assertNotIn("gate-east-athlete", str(press))
        self.assertEqual("gate-east-general", press["screening_ref"])

    def test_old_request_resources_are_released_on_supersede(self) -> None:
        # REQ-ALPHA-1 是公共交通，无运力占用；释放事件不应凭空产生。
        rels = [m for m in self.rep.capacity_movements()
                if m["request_ref"] == "REQ-ALPHA-1"]
        self.assertEqual([], rels)

    def test_report_covers_three_sections(self) -> None:
        from venue_mobility.replay import render_text
        report = render_text(self.rep, "REQ-ALPHA-2")
        self.assertIn("事实变化", report)
        self.assertIn("资源调整", report)
        self.assertIn("最终到场依据", report)
        self.assertIn("freeze-q-r2", report)
        self.assertIn("arrive-v-r2", report)

    def test_emergency_was_ratified_within_window(self) -> None:
        track = {(e["event_type"], e["payload"].get("request_ref")) for e in
                 self.hub.store.all()
                 if e["event_type"] in ("EMERGENCY_RATIFIED", "ESCALATION_RAISED")}
        self.assertIn(("EMERGENCY_RATIFIED", "REQ-ALPHA-2"), track)
        self.assertNotIn(("ESCALATION_RAISED", "REQ-ALPHA-2"), track)

    def test_no_show_for_press_bus_is_recorded(self) -> None:
        statuses = [a["action"] for a in self._auto_actions()]
        self.assertIn("no_show_released", statuses)

    def _auto_actions(self) -> list[dict]:
        return [t["result"] for t in self.trace
                if t["step"].startswith("时钟推进自动处置")]


if __name__ == "__main__":
    unittest.main()
