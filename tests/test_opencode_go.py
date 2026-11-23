import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "proxy", Path(__file__).resolve().parents[1] / "services/usage-server.py"
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class OpenCodeGoUsageTests(unittest.TestCase):
    def setUp(self):
        self._orig_auth_path = proxy._OPENCODE_GO_AUTH_PATH
        self._orig_token_cache = dict(proxy._opencode_go_token_cache)
        self._orig_usage_cache = dict(proxy._opencode_go_usage_cache)
        self._orig_env = os.environ.get(proxy._OPENCODE_GO_KEY_ENV)
        os.environ.pop(proxy._OPENCODE_GO_KEY_ENV, None)
        proxy._opencode_go_token_cache.update({"api_key": "", "at": 0.0, "mtime": 0.0})
        proxy._opencode_go_usage_cache.update({
            "body": b"",
            "dict": None,
            "at": 0.0,
            "cooldown_until": 0.0,
            "last_error_status": 0,
        })

    def tearDown(self):
        proxy._OPENCODE_GO_AUTH_PATH = self._orig_auth_path
        proxy._opencode_go_token_cache.update(self._orig_token_cache)
        proxy._opencode_go_usage_cache.update(self._orig_usage_cache)
        if self._orig_env is None:
            os.environ.pop(proxy._OPENCODE_GO_KEY_ENV, None)
        else:
            os.environ[proxy._OPENCODE_GO_KEY_ENV] = self._orig_env

    def test_build_opencode_go_dict_parses_windows(self):
        now = datetime.now(timezone.utc)
        raw = json.dumps({
            "usage": {
                "rolling": {
                    "status": "ok",
                    "percent": 18,
                    "resetsAt": (now + timedelta(hours=3)).isoformat(),
                },
                "weekly": {
                    "status": "rate-limited",
                    "percent": 100,
                    "resetsAt": (now + timedelta(days=2)).isoformat(),
                },
                "monthly": {
                    "status": "ok",
                    "percent": 42,
                    "resetsAt": (now + timedelta(days=12)).isoformat(),
                },
            }
        })

        result = proxy._build_opencode_go_dict(raw)
        self.assertEqual(result["five_h"], 18)
        self.assertGreater(result["five_h_resets_in"], 0)
        self.assertEqual(result["five_h_status"], "ok")
        self.assertEqual(result["seven_d"], 100)
        self.assertEqual(result["seven_d_status"], "rate-limited")
        self.assertEqual(result["monthly"], 42)
        self.assertTrue(result["limit_reached"])
        self.assertGreater(result["observed_at"], 0)

    def test_build_opencode_go_dict_handles_missing_window_and_rejects_empty(self):
        raw = json.dumps({
            "usage": {
                "rolling": {"status": "ok", "percent": 0, "resetsAt": None},
            }
        })
        result = proxy._build_opencode_go_dict(raw)
        self.assertEqual(result["five_h"], 0)
        self.assertIsNone(result["seven_d"])
        self.assertIsNone(result["monthly"])
        self.assertFalse(result["limit_reached"])

        with self.assertRaises(ValueError):
            proxy._build_opencode_go_dict(json.dumps({"usage": {}}))

    def test_get_key_reads_keychain_auth_file_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text(json.dumps({"opencode-go": {"type": "api", "key": "file-key"}}))
            proxy._OPENCODE_GO_AUTH_PATH = auth_file

            with mock.patch.object(proxy, "_read_opencode_go_keychain", return_value=""):
                self.assertEqual(proxy._get_opencode_go_key(), "file-key")

            with mock.patch.object(proxy, "_read_opencode_go_keychain", return_value="keychain-key"):
                self.assertEqual(proxy._get_opencode_go_key(force=True), "keychain-key")

            os.environ[proxy._OPENCODE_GO_KEY_ENV] = "env-key"
            self.assertEqual(proxy._get_opencode_go_key(), "env-key")

    def test_fetch_caches_and_handles_auth_cooldown(self):
        os.environ[proxy._OPENCODE_GO_KEY_ENV] = "expired-key"
        with mock.patch.object(proxy, "_fetch_opencode_go", return_value=(401, {}, b'{"error":"expired"}')) as fetch_mock:
            status1, _, _, parsed1 = proxy._fetch_opencode_go_usage()
            status2, _, _, parsed2 = proxy._fetch_opencode_go_usage()

        self.assertEqual(status1, 401)
        self.assertIsNone(parsed1)
        self.assertEqual(status2, 401)
        self.assertIsNone(parsed2)
        self.assertEqual(fetch_mock.call_count, 1)

    def test_fetch_respects_429_retry_after_and_stale_fallback_on_network_error(self):
        os.environ[proxy._OPENCODE_GO_KEY_ENV] = "valid-key"
        payload = json.dumps({
            "usage": {
                "rolling": {"status": "ok", "percent": 15, "resetsAt": None},
                "weekly": {"status": "ok", "percent": 30, "resetsAt": None},
                "monthly": {"status": "ok", "percent": 45, "resetsAt": None},
            }
        }).encode()

        with mock.patch.object(proxy, "_fetch_opencode_go", return_value=(200, {}, payload)):
            status, _, _, parsed = proxy._fetch_opencode_go_usage()
        self.assertEqual(status, 200)
        self.assertEqual(parsed["monthly"], 45)

        proxy._opencode_go_usage_cache["at"] -= proxy._OPENCODE_GO_USAGE_TTL + 1
        with mock.patch.object(proxy, "_fetch_opencode_go", side_effect=OSError("timeout")):
            status, _, _, parsed = proxy._fetch_opencode_go_usage()
        self.assertEqual(status, 200)
        self.assertEqual(parsed["monthly"], 45)

        proxy._opencode_go_usage_cache["body"] = b""
        proxy._opencode_go_usage_cache["dict"] = None
        with mock.patch.object(proxy, "_fetch_opencode_go", return_value=(429, {"Retry-After": "120"}, b'{"error":"rate_limited"}')) as fetch_mock:
            status, headers, _, _ = proxy._fetch_opencode_go_usage()
            status_during_cd, _, _, _ = proxy._fetch_opencode_go_usage()
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("Retry-After"), "120")
        self.assertEqual(status_during_cd, 429)
        self.assertEqual(fetch_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
