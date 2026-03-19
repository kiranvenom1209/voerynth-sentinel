import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules.setdefault("paramiko", MagicMock())
sys.modules.setdefault("tinytuya", MagicMock())

import ha_watchdog


class DecideCoreFailureActionTests(unittest.TestCase):
    def test_wait_states_pause_counters(self):
        for state in ("starting", "rebuilding", "updating"):
            self.assertEqual(ha_watchdog.decide_core_failure_action(state), "wait")

    def test_stopped_falls_back_to_non_fast_track_logic(self):
        self.assertEqual(
            ha_watchdog.decide_core_failure_action("stopped"),
            "fallback",
        )

    def test_dead_fast_tracks_hard_recovery(self):
        self.assertEqual(
            ha_watchdog.decide_core_failure_action("dead"),
            "fast_track_hard",
        )

    def test_unknown_falls_back_to_existing_observer_logic(self):
        self.assertEqual(ha_watchdog.decide_core_failure_action("unknown"), "fallback")

    def test_running_falls_back_to_existing_observer_logic(self):
        self.assertEqual(ha_watchdog.decide_core_failure_action("running"), "fallback")


class IntentionalRebootGraceTests(unittest.TestCase):
    def test_stopped_state_starts_grace_when_none_is_active(self):
        self.assertEqual(
            ha_watchdog.decide_intentional_reboot_grace_action(
                "stopped",
                grace_deadline=0.0,
                grace_started_ts=0.0,
                now=100.0,
            ),
            "start",
        )

    def test_active_grace_suppresses_dead_state_recovery(self):
        self.assertEqual(
            ha_watchdog.decide_intentional_reboot_grace_action(
                "dead",
                grace_deadline=250.0,
                grace_started_ts=100.0,
                now=150.0,
            ),
            "wait",
        )

    def test_expired_grace_does_not_rearm_on_repeated_stopped_state(self):
        self.assertEqual(
            ha_watchdog.decide_intentional_reboot_grace_action(
                "stopped",
                grace_deadline=120.0,
                grace_started_ts=60.0,
                now=180.0,
            ),
            "resume",
        )


class CoreOfflineRebootWindowTests(unittest.TestCase):
    def test_non_wait_outage_starts_reboot_window(self):
        self.assertTrue(
            ha_watchdog.should_start_core_offline_reboot_window(
                "fallback",
                grace_deadline=0.0,
                grace_started_ts=0.0,
            )
        )

    def test_wait_state_does_not_start_reboot_window(self):
        self.assertFalse(
            ha_watchdog.should_start_core_offline_reboot_window(
                "wait",
                grace_deadline=0.0,
                grace_started_ts=0.0,
            )
        )

    def test_existing_reboot_window_does_not_restart(self):
        self.assertFalse(
            ha_watchdog.should_start_core_offline_reboot_window(
                "dead",
                grace_deadline=250.0,
                grace_started_ts=100.0,
            )
        )

    def test_observer_alive_keeps_waiting_during_reboot_window(self):
        self.assertEqual(
            ha_watchdog.decide_reboot_window_observer_action(
                observer_ok=True,
                grace_deadline=250.0,
                now=150.0,
            ),
            "wait",
        )

    def test_observer_offline_triggers_recovery_during_reboot_window(self):
        self.assertEqual(
            ha_watchdog.decide_reboot_window_observer_action(
                observer_ok=False,
                grace_deadline=250.0,
                now=150.0,
            ),
            "recover",
        )

    def test_expired_reboot_window_resumes_normal_logic(self):
        self.assertEqual(
            ha_watchdog.decide_reboot_window_observer_action(
                observer_ok=True,
                grace_deadline=120.0,
                now=180.0,
            ),
            "resume",
        )


class GetCoreStateViaSshTests(unittest.TestCase):
    @staticmethod
    def _exec_result(payload=None, stderr_text=""):
        stdout = MagicMock()
        stderr = MagicMock()
        if payload is None:
            stdout.read.return_value = b""
        elif isinstance(payload, bytes):
            stdout.read.return_value = payload
        else:
            stdout.read.return_value = json.dumps(payload).encode()
        stderr.read.return_value = stderr_text.encode()
        return (MagicMock(), stdout, stderr)

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_starting_when_supervisor_is_setting_up(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.return_value = self._exec_result({"data": {"state": "setup"}})
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "starting")
        ssh.connect.assert_called_once_with(
            ha_watchdog.HA_HOST,
            port=ha_watchdog.HA_SSH_PORT,
            username=ha_watchdog.HA_SSH_USER,
            timeout=ha_watchdog.SSH_INVESTIGATION_TIMEOUT,
        )

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_starting_for_active_nested_core_start_job(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result({"data": {"state": "running"}}),
            self._exec_result(
                {
                    "data": {
                        "jobs": [
                            {
                                "name": "job_group",
                                "done": False,
                                "stage": None,
                                "reference": "group-1",
                                "child_jobs": [
                                    {
                                        "name": "home_assistant_core_start",
                                        "done": False,
                                        "stage": None,
                                        "reference": None,
                                        "child_jobs": [],
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "starting")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_rebuilding_when_restore_job_is_active(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result({"data": {"state": "running"}}),
            self._exec_result(
                {
                    "data": {
                        "jobs": [
                            {
                                "name": "backup_restore_homeassistant",
                                "done": False,
                                "stage": None,
                                "reference": "backup-1",
                                "child_jobs": [],
                            }
                        ]
                    }
                }
            ),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "rebuilding")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_updating_when_update_job_is_active(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result({"data": {"state": "running"}}),
            self._exec_result(
                {
                    "data": {
                        "jobs": [
                            {
                                "name": "home_assistant_core_update",
                                "done": False,
                                "stage": None,
                                "reference": None,
                                "child_jobs": [],
                            }
                        ]
                    }
                }
            ),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "updating")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_stopped_when_supervisor_running_and_no_active_jobs(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result({"data": {"state": "running"}}),
            self._exec_result({"data": {"jobs": []}}),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "stopped")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_unknown_when_supervisor_and_jobs_cannot_classify(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result({"data": {"version": "2026.3.1"}}),
            self._exec_result({"data": {"jobs": []}}),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "unknown")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_unknown_when_json_is_invalid(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result(b"not-json"),
            self._exec_result({"data": {"jobs": []}}),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "unknown")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_unknown_when_json_missing(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._exec_result(None, stderr_text="some stderr"),
            self._exec_result({"data": {"jobs": []}}),
        ]
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "unknown")

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_returns_dead_when_ssh_fails(self, mock_ssh_client, mock_policy):
        ssh = MagicMock()
        ssh.connect.side_effect = RuntimeError("boom")
        mock_ssh_client.return_value = ssh

        self.assertEqual(ha_watchdog.get_core_state_via_ssh(), "dead")


class RestoreBackupSelectionTests(unittest.TestCase):
    def test_select_restore_backup_slug_accepts_real_multi_location_backup_shape(self):
        backups_info = {
            "data": {
                "backups": [
                    {
                        "slug": "older-local",
                        "date": "2026-03-18T04:13:02.008471+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": True},
                    },
                    {
                        "slug": "multi-location-newest",
                        "date": "2026-03-19T07:11:21.622034+00:00",
                        "location": None,
                        "locations": [None, "Local_NAS"],
                        "location_attributes": {
                            ".local": {"protected": True, "size_bytes": 6344550400},
                            "Local_NAS": {"protected": True, "size_bytes": 6344550400},
                        },
                        "content": {"homeassistant": True},
                    },
                ]
            }
        }

        self.assertEqual(
            ha_watchdog._select_restore_backup_slug(backups_info),
            "multi-location-newest",
        )

    def test_select_restore_backup_slug_prefers_local_nas_over_newer_local_only_backup(self):
        backups_info = {
            "data": {
                "backups": [
                    {
                        "slug": "local-only-newest",
                        "date": "2026-03-19T08:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": True},
                    },
                    {
                        "slug": "local-nas-older",
                        "date": "2026-03-19T07:11:21.622034+00:00",
                        "location": None,
                        "locations": [None, "Local_NAS"],
                        "location_attributes": {
                            ".local": {"protected": True},
                            "Local_NAS": {"protected": True},
                        },
                        "content": {"homeassistant": True},
                    },
                    {
                        "slug": "broken-location",
                        "date": "2026-03-16T13:00:00+00:00",
                        "location": "   ",
                        "locations": [],
                        "location_attributes": {},
                        "content": {"homeassistant": True},
                    },
                ]
            }
        }

        self.assertEqual(
            ha_watchdog._select_restore_backup_slug(backups_info),
            "local-nas-older",
        )

    def test_select_restore_backup_slug_falls_back_to_newest_restore_available_homeassistant_backup(self):
        backups_info = {
            "data": {
                "backups": [
                    {
                        "slug": "addon-only",
                        "date": "2026-03-16T11:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": False},
                    },
                    {
                        "slug": "ha-older",
                        "date": "2026-03-14T08:51:20.034998+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": True},
                    },
                    {
                        "slug": "ha-newest",
                        "date": "2026-03-16T10:15:16.538157+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": True},
                    },
                ]
            }
        }

        self.assertEqual(
            ha_watchdog._select_restore_backup_slug(backups_info),
            "ha-newest",
        )

    def test_select_restore_backup_slug_returns_none_when_no_homeassistant_backup_exists(self):
        backups_info = {
            "data": {
                "backups": [
                    {
                        "slug": "addon-only",
                        "date": "2026-03-16T11:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": False},
                    }
                ]
            }
        }

        self.assertIsNone(ha_watchdog._select_restore_backup_slug(backups_info))


class TriggerSshBackupRestoreTests(unittest.TestCase):
    @patch("ha_watchdog._run_ha_cli_json_via_ssh")
    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_trigger_restore_uses_selected_homeassistant_backup_slug(
        self,
        mock_ssh_client,
        mock_policy,
        mock_run_json,
    ):
        ssh = MagicMock()
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stderr.read.return_value = b""
        ssh.exec_command.return_value = (MagicMock(), stdout, stderr)
        mock_ssh_client.return_value = ssh
        mock_run_json.return_value = {
            "data": {
                "backups": [
                    {
                        "slug": "addon-only",
                        "date": "2026-03-16T11:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": False},
                    },
                    {
                        "slug": "local-only-newer",
                        "date": "2026-03-19T08:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": True},
                    },
                    {
                        "slug": "restore-me",
                        "date": "2026-03-19T07:11:21.622034+00:00",
                        "location": None,
                        "locations": [None, "Local_NAS"],
                        "location_attributes": {
                            ".local": {"protected": True},
                            "Local_NAS": {"protected": True},
                        },
                        "content": {"homeassistant": True},
                    },
                ]
            }
        }

        with patch.object(ha_watchdog, "BACKUP_PASS", "test-backup-password"):
            self.assertTrue(ha_watchdog.trigger_ssh_backup_restore())

        expected_inner_command = (
            f"ha backups restore restore-me --password {ha_watchdog.shlex.quote('test-backup-password')}"
        )
        expected_restore_cmd = f"bash -l -c {ha_watchdog.shlex.quote(expected_inner_command)}"
        ssh.exec_command.assert_called_once_with(expected_restore_cmd)

    @patch("ha_watchdog._run_ha_cli_json_via_ssh")
    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_trigger_restore_fails_when_no_homeassistant_backup_is_available(
        self,
        mock_ssh_client,
        mock_policy,
        mock_run_json,
    ):
        ssh = MagicMock()
        mock_ssh_client.return_value = ssh
        mock_run_json.return_value = {
            "data": {
                "backups": [
                    {
                        "slug": "addon-only",
                        "date": "2026-03-16T11:00:00+00:00",
                        "location": None,
                        "locations": [None],
                        "location_attributes": {".local": {"protected": True}},
                        "content": {"homeassistant": False},
                    }
                ]
            }
        }

        self.assertFalse(ha_watchdog.trigger_ssh_backup_restore())

    @patch("ha_watchdog.paramiko.AutoAddPolicy")
    @patch("ha_watchdog.paramiko.SSHClient")
    def test_trigger_restore_fails_when_backup_password_is_missing(
        self,
        mock_ssh_client,
        mock_policy,
    ):
        with patch.object(ha_watchdog, "BACKUP_PASS", ""):
            self.assertFalse(ha_watchdog.trigger_ssh_backup_restore())

        mock_ssh_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()