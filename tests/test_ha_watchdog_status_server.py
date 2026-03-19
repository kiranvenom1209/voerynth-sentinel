import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("tinytuya", MagicMock())

import ha_watchdog_status_server as status_server


class OfflineDashboardAssetTests(unittest.TestCase):
    def test_html_uses_only_local_font_and_logo_assets(self):
        html = status_server.HTML
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)
        self.assertNotIn("raw.githubusercontent.com", html)
        self.assertIn("/assets/fonts/kumbh-sans-400.ttf", html)
        self.assertIn("/assets/fonts/jetbrains-mono-500.ttf", html)
        self.assertIn("/assets/images/logo-gold-200.png", html)

    def test_vendored_assets_exist_on_disk(self):
        expected_assets = [
            "fonts/kumbh-sans-400.ttf",
            "fonts/jetbrains-mono-500.ttf",
            "fonts/playfair-display-700.ttf",
            "images/logo-gold-200.png",
        ]
        for relative_path in expected_assets:
            self.assertTrue((status_server.ASSETS_DIR / relative_path).is_file(), relative_path)

    def test_build_payload_disables_remote_probe_in_offline_mode(self):
        handler = status_server.Handler.__new__(status_server.Handler)
        stats = {
            "consecutive_failures": 0,
            "reboots_last_hour": 0,
            "last_reboot_ts": None,
            "in_cooldown": False,
            "in_boot_grace": False,
        }
        fake_log_file = MagicMock()
        fake_log_file.exists.return_value = False

        with patch.object(status_server, "ENABLE_REMOTE_CHECK", False), \
             patch.object(status_server, "check_url") as mock_check_url, \
             patch.object(status_server, "get_plug_status", return_value={"ok": True, "relay_on": True}), \
             patch.object(status_server, "read_recent_logs", return_value=[]), \
             patch.object(status_server, "parse_log_stats", return_value=stats), \
             patch.object(status_server, "LOG_FILE", fake_log_file):
            mock_check_url.side_effect = [
                {"ok": True, "status": 403, "error": None, "url": status_server.HA_CORE_URL},
                {"ok": True, "status": 200, "error": None, "url": status_server.HA_OBSERVER_URL},
            ]

            payload = status_server.Handler.build_payload(handler)

        self.assertEqual(mock_check_url.call_count, 2)
        self.assertFalse(payload["remote"]["enabled"])
        self.assertEqual(payload["remote"]["error"], "Remote check disabled or not configured")
        self.assertEqual(payload["remote"]["url"], status_server.NABU_CASA_URL)

    def test_build_payload_disables_remote_probe_when_url_is_missing(self):
        handler = status_server.Handler.__new__(status_server.Handler)
        stats = {
            "consecutive_failures": 0,
            "reboots_last_hour": 0,
            "last_reboot_ts": None,
            "in_cooldown": False,
            "in_boot_grace": False,
        }
        fake_log_file = MagicMock()
        fake_log_file.exists.return_value = False

        with patch.object(status_server, "ENABLE_REMOTE_CHECK", True), \
             patch.object(status_server, "NABU_CASA_URL", ""), \
             patch.object(status_server, "check_url") as mock_check_url, \
             patch.object(status_server, "get_plug_status", return_value={"ok": True, "relay_on": True}), \
             patch.object(status_server, "read_recent_logs", return_value=[]), \
             patch.object(status_server, "parse_log_stats", return_value=stats), \
             patch.object(status_server, "LOG_FILE", fake_log_file):
            mock_check_url.side_effect = [
                {"ok": True, "status": 403, "error": None, "url": status_server.HA_CORE_URL},
                {"ok": True, "status": 200, "error": None, "url": status_server.HA_OBSERVER_URL},
            ]

            payload = status_server.Handler.build_payload(handler)

        self.assertEqual(mock_check_url.call_count, 2)
        self.assertFalse(payload["remote"]["enabled"])