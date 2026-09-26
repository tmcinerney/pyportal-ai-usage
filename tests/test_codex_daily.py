import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("proxy", Path(__file__).resolve().parents[1] / "services/usage-server.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class DailyUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = Path(self.tmp.name) / "daily.json"
        patcher = patch.object(proxy, "_CODEX_HISTORY_PATH", self.history)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1788883200

    def sample(self, pct, offset=0, reset=1790000000, account="a"):
        return proxy._observe_codex_day({"account_id": account, "user_id": "u", "rate_limit": {
            "primary_window": {"limit_window_seconds": 604800, "used_percent": pct, "reset_at": reset}
        }}, self.now + offset)

    def test_first_sample_then_persisted_increase(self):
        self.assertIsNone(self.sample(4))
        self.assertEqual(self.sample(7, 900), 3)
        self.assertEqual(self.sample(7, 1800), 3)
        self.assertEqual(json.loads(self.history.read_text())["points"], 3)

    def test_new_day_does_not_count_overnight_gap(self):
        self.sample(4)
        self.assertIsNone(self.sample(9, 86400))
        self.assertEqual(self.sample(11, 87300), 2)

    def test_reset_account_change_and_decrease_rebaseline(self):
        self.sample(90)
        self.assertIsNone(self.sample(2, 900, reset=1790604800))
        self.assertIsNone(self.sample(3, 1800, account="b"))
        self.assertIsNone(self.sample(1, 2700, account="b"))
        self.assertEqual(self.sample(4, 3600, account="b"), 3)

    def test_corrupt_history_recovers(self):
        self.history.write_text("invalid")
        self.assertIsNone(self.sample(4))
        self.assertEqual(self.sample(7, 900), 3)

    def test_missing_quota_is_unknown(self):
        self.assertIsNone(proxy._observe_codex_day({}, self.now))

    def test_write_failure_is_unknown(self):
        with patch.object(Path, "write_text", side_effect=OSError):
            self.assertIsNone(self.sample(4))

    def test_applicable_credits_pass_through(self):
        result = proxy._build_codex_dict(json.dumps({"rate_limit_reset_credits": {
            "available_count": 2, "applicable_available_count": 0}}))
        self.assertEqual(result["reset_credits"], 2)
        self.assertEqual(result["applicable_reset_credits"], 0)
