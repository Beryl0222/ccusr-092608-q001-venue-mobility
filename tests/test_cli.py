"""命令行与契约一致性测试。"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility import cli

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_legacy_validate_still_works(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main([str(ROOT / "contracts/domain.schema.json"),
                             str(ROOT / "data/sample.json")])
        self.assertEqual(0, code)
        self.assertIn("valid", buf.getvalue())

    def test_scenario_then_replay_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(["scenario", "--state", tmp])
            self.assertEqual(0, code)
            self.assertTrue((Path(tmp) / "events.jsonl").exists())
            self.assertTrue((Path(tmp) / "clock.json").exists())

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                code = cli.main(["replay", "--state", tmp, "--request", "REQ-ALPHA-2"])
            self.assertEqual(0, code)
            report = buf2.getvalue()
            self.assertIn("事实变化", report)
            self.assertIn("REQ-ALPHA-2", report)

            buf3 = io.StringIO()
            out_json = Path(tmp) / "report.json"
            with redirect_stdout(buf3):
                code = cli.main(["replay", "--state", tmp, "--json", "--json-out", str(out_json)])
            self.assertEqual(0, code)
            data = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(any(a["request_ref"] == "REQ-ALPHA-2" for a in data["arrivals"] if a))


if __name__ == "__main__":
    unittest.main()
