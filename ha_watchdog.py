#!/usr/bin/env python3

import json
import os
import shlex
import signal
import subprocess
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from collections import deque
from datetime import datetime, timezone

import paramiko
import requests
import tinytuya

from runtime_config import (
    BACKUP_PASS,
    BOOT_GRACE_PERIOD,
    CHECK_INTERVAL,
    COOLDOWN_AFTER_REBOOT,
    DRY_RUN,
    HA_CORE_URL,
    HA_HOST,
    HA_OBSERVER_URL,
    HA_SSH_HOST_KEY,
    HA_SSH_PORT,
    HA_SSH_USER,
    HARD_FAILURE_THRESHOLD,
    INTENTIONAL_REBOOT_GRACE_PERIOD,
    MAX_REBOOTS_PER_HOUR,
    NETWORK_SANITY_CHECK_HOST,
    NETWORK_SANITY_CHECK_TIMEOUT,
    POST_RESTORE_BOOT_GRACE_PERIOD,
    POWER_OFF_SECONDS,
    PREFERRED_RESTORE_LOCATION,
    REBOOT_WINDOW_SECONDS,
    REQUEST_TIMEOUT,
    SOFT_FAILURE_GRACE,
    SOFT_FAILURE_TIMEOUT,
    SSH_INVESTIGATION_TIMEOUT,
    STARTUP_GRACE_PERIOD,
    TUYA_DEVICE_ID,
    TUYA_DEVICE_IP,
    TUYA_LOCAL_KEY,
    TUYA_VERSION,
    require_settings,
)

# ── Graceful shutdown flag ─────────────────────────────────────
_running = True

def _handle_signal(signum, _frame):
    global _running
    logging.getLogger("ha_watchdog").info(
        f"Signal {signum} received — watchdog shutting down gracefully"
    )
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# =========================
# CONFIG
# =========================

# Hard failure = both Core AND Observer are unreachable (machine frozen)
# Soft failure = only Core is down but Observer responds (HA restarting/updating)
CONSECUTIVE_FAILURES_REQUIRED = HARD_FAILURE_THRESHOLD  # alias for status-server compat

CORE_STATES_WAIT = {"starting", "rebuilding", "updating"}
CORE_STATES_FAST_TRACK_SOFT = set()
SUPERVISOR_STATES_WAIT = {"initialize", "setup", "startup"}

# =========================
# LOGGING
# =========================

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "watchdog.log"

logger = logging.getLogger("ha_watchdog")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    "%Y-%m-%d %H:%M:%S"
)

if not logger.handlers:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# =========================
# TUYA PLUG
# =========================

def make_plug():
    require_settings(
        TUYA_DEVICE_ID=TUYA_DEVICE_ID,
        TUYA_DEVICE_IP=TUYA_DEVICE_IP,
        TUYA_LOCAL_KEY=TUYA_LOCAL_KEY,
    )
    plug = tinytuya.OutletDevice(
        dev_id=TUYA_DEVICE_ID,
        address=TUYA_DEVICE_IP,
        local_key=TUYA_LOCAL_KEY,
        version=TUYA_VERSION
    )
    plug.set_socketPersistent(True)
    plug.set_socketNODELAY(True)
    plug.set_retry(True)
    return plug

# =========================
# HEALTH CHECKS
# =========================

def check_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        response = requests.get(url, timeout=timeout)
        return True, response.status_code, None
    except requests.RequestException as exc:
        return False, None, str(exc)

def ha_core_alive():
    ok, status, err = check_url(HA_CORE_URL)
    if ok:
        return True, status, None
    return False, None, err

def ha_observer_alive():
    ok, status, err = check_url(HA_OBSERVER_URL)
    if ok and status == 200:
        return True, status, None
    if ok:
        return False, status, f"Unexpected Observer status {status}"
    return False, None, err

def _build_ping_command(host: str, timeout: int):
    safe_timeout = max(1, int(timeout))
    if os.name == "nt":
        return ["ping", "-n", "1", "-w", str(safe_timeout * 1000), host]
    return ["ping", "-c", "1", "-W", str(safe_timeout), host]

def network_sanity_check_host_reachable(
    host: str = NETWORK_SANITY_CHECK_HOST,
    timeout: int = NETWORK_SANITY_CHECK_TIMEOUT,
):
    normalized_host = (host or "").strip()
    if not normalized_host:
        return True, "disabled"

    safe_timeout = max(1, int(timeout))
    try:
        result = subprocess.run(
            _build_ping_command(normalized_host, safe_timeout),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=safe_timeout + 2,
        )
    except FileNotFoundError as exc:
        logger.warning(
            "Network sanity check host '%s' is configured but ping is unavailable (%s). "
            "Proceeding with normal enforcement.",
            normalized_host,
            exc,
        )
        return True, "ping-unavailable"
    except subprocess.TimeoutExpired:
        return False, "ping-timeout"

    if result.returncode == 0:
        return True, "reachable"
    return False, f"ping-exit-{result.returncode}"

def should_pause_for_local_network_issue() -> bool:
    normalized_host = NETWORK_SANITY_CHECK_HOST.strip()
    if not normalized_host:
        return False

    network_ok, detail = network_sanity_check_host_reachable(
        normalized_host,
        NETWORK_SANITY_CHECK_TIMEOUT,
    )
    if network_ok:
        return False

    logger.warning(
        "Local network sanity check failed: '%s' is unreachable (%s). Pausing hard "
        "recovery enforcement because the Pi may be network-partitioned.",
        normalized_host,
        detail,
    )
    return True

def _run_ha_cli_json_via_ssh(ssh, cli_command: str, description: str):
    """Execute a Home Assistant CLI command over SSH and parse its JSON output."""
    _stdin, stdout, stderr = ssh.exec_command(
        f"bash -l -c '{cli_command}'",
        timeout=SSH_INVESTIGATION_TIMEOUT,
    )

    output = stdout.read().decode().strip()
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            logger.warning(f"Deep SSH Investigation returned invalid {description} JSON: {exc}")
            return None

    error_output = stderr.read().decode().strip()
    if error_output:
        logger.warning(f"Deep SSH Investigation returned no {description} JSON: {error_output}")
    return None

def _parse_pinned_host_key(host_key_value: str):
    """Parse a pinned SSH host key from config.

    Accepts either a full known_hosts line or a bare `<key-type> <base64-key>` entry.
    """
    normalized = (host_key_value or "").strip()
    if not normalized:
        raise RuntimeError(
            "Missing required configuration: HA_SSH_HOST_KEY. "
            "Set the Home Assistant SSH server public host key in the environment or config.env."
        )

    entry = paramiko.hostkeys.HostKeyEntry.from_line(normalized)
    if entry is None:
        entry = paramiko.hostkeys.HostKeyEntry.from_line(f"{HA_HOST} {normalized}")
    if entry is None or getattr(entry, "key", None) is None:
        raise RuntimeError(
            "Invalid HA_SSH_HOST_KEY. Provide either a full known_hosts entry or "
            "'<key-type> <base64-key>'."
        )
    return entry.key

class _PinnedHostKeyPolicy:
    """Paramiko-compatible policy that accepts only the configured host key."""

    def __init__(self, expected_key):
        self.expected_key = expected_key

    def missing_host_key(self, client, hostname, key):
        if key.get_name() != self.expected_key.get_name() or key.asbytes() != self.expected_key.asbytes():
            raise RuntimeError(f"Pinned SSH host key mismatch for {hostname}")
        client.get_host_keys().add(hostname, key.get_name(), key)

def _connect_ha_ssh_client(timeout: int):
    """Open an SSH session to HA using pinned host-key verification."""
    expected_key = _parse_pinned_host_key(HA_SSH_HOST_KEY)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(_PinnedHostKeyPolicy(expected_key))
    ssh.connect(
        HA_HOST,
        port=HA_SSH_PORT,
        username=HA_SSH_USER,
        timeout=timeout,
    )
    return ssh

def _iter_jobs(jobs):
    """Yield Supervisor jobs recursively so nested child jobs can be inspected."""
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        yield job
        yield from _iter_jobs(job.get("child_jobs"))

def _classify_active_job(job) -> str | None:
    """Map an active Supervisor job to a coarse watchdog lifecycle state."""
    if not isinstance(job, dict) or job.get("done") is True:
        return None

    fields = " ".join(
        str(job.get(key, "") or "").strip().lower()
        for key in ("name", "stage", "reference")
    )
    if not fields:
        return None

    if "await_home_assistant_restart" in fields:
        return "starting"
    if "restore" in fields or "rebuild" in fields:
        return "rebuilding"
    if "update" in fields and any(token in fields for token in ("homeassistant", "home_assistant", "core")):
        return "updating"
    if any(token in fields for token in ("homeassistant", "home_assistant", "core")) and any(
        token in fields for token in ("start", "restart")
    ):
        return "starting"
    return None

def _is_restore_available_backup(backup) -> bool:
    """Return True when Supervisor reports the backup is available for restore."""
    if not isinstance(backup, dict):
        return False

    location = backup.get("location")
    locations = backup.get("locations")
    location_attributes = backup.get("location_attributes")
    has_detailed_location_metadata = isinstance(locations, list) or isinstance(location_attributes, dict)
    if has_detailed_location_metadata:
        if isinstance(locations, list) and any(
            item is None or (isinstance(item, str) and item.strip())
            for item in locations
        ):
            return True

        if isinstance(location_attributes, dict) and any(
            name == ".local" or (isinstance(name, str) and name.strip() and not name.startswith("."))
            for name in location_attributes
        ):
            return True

        return False

    if location is None:
        return True
    if isinstance(location, str) and location.strip():
        return True

    return False

def _backup_has_restore_location(backup, location_name: str) -> bool:
    """Return True when Supervisor metadata shows the backup on the named restore location."""
    if not isinstance(backup, dict):
        return False

    normalized_location_name = str(location_name or "").strip().casefold()
    if not normalized_location_name:
        return False

    location = backup.get("location")
    if isinstance(location, str) and location.strip().casefold() == normalized_location_name:
        return True

    locations = backup.get("locations")
    if isinstance(locations, list) and any(
        isinstance(item, str) and item.strip().casefold() == normalized_location_name
        for item in locations
    ):
        return True

    location_attributes = backup.get("location_attributes")
    if isinstance(location_attributes, dict) and any(
        isinstance(name, str) and name.strip().casefold() == normalized_location_name
        for name in location_attributes
    ):
        return True

    return False

def _backup_timestamp(backup) -> float:
    """Parse the backup date for newest-first selection, falling back safely."""
    if not isinstance(backup, dict):
        return float("-inf")

    date_value = backup.get("date")
    if not isinstance(date_value, str) or not date_value.strip():
        return float("-inf")

    try:
        parsed = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

def _select_restore_backup_slug(backups_info) -> str | None:
    """Choose the newest HA backup, preferring the configured restore location when available."""
    backups = (backups_info or {}).get("data", {}).get("backups", [])
    candidates = []

    for backup in backups:
        if not isinstance(backup, dict):
            continue

        slug = backup.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue

        content = backup.get("content")
        if not isinstance(content, dict) or not content.get("homeassistant"):
            continue

        if not _is_restore_available_backup(backup):
            continue

        candidates.append(backup)

    if not candidates:
        return None

    preferred_candidates = [
        backup
        for backup in candidates
        if _backup_has_restore_location(backup, PREFERRED_RESTORE_LOCATION)
    ]
    ranked_candidates = preferred_candidates or candidates

    newest_backup = max(
        ranked_candidates,
        key=lambda backup: (_backup_timestamp(backup), backup.get("slug", "")),
    )
    return newest_backup.get("slug")

def get_core_state_via_ssh():
    """
    SSH into Home Assistant and infer the current Core lifecycle state.

    The Home Assistant CLI does not expose a direct runtime state via
    `ha core info --raw-json` in this environment, so this function combines:
      - `ha info --raw-json` for Supervisor/system state
      - `ha jobs info --raw-json` for active Core-related work

    Returns one of:
      - "starting" / "rebuilding" / "updating" when legitimate work is active
      - "stopped" when Supervisor is running but Core is not serving HTTP and no
        active Core job explains it
      - "unknown" when the SSH probe succeeds but cannot classify safely
      - "dead" if the SSH/host path is unavailable
    """
    ssh = None

    try:
        ssh = _connect_ha_ssh_client(timeout=SSH_INVESTIGATION_TIMEOUT)
        supervisor_info = _run_ha_cli_json_via_ssh(
            ssh,
            "ha info --raw-json",
            "Supervisor info",
        )
        supervisor_state = (
            str((supervisor_info or {}).get("data", {}).get("state", "") or "")
            .strip()
            .lower()
        )
        if supervisor_state in SUPERVISOR_STATES_WAIT:
            return "starting"

        jobs_info = _run_ha_cli_json_via_ssh(
            ssh,
            "ha jobs info --raw-json",
            "job info",
        )
        jobs = (jobs_info or {}).get("data", {}).get("jobs", [])
        for job in _iter_jobs(jobs):
            job_state = _classify_active_job(job)
            if job_state:
                return job_state

        if supervisor_state == "running" and jobs_info is not None:
            return "stopped"
        return "unknown"
    except RuntimeError as exc:
        logger.error(f"Deep SSH Investigation unavailable: {exc}")
        return "unknown"
    except Exception as exc:
        logger.warning(f"Deep SSH Investigation failed. Host OS appears dead: {exc}")
        return "dead"
    finally:
        if ssh is not None:
            ssh.close()

def decide_core_failure_action(core_internal_state: str) -> str:
    """Map an SSH-reported Core state to the watchdog action to take."""
    normalized_state = (core_internal_state or "unknown").strip().lower()

    if normalized_state in CORE_STATES_WAIT:
        return "wait"
    if normalized_state in CORE_STATES_FAST_TRACK_SOFT:
        return "fast_track_soft"
    if normalized_state == "dead":
        return "fast_track_hard"
    return "fallback"

def decide_intentional_reboot_grace_action(
    core_internal_state: str,
    grace_deadline: float,
    grace_started_ts: float,
    now: float,
) -> str:
    """
    Decide whether a stopped-state signal should start the reboot window, whether
    the reboot window is still active, or whether normal logic should resume.

    Returns one of:
      - "start": begin the grace window after first seeing SSH state "stopped"
      - "wait": the grace window is active; runtime logic should inspect Observer
      - "resume": no grace suppression; continue with normal watchdog logic
    """
    normalized_state = (core_internal_state or "unknown").strip().lower()

    if normalized_state == "stopped" and grace_started_ts <= 0.0:
        return "start"
    if grace_deadline > now:
        return "wait"
    return "resume"

def should_start_core_offline_reboot_window(
    investigation_action: str,
    grace_deadline: float,
    grace_started_ts: float,
) -> bool:
    """
    Start the reboot window on the first Core outage unless SSH explicitly says
    Home Assistant is in a legitimate startup/update/rebuild state.
    """
    if grace_deadline > 0.0 or grace_started_ts > 0.0:
        return False

    return (investigation_action or "fallback").strip().lower() != "wait"

def decide_reboot_window_observer_action(
    observer_ok: bool,
    grace_deadline: float,
    now: float,
) -> str:
    """While the reboot window is active, decide whether to wait or recover."""
    if grace_deadline <= now:
        return "resume"
    if observer_ok:
        return "wait"
    return "recover"

def trigger_ssh_backup_restore():
    """Connect to the HA host via SSH and restore the preferred HA backup available to Supervisor."""
    try:
        require_settings(BACKUP_PASS=BACKUP_PASS)
    except RuntimeError as exc:
        logger.error(f"SSH backup restore aborted: {exc}")
        return False

    ssh = None

    logger.info("Initiating SSH connection to trigger HA backup restore...")
    try:
        ssh = _connect_ha_ssh_client(timeout=10)
        backups_info = _run_ha_cli_json_via_ssh(
            ssh,
            "ha backups list --raw-json",
            "backup list",
        )
        backup_slug = _select_restore_backup_slug(backups_info)
        if not backup_slug:
            logger.error(
                "SSH backup restore aborted: no restore-available backup containing Home Assistant data was found."
            )
            return False

        restore_inner_command = (
            f"ha backups restore {shlex.quote(backup_slug)} --password {shlex.quote(BACKUP_PASS)}"
        )
        restore_cmd = f"bash -l -c {shlex.quote(restore_inner_command)}"

        logger.warning(f"Selected backup '{backup_slug}' for SSH restore.")
        _stdin, stdout, stderr = ssh.exec_command(restore_cmd)
        exit_status = stdout.channel.recv_exit_status()

        if exit_status == 0:
            logger.warning("SSH backup restore command executed successfully.")
            return True

        logger.error(
            f"SSH backup restore failed with exit status {exit_status}. "
            f"Error: {stderr.read().decode().strip()}"
        )
        return False
    except RuntimeError as exc:
        logger.error(f"SSH backup restore aborted: {exc}")
        return False
    except Exception as exc:
        logger.error(
            f"Failed to connect via SSH or execute restore. OS might be completely dead: {exc}"
        )
        return False
    finally:
        if ssh is not None:
            ssh.close()

def verify_post_reboot_and_restore_if_needed(consecutive_reboots: int):
    """After a power cycle, verify Core status and apply the two-strike restore rule."""
    logger.info("Boot grace period ended. Verifying HA Core status...")
    core_ok, core_status, core_err = ha_core_alive()

    if core_ok:
        logger.info(f"HA Core recovered successfully post-reboot (HTTP {core_status}).")
        return "recovered"

    if consecutive_reboots < 2:
        logger.warning(
            "HA Core is still offline after the first hard reboot. Holding off on SSH restore "
            f"and waiting for the normal cooldown ({COOLDOWN_AFTER_REBOOT}s) before a "
            "second reboot attempt."
        )
        return "await_second_strike"

    logger.error(
        "HA Core is still offline after TWO hard reboots. "
        f"Initiating SSH restore as last resort ({core_err})."
    )
    restore_success = trigger_ssh_backup_restore()
    if not restore_success:
        return "restore_failed"

    logger.warning(
        f"Backup restore initiated. Entering monitored post-restore grace for up to "
        f"{POST_RESTORE_BOOT_GRACE_PERIOD}s while HA rebuilds..."
    )
    return "restore_started"

# =========================
# RECOVERY
# =========================

def power_cycle_host():
    logger.warning("Starting power cycle via Tuya plug...")

    if DRY_RUN:
        logger.warning("DRY_RUN enabled: not actually switching the plug.")
        logger.warning(f"Would turn OFF for {POWER_OFF_SECONDS}s and then ON.")
        logger.warning("Power cycle complete")
        return True

    plug = None
    try:
        plug = make_plug()
        logger.warning("Turning plug OFF")
        off_result = plug.set_status(False, 1)
        logger.warning(f"OFF result: {off_result}")
        time.sleep(POWER_OFF_SECONDS)

        # Turn ON — one automatic retry on failure, this step is critical
        for attempt in range(1, 3):
            try:
                logger.warning(f"Turning plug ON (attempt {attempt})")
                on_result = plug.set_status(True, 1)
                logger.warning(f"ON result: {on_result}")
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.warning(f"Plug ON attempt {attempt} failed ({exc}), retrying in 3s...")
                time.sleep(3)

        logger.warning("Power cycle complete")
        return True
    except Exception as exc:
        logger.exception(f"Power cycle failed: {exc}")
        return False
    finally:
        try:
            if plug is not None:
                plug.close()
        except Exception:
            pass

# =========================
# MAIN LOOP
# =========================

def main():
    logger.info("=" * 60)
    logger.info("Vœrynth watchdog started")
    logger.info(
        f"Config: host={HA_HOST}, interval={CHECK_INTERVAL}s"
    )
    logger.info(
        f"Thresholds: hard_failure={HARD_FAILURE_THRESHOLD} consecutive checks, "
        f"soft_failure grace={SOFT_FAILURE_GRACE} checks, "
        f"soft_failure timeout={SOFT_FAILURE_TIMEOUT}s → power cycle, "
        f"startup grace={STARTUP_GRACE_PERIOD}s"
    )
    logger.info(
        f"Policy: max_reboots/hr={MAX_REBOOTS_PER_HOUR}, "
        f"cooldown={COOLDOWN_AFTER_REBOOT}s, boot_grace={BOOT_GRACE_PERIOD}s, "
        f"plug_off={POWER_OFF_SECONDS}s"
    )
    if NETWORK_SANITY_CHECK_HOST:
        logger.info(
            f"Network sanity check enabled: host={NETWORK_SANITY_CHECK_HOST}, "
            f"timeout={NETWORK_SANITY_CHECK_TIMEOUT}s"
        )
    if DRY_RUN:
        logger.warning("DRY RUN mode enabled — relay will NOT be switched")
    logger.info("=" * 60)

    # Startup grace: during system boot Observer comes up before Core.
    # Wait before the first check to avoid a false hard-failure if both
    # ports happen to be down while the NUC is still booting.
    logger.info(
        f"Startup grace: waiting {STARTUP_GRACE_PERIOD}s before first check "
        f"(Observer leads Core on boot)"
    )
    time.sleep(STARTUP_GRACE_PERIOD)
    logger.info("Startup grace complete — beginning health checks.")

    # hard_failures — counts consecutive checks where the host looks completely dead.
    # soft_failures — counts checks where the host is alive but Core looks crashed.
    # SSH investigation can pause both counters during legitimate rebuild/update states.
    hard_failures = 0
    soft_failures = 0
    soft_failure_start_ts = 0.0   # timestamp of the first soft failure in the current run
    consecutive_reboots = 0
    intentional_reboot_grace_deadline = 0.0
    intentional_reboot_grace_started_ts = 0.0
    intentional_reboot_outage_start_ts = 0.0
    post_restore_grace_deadline = 0.0
    reboot_times = deque()
    last_reboot_ts = 0.0

    while _running:
        now = time.time()

        # Expire reboot records outside the rolling window
        while reboot_times and now - reboot_times[0] > REBOOT_WINDOW_SECONDS:
            reboot_times.popleft()

        core_ok, core_status, core_err = ha_core_alive()

        if post_restore_grace_deadline > 0.0:
            if core_ok:
                logger.info(
                    f"HA Core recovered during post-restore grace window (HTTP {core_status}). "
                    "Resuming normal monitoring."
                )
                hard_failures = 0
                soft_failures = 0
                soft_failure_start_ts = 0.0
                consecutive_reboots = 0
                intentional_reboot_grace_deadline = 0.0
                intentional_reboot_grace_started_ts = 0.0
                intentional_reboot_outage_start_ts = 0.0
                post_restore_grace_deadline = 0.0
                time.sleep(CHECK_INTERVAL)
                continue

            remaining_restore_grace = int(post_restore_grace_deadline - now)
            if remaining_restore_grace > 0:
                logger.info(
                    f"Post-restore grace active: waiting for HA rebuild "
                    f"({remaining_restore_grace}s remaining)..."
                )
                time.sleep(CHECK_INTERVAL)
                continue

            logger.warning(
                "Post-restore grace expired without Core recovery — resuming normal watchdog "
                "enforcement."
            )
            post_restore_grace_deadline = 0.0

        if core_ok:
            logger.info(f"Vœrynth Core alive on 8123 (HTTP {core_status})")
            hard_failures = 0
            soft_failures = 0
            soft_failure_start_ts = 0.0
            consecutive_reboots = 0
            intentional_reboot_grace_deadline = 0.0
            intentional_reboot_grace_started_ts = 0.0
            intentional_reboot_outage_start_ts = 0.0
            post_restore_grace_deadline = 0.0
            time.sleep(CHECK_INTERVAL)
            continue

        logger.warning("HTTP Core check failed. Initiating Deep SSH Investigation...")
        core_internal_state = get_core_state_via_ssh()
        investigation_action = decide_core_failure_action(core_internal_state)
        grace_action = decide_intentional_reboot_grace_action(
            core_internal_state,
            intentional_reboot_grace_deadline,
            intentional_reboot_grace_started_ts,
            now,
        )
        expired_intentional_reboot_grace_start_ts = 0.0
        trigger_recovery = False
        skip_observer_check = False
        observer_ok = False
        observer_status = None
        observer_err = None

        if grace_action == "start" or should_start_core_offline_reboot_window(
            investigation_action,
            intentional_reboot_grace_deadline,
            intentional_reboot_grace_started_ts,
        ):
            intentional_reboot_grace_started_ts = now
            intentional_reboot_grace_deadline = now + INTENTIONAL_REBOOT_GRACE_PERIOD
            intentional_reboot_outage_start_ts = soft_failure_start_ts or now
            if grace_action == "start":
                logger.warning(
                    f"SSH investigation indicates Core is '{core_internal_state}'. Starting intentional "
                    f"reboot/shutdown grace for up to {INTENTIONAL_REBOOT_GRACE_PERIOD}s to avoid "
                    "interrupting a legitimate HA reboot or shutdown."
                )
            else:
                logger.warning(
                    f"Core is offline and SSH investigation state is '{core_internal_state}'. "
                    f"Starting a {INTENTIONAL_REBOOT_GRACE_PERIOD}s reboot/shutdown window. "
                    "If Observer goes offline during this window, initiate power-cycle."
                )
            grace_action = "wait"

        if grace_action == "wait":
            observer_ok, observer_status, observer_err = ha_observer_alive()
            reboot_window_action = decide_reboot_window_observer_action(
                observer_ok,
                intentional_reboot_grace_deadline,
                now,
            )

            if reboot_window_action == "wait":
                remaining_grace = int(intentional_reboot_grace_deadline - now)
                logger.info(
                    f"Intentional reboot/shutdown grace active; current SSH state is "
                    f"'{core_internal_state}' and Observer is alive (HTTP {observer_status}). "
                    f"Waiting ({remaining_grace}s remaining)..."
                )
                hard_failures = 0
                soft_failures = 0
                soft_failure_start_ts = 0.0
                time.sleep(CHECK_INTERVAL)
                continue

            if reboot_window_action == "recover":
                if should_pause_for_local_network_issue():
                    hard_failures = 0
                    soft_failures = 0
                    soft_failure_start_ts = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue
                logger.warning(
                    "Intentional reboot/shutdown grace active and Observer is now offline. "
                    "Initiating power-cycle immediately."
                )
                hard_failures = HARD_FAILURE_THRESHOLD - 1
                soft_failures = 0
                soft_failure_start_ts = 0.0
                trigger_recovery = True
                skip_observer_check = True

        if (
            intentional_reboot_grace_deadline > 0.0
            and now >= intentional_reboot_grace_deadline
        ):
            expired_intentional_reboot_grace_start_ts = (
                intentional_reboot_outage_start_ts
                or intentional_reboot_grace_started_ts
                or now
            )
            logger.warning(
                "Intentional reboot/shutdown grace expired without Core recovery — resuming "
                "normal watchdog enforcement."
            )
            intentional_reboot_grace_deadline = 0.0
            intentional_reboot_grace_started_ts = 0.0
            intentional_reboot_outage_start_ts = 0.0

        if not trigger_recovery and investigation_action == "wait":
            logger.info(
                f"SSH investigation indicates Core is '{core_internal_state}'. Pausing failure counters "
                "to allow the legitimate system process to finish."
            )
            hard_failures = 0
            soft_failures = 0
            soft_failure_start_ts = 0.0
            time.sleep(CHECK_INTERVAL)
            continue

        if not trigger_recovery and investigation_action == "fast_track_soft":
            logger.error(
                f"SSH investigation indicates Core is '{core_internal_state}'. Fast-tracking recovery."
            )
            hard_failures = 0
            if soft_failures == 0:
                soft_failure_start_ts = now - SOFT_FAILURE_TIMEOUT
            soft_failures = max(soft_failures, SOFT_FAILURE_GRACE) + 1
            trigger_recovery = True
            skip_observer_check = True
        elif not trigger_recovery and investigation_action == "fast_track_hard":
            logger.error(
                "SSH Investigation failed. Both Core and Host OS are unresponsive. "
                "Fast-tracking hard reboot."
            )
            hard_failures = HARD_FAILURE_THRESHOLD - 1
            soft_failures = 0
            soft_failure_start_ts = 0.0
            skip_observer_check = True

        if not skip_observer_check:
            # Core is down — check observer to classify the failure when SSH did not
            # clearly indicate a legitimate update or a definite crash.
            observer_ok, observer_status, observer_err = ha_observer_alive()

        if not skip_observer_check and observer_ok:
            # Soft failure: Core is down but machine is reachable via Observer.
            hard_failures = 0   # machine is responsive — reset hard counter
            if soft_failures == 0:
                soft_failure_start_ts = (
                    expired_intentional_reboot_grace_start_ts or now
                )
            soft_failures += 1
            elapsed = now - soft_failure_start_ts

            if elapsed >= SOFT_FAILURE_TIMEOUT:
                # Core has been down with Observer alive longer than the allowed window.
                # Likely stuck/hung rather than a normal restart — escalate.
                logger.error(
                    f"Vœrynth Core offline for {elapsed:.0f}s with Observer alive on 4357 "
                    f"(HTTP {observer_status}) — Core stuck, escalating to power cycle "
                    f"| soft_failures={soft_failures} | failure count=0"
                )
                trigger_recovery = True
            elif soft_failures <= SOFT_FAILURE_GRACE:
                # Silent grace period — normal restarts finish well within this window.
                logger.info(
                    f"Vœrynth Core offline ({core_err}), Observer alive — restart grace "
                    f"{soft_failures}/{SOFT_FAILURE_GRACE} ({elapsed:.0f}s elapsed) | failure count=0"
                )
            else:
                # Beyond grace but not yet at the timeout — log a warning.
                logger.warning(
                    f"Vœrynth Core still offline after {soft_failures} checks "
                    f"({elapsed:.0f}s), Observer alive on 4357 "
                    f"(HTTP {observer_status}) — extended restart? "
                    f"| soft_failures={soft_failures} | failure count=0"
                )

            if not trigger_recovery:
                time.sleep(CHECK_INTERVAL)
                continue

        if not trigger_recovery:
            # Hard failure: both Core and Observer are unreachable.
            # Machine is likely frozen — count toward the reboot threshold.
            if should_pause_for_local_network_issue():
                hard_failures = 0
                soft_failures = 0
                soft_failure_start_ts = 0.0
                time.sleep(CHECK_INTERVAL)
                continue
            hard_failures += 1
            soft_failures = 0
            soft_failure_start_ts = 0.0
            logger.warning(
                f"Vœrynth Core failed ({core_err}) and Observer failed ({observer_err}) "
                f"— machine may be frozen | failure count={hard_failures}"
            )
            if hard_failures < HARD_FAILURE_THRESHOLD:
                time.sleep(CHECK_INTERVAL)
                continue

        if len(reboot_times) >= MAX_REBOOTS_PER_HOUR:
            logger.error(
                f"Reboot limit reached ({MAX_REBOOTS_PER_HOUR}/hr). "
                "Skipping power cycle until window clears."
            )
            time.sleep(CHECK_INTERVAL)
            continue

        secs_since_reboot = now - last_reboot_ts
        if secs_since_reboot < COOLDOWN_AFTER_REBOOT:
            remaining = int(COOLDOWN_AFTER_REBOOT - secs_since_reboot)
            logger.error(
                f"Still in cooldown period after previous reboot "
                f"({remaining}s remaining). Skipping power cycle."
            )
            time.sleep(CHECK_INTERVAL)
            continue

        logger.error("Failure threshold reached. Attempting host recovery.")

        success = power_cycle_host()
        if success:
            cycle_ts = time.time()
            reboot_times.append(cycle_ts)
            last_reboot_ts = cycle_ts
            consecutive_reboots += 1
            hard_failures = 0
            soft_failures = 0
            soft_failure_start_ts = 0.0
            intentional_reboot_grace_deadline = 0.0
            intentional_reboot_grace_started_ts = 0.0
            intentional_reboot_outage_start_ts = 0.0

            # Actively monitor recovery for BOOT_GRACE_PERIOD seconds.
            # Check every CHECK_INTERVAL and break early if Core comes back.
            logger.warning(
                f"Power cycle #{consecutive_reboots} complete — monitoring recovery for up to "
                f"{BOOT_GRACE_PERIOD}s..."
            )
            boot_deadline = time.time() + BOOT_GRACE_PERIOD
            host_recovered = False
            while _running and time.time() < boot_deadline:
                time.sleep(CHECK_INTERVAL)
                core_ok, _, _ = ha_core_alive()
                if core_ok:
                    logger.info(
                        "Host recovered within boot grace window — resuming normal monitoring."
                    )
                    host_recovered = True
                    consecutive_reboots = 0
                    break
                remaining = int(boot_deadline - time.time())
                logger.info(f"Boot grace: host still offline ({remaining}s remaining)...")

            if not host_recovered and _running:
                post_reboot_status = verify_post_reboot_and_restore_if_needed(consecutive_reboots)
                if post_reboot_status == "recovered":
                    consecutive_reboots = 0
                    continue
                if post_reboot_status == "restore_started":
                    hard_failures = 0
                    soft_failures = 0
                    soft_failure_start_ts = 0.0
                    consecutive_reboots = 0
                    intentional_reboot_grace_deadline = 0.0
                    intentional_reboot_grace_started_ts = 0.0
                    intentional_reboot_outage_start_ts = 0.0
                    post_restore_grace_deadline = (
                        time.time() + POST_RESTORE_BOOT_GRACE_PERIOD
                    )
                    last_reboot_ts = time.time()
                    continue

                # Do not trigger another power cycle immediately. Let the normal cooldown
                # and main loop govern the second-strike reboot attempt.
                time.sleep(CHECK_INTERVAL)
        else:
            logger.error("Recovery attempt failed. Will retry on future cycles.")
            time.sleep(CHECK_INTERVAL)

    logger.info("Watchdog main loop exited cleanly.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Vœrynth watchdog stopped by user")
    except Exception as exc:
        logger.exception(f"Fatal crash in watchdog: {exc}")
        raise
