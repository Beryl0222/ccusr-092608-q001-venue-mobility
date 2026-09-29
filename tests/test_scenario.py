import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.scenario import ScenarioRunner, load_scenario
from venue_mobility.store import EventStore
from venue_mobility.clock import ControllableClock
from venue_mobility.service import MobilityService

SCENARIO = ROOT / "data/scenario-delay.json"


class ScenarioReplayTests(unittest.TestCase):
    def test_delay_scenario_full_evidence_chain(self) -> None:
        scenario = load_scenario(SCENARIO)
        runner = ScenarioRunner(EventStore(), scenario["start"])
        runner.run(scenario)
        report = runner.build_report(scenario["title"])

        # 唯一失败步骤是狼群队超配（脚本标注的预期拒绝）。
        failures = [s for s in report["steps"] if s["error"]]
        self.assertEqual(1, len(failures))
        self.assertIn("capacity_exhausted", failures[0]["error"])

        by_alias = {d["alias"]: d for d in report["demands"]}
        blue = by_alias["blue"]
        press = by_alias["press"]
        wolves = by_alias["wolves"]

        # 蓝队：锁定路网 v2，延误 5 分钟到场，立案，改线已批准。
        self.assertEqual("arrived", blue["status"])
        self.assertEqual(2, blue["road_version"])
        self.assertEqual(5, blue["delay_minutes"])
        self.assertIsNotNone(blue["case_id"])
        self.assertEqual("ratified", blue["revision_history"][0]["status"])

        # 媒体：公交停运后改接驳，准时到场。
        self.assertEqual("arrived", press["status"])
        self.assertEqual(0, press["delay_minutes"])

        # 狼群队：未获得放行（运力超配被原子拒绝）。
        self.assertEqual("frozen", wolves["status"])

        # 复盘文本包含事实变化、资源调整、到场三个段落。
        text = runner.render_text(report)
        self.assertIn("事实变化", text)
        self.assertIn("资源调整", text)
        self.assertIn("到场", text)

    def test_scenario_persistence_and_recovery(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vm-scenario-"))
        try:
            scenario = load_scenario(SCENARIO)
            store = EventStore(tmp / "events.jsonl")
            runner = ScenarioRunner(store, scenario["start"])
            runner.run(scenario)
            event_count = len(store.events)

            # 用落盘日志重建：状态、台账、时钟全部可复原。
            reopened = EventStore(tmp / "events.jsonl")
            svc = MobilityService(reopened, ControllableClock(reopened.load_clock()))
            self.assertEqual(event_count, len(reopened.events))
            blue = svc.demands["demand-0001"]
            self.assertEqual("arrived", blue.status)
            self.assertEqual("2026-09-27T14:05:00+08:00", blue.arrived_at)
            # 所有到场队伍运力已归还，狼群从未放行：台账归零。
            self.assertEqual(0, sum(u["held"] for u in svc.inventory.usage()))
            # 改线历史在恢复后仍可复盘。
            self.assertEqual("ratified", blue.revision_history[0]["status"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
