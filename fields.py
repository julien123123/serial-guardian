"""
Configurable "extra column" extraction for the Sessions page.

Each extractor is a {name, pattern} pair where `pattern` is a regex with
exactly one capture group, e.g.:

    {"name": "REFRESH",     "pattern": r"REFRESH:\\s*(\\d+)"}
    {"name": "reset_cause", "pattern": r"reset cause = (\\d+)"}

Deliberately NOT baked into a session's index.jsonl record at write time.
Instead, extract() is called at render time against the session's raw log
text (see monitor.read_session_text). That means editing the extractor
list while you're mid-debug immediately applies to sessions already on
disk too, not just ones from that point forward -- which is the whole
point of making this runtime-configurable.

Config lives in DATA_DIR/field_config.json so it survives service
restarts but needs no code deploy to change; edit it from the web UI
(Settings page) or by hand.
"""
import json
import os
import re

DEFAULT_EXTRACTORS = [
    {"name": "REFRESH", "pattern": r"REFRESH:\s*(\d+)"},
    {"name": "reset_cause", "pattern": r"reset cause = (\d+)"},
    {"name": "wake_pins", "pattern": r"wake up pins \[([^\]]*)\]"},
]


def _path(cfg):
    return os.path.join(cfg.DATA_DIR, "field_config.json")


def load(cfg):
    p = _path(cfg)
    if not os.path.exists(p):
        return [dict(e) for e in DEFAULT_EXTRACTORS]
    try:
        with open(p) as f:
            data = json.load(f)
        extractors = [e for e in data.get("extractors", []) if _valid(e)]
        return extractors
    except (json.JSONDecodeError, OSError):
        # A hand-edited or corrupt file shouldn't take the Sessions page down.
        return [dict(e) for e in DEFAULT_EXTRACTORS]


def save(cfg, extractors):
    """Validate and persist a new extractor list. Returns a list of error
    strings; on success that list is empty and the file has been written
    (all-or-nothing -- a bad entry blocks the whole save)."""
    errors = []
    cleaned = []
    seen_names = set()
    for e in extractors:
        name = (e.get("name") or "").strip()
        pattern = (e.get("pattern") or "").strip()
        if not name and not pattern:
            continue  # silently drop fully-blank rows from the editor
        if not name or not pattern:
            errors.append(f"'{name or pattern}': needs both a column name and a pattern")
            continue
        if name in seen_names:
            errors.append(f"'{name}': duplicate column name")
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            errors.append(f"'{name}': invalid regex ({exc})")
            continue
        if compiled.groups != 1:
            errors.append(f"'{name}': pattern must have exactly one capture group, has {compiled.groups}")
            continue
        seen_names.add(name)
        cleaned.append({"name": name, "pattern": pattern})

    if errors:
        return errors

    p = _path(cfg)
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"extractors": cleaned}, f, indent=2)
    os.replace(tmp, p)
    return []


def _valid(e):
    try:
        name, pattern = e.get("name"), e.get("pattern")
        if not name or not pattern:
            return False
        return re.compile(pattern).groups == 1
    except Exception:
        return False


def extract(text, extractors):
    """Return {name: value_or_None} for one session's raw log text.
    `text` may be None (log was pruned) -- every value comes back None."""
    out = {}
    for e in extractors:
        value = None
        if text:
            try:
                m = re.search(e["pattern"], text)
                if m:
                    value = m.group(1)
            except re.error:
                pass
        out[e["name"]] = value
    return out
