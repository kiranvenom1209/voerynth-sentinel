#!/usr/bin/env python3

import os
from pathlib import Path


def _is_truthy(value) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _iter_local_config_paths():
    seen = set()
    for path in (Path(__file__).resolve().parent / "config.env", Path.cwd() / "config.env"):
        key = str(path.absolute())
        if key not in seen:
            seen.add(key)
            yield path


def _parse_env_assignment(line: str):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    name, value = stripped.split("=", 1)
    name = name.strip()
    if not name:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return name, value


def _load_local_config() -> None:
    if _is_truthy(os.getenv("HA_WATCHDOG_DISABLE_LOCAL_CONFIG")):
        return
    for path in _iter_local_config_paths():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            parsed = _parse_env_assignment(line)
            if parsed is None:
                continue
            name, value = parsed
            os.environ.setdefault(name, value)
        return


_load_local_config()


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
NETWORK_SANITY_CHECK_HOST = _env_str("NETWORK_SANITY_CHECK_HOST")
NETWORK_SANITY_CHECK_TIMEOUT = _env_int("NETWORK_SANITY_CHECK_TIMEOUT", 1)

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
HA_SSH_HOST_KEY = _env_str("HA_SSH_HOST_KEY")
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