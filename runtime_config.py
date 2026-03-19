#!/usr/bin/env python3

import os


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


HA_HOST = _env_str("HA_HOST", "homeassistant.local")
HA_CORE_URL = f"http://{HA_HOST}:8123/api/"
HA_OBSERVER_URL = f"http://{HA_HOST}:4357"

CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 10)
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 4)
SSH_INVESTIGATION_TIMEOUT = _env_int("SSH_INVESTIGATION_TIMEOUT", 5)

HARD_FAILURE_THRESHOLD = _env_int("HARD_FAILURE_THRESHOLD", 3)
SOFT_FAILURE_GRACE = _env_int("SOFT_FAILURE_GRACE", 2)
SOFT_FAILURE_TIMEOUT = _env_int("SOFT_FAILURE_TIMEOUT", 120)

BOOT_GRACE_PERIOD = _env_int("BOOT_GRACE_PERIOD", 180)
STARTUP_GRACE_PERIOD = _env_int("STARTUP_GRACE_PERIOD", 120)
INTENTIONAL_REBOOT_GRACE_PERIOD = _env_int("INTENTIONAL_REBOOT_GRACE_PERIOD", 300)
POST_RESTORE_BOOT_GRACE_PERIOD = _env_int("POST_RESTORE_BOOT_GRACE_PERIOD", 400)
POWER_OFF_SECONDS = _env_int("POWER_OFF_SECONDS", 12)

MAX_REBOOTS_PER_HOUR = _env_int("MAX_REBOOTS_PER_HOUR", 3)
REBOOT_WINDOW_SECONDS = _env_int("REBOOT_WINDOW_SECONDS", 3600)
COOLDOWN_AFTER_REBOOT = _env_int("COOLDOWN_AFTER_REBOOT", 300)

TUYA_DEVICE_ID = _env_str("TUYA_DEVICE_ID")
TUYA_DEVICE_IP = _env_str("TUYA_DEVICE_IP")
TUYA_LOCAL_KEY = _env_str("TUYA_LOCAL_KEY")
TUYA_VERSION = _env_float("TUYA_VERSION", 3.4)
HA_SSH_USER = _env_str("HA_SSH_USER", "ha")
HA_SSH_PORT = _env_int("HA_SSH_PORT", 22)
BACKUP_PASS = _env_str("BACKUP_PASS")
PREFERRED_RESTORE_LOCATION = _env_str("PREFERRED_RESTORE_LOCATION", "Local_NAS")

DRY_RUN = _env_bool("DRY_RUN", False)

NABU_CASA_URL = _env_str("NABU_CASA_URL")
NABU_CASA_TIMEOUT = _env_int("NABU_CASA_TIMEOUT", 8)
ENABLE_REMOTE_CHECK = _env_bool("ENABLE_REMOTE_CHECK", bool(NABU_CASA_URL))

BIND_HOST = _env_str("BIND_HOST", "0.0.0.0")
DASHBOARD_HOST = _env_str("DASHBOARD_HOST", "127.0.0.1")
PORT = _env_int("PORT", 8080)


def require_settings(*names: str, **named_values):
    missing = [name for name in names if not _has_value(os.getenv(name))]
    missing.extend(name for name, value in named_values.items() if not _has_value(value))
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            f"Missing required configuration: {missing_list}. "
            "Set these in the environment or config.env before running the service."
        )