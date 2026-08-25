"""
Runtime-editable overrides for config.py, exposed on the Settings page.

config.py stays the source of defaults, plus a handful of process-bootstrap
values that can't sensibly change without a restart -- see NOT_EDITABLE.
Everything else here can be tuned from the browser and takes effect
immediately: apply_overrides() mutates the shared config module's
attributes in place, and monitor.py / webapp.py already read cfg.SOMETHING
fresh at the point of use rather than caching it at startup, so the new
value is picked up automatically without any other code changes.

Overrides persist in DATA_DIR/settings.json.
"""
import json
import os

# (key, kind, label, help) -- kind is one of str / int / float / bool / list
FIELD_SPECS = [
    ("SERIAL_PORT", "str", "Serial port",
     "Device path for the MicroPython board. Takes effect on the next (re)connect."),
    ("BAUD", "int", "Baud rate",
     "Serial baud rate. Takes effect on the next (re)connect."),
    ("SERIAL_READ_TIMEOUT", "float", "Read timeout (s)",
     "How long to block waiting for at least one byte when idle. Takes effect on the next (re)connect."),
    ("GOTOSLEEP_MARKER", "str", "Clean-shutdown marker",
     "A session containing this text before it disconnects is tagged NORMAL."),
    ("BOOT_BANNER_MARKER", "str", "Boot banner marker",
     "Printed on a fresh boot; used to detect that a reset actually took effect."),
    ("EXCEPTION_MARKERS", "list", "Exception markers",
     "One per line. Any of these appearing flags a session EXCEPTION and triggers auto-recovery."),
    ("SOFT_RESET_DELAY", "float", "Soft-reset delay (s)",
     "Wait this long after an exception marker before sending the soft-reset keystrokes."),
    ("BOOT_TIMEOUT", "float", "Boot timeout (s)",
     "If no fresh boot banner shows up this long after a soft reset, escalate to hardware reset."),
    ("STOP_SEND_DELAY", "float", "Stop settle delay (s)",
     "Settle time after a fresh connect before sending an armed 'stop on next update'."),
    ("ENABLE_GPIO_RESET", "bool", "Enable GPIO hardware reset",
     "Requires wiring, see README. Only ever used as an automatic-recovery fallback, never for manual Stop/Resume."),
    ("GPIO_RESET_PIN", "int", "GPIO reset pin (BCM)",
     "Which GPIO pin is wired to the board's RESET line."),
    ("GPIO_RESET_PULSE", "float", "GPIO reset pulse (s)",
     "How long to hold RESET low."),
    ("NORMAL_LOG_RETENTION", "int", "Normal log retention",
     "How many NORMAL session logs to keep on disk (0 disables pruning). EXCEPTION/ANOMALY/STOPPED are always kept."),
    ("LIVE_TAIL_LINES", "int", "Live tail buffer",
     "How many lines the Live page's scrolling view keeps in memory."),
    ("MONITORING_PAUSE_POLL_INTERVAL", "float", "Pause poll interval (s)",
     "How often the paused monitor checks whether it's been resumed."),
    ("MIN_FREE_DISK_MB", "int", "Minimum free disk (MB)",
     "Below this, new sessions skip writing their raw log text (the index entry is still recorded) rather than risking a write failure."),
]

# Shown on the Settings page as read-only, with the reason why.
NOT_EDITABLE = [
    ("DATA_DIR", "Where this Settings page's own override file lives -- changing it live would strand it."),
    ("WEB_HOST", "The web server is already bound at startup; this can't rebind it."),
    ("WEB_PORT", "Same as above -- this page is being served on the current port right now."),
    ("SERVICE_NAME", "Tied to the actual systemd unit filename on disk."),
    ("SOFT_RESET_BYTES", "Raw control-byte sequence -- edit config.py directly if you need to change it."),
    ("STOP_BYTES", "Raw control-byte sequence -- edit config.py directly if you need to change it."),
]

_VALID_KEYS = {k for k, *_ in FIELD_SPECS}


def _path(cfg):
    return os.path.join(cfg.DATA_DIR, "settings.json")


def _cast(kind, raw):
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(str(raw).strip())
    if kind == "float":
        return float(str(raw).strip())
    if kind == "list":
        if isinstance(raw, list):
            items = raw
        else:
            items = str(raw).splitlines()
        return [s.strip() for s in items if s.strip()]
    return str(raw).strip()


def load_overrides(cfg):
    p = _path(cfg)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in _VALID_KEYS}
    except (json.JSONDecodeError, OSError):
        # A hand-edited or corrupt override file shouldn't break startup.
        return {}


def apply_overrides(cfg):
    """Mutate the live config module with whatever's saved on disk. Safe
    to call repeatedly (e.g. once at startup, again after every save)."""
    for key, value in load_overrides(cfg).items():
        setattr(cfg, key, value)


def current_values(cfg):
    return {key: getattr(cfg, key) for key, *_ in FIELD_SPECS}


def save(cfg, submitted):
    """submitted: dict of key -> raw form value (str/bool/list depending
    on field kind). Validates, persists to settings.json, and applies
    immediately. Returns a list of error strings; empty means success."""
    errors = []
    cleaned = {}
    for key, kind, label, _help in FIELD_SPECS:
        if key not in submitted:
            if kind == "bool":
                cleaned[key] = False  # an unchecked box; still a valid, deliberate value
                continue
            errors.append(f"{label}: missing")
            continue
        try:
            value = _cast(kind, submitted[key])
        except (TypeError, ValueError):
            errors.append(f"{label}: must be a {kind}")
            continue

        if kind in ("int", "float") and key != "GPIO_RESET_PIN" and value < 0:
            errors.append(f"{label}: must not be negative")
            continue
        if kind == "str" and not value:
            errors.append(f"{label}: can't be blank")
            continue
        if kind == "list" and not value:
            errors.append(f"{label}: needs at least one entry")
            continue

        cleaned[key] = value

    if errors:
        return errors

    p = _path(cfg)
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cleaned, f, indent=2)
    os.replace(tmp, p)

    apply_overrides(cfg)
    return []
