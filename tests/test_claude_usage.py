import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "proxy", Path(__file__).resolve().parents[1] / "services/usage-server.py"
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ClaudeUsageTests(unittest.TestCase):
    def test_scoped_limit_includes_reset_and_status(self):
        reset_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        result = proxy._build_claude_dict(json.dumps({
            "limits": [{
                "kind": "weekly_scoped",
                "percent": 26,
                "resets_at": reset_at,
                "is_active": True,
                "severity": "warning",
                "scope": {"model": {"display_name": "Fable"}},
            }],
        }))

        self.assertEqual(result["fable"], 26)
        self.assertGreater(result["fable_resets_in"], 0)
        self.assertTrue(result["fable_active"])
        self.assertEqual(result["fable_severity"], "warning")


if __name__ == "__main__":
    unittest.main()
