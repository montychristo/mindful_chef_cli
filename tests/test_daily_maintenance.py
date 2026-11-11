from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import daily_maintenance


class DailyMaintenanceTests(unittest.TestCase):
    def test_source_baseline_survives_outage_and_detects_change_after_recovery(self):
        source = {"key": "example", "label": "Example help", "url": "https://example.com/help", "digest": "a" * 64}
        baseline = daily_maintenance.render_report("2030-01-01", [], [source], "")
        outage = daily_maintenance.render_report("2030-01-02", [], [dict(source, digest=None)], baseline)
        recovery = daily_maintenance.render_report("2030-01-03", [], [dict(source, digest="b" * 64)], outage)
        self.assertIn("| Unavailable |", outage)
        self.assertEqual(daily_maintenance.previous_fingerprints(outage), {"example": "sha256" + "a" * 64})
        self.assertIn("Changed since last successful check", recovery)

    def test_same_day_rerun_leaves_report_and_does_not_repeat_checks(self):
        day = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
        with TemporaryDirectory() as directory:
            report = Path(directory) / "HEALTH.md"
            report.write_text(f"# Daily maintenance\n\n**Checked on {day}**\n", encoding="utf-8")
            with patch.object(daily_maintenance, "REPORT", report), \
                    patch.object(daily_maintenance, "check_contracts") as contracts, \
                    patch("builtins.print"):
                self.assertEqual(daily_maintenance.main([]), 0)
            contracts.assert_not_called()
            self.assertEqual(report.read_text(encoding="utf-8"), f"# Daily maintenance\n\n**Checked on {day}**\n")
