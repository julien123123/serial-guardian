"""
Free-standing labels (timeline notes) for the Sessions page.

A label is a short note you drop into the session log -- "swapped the
battery", "firmware v2 flashed" -- pinned to the moment you added it.
Labels are NOT attached to a session row; they sit between sessions at
their timestamp, so they show up in the same place in the log whatever
status filter is active (see the /sessions route in webapp.py).

Stored in DATA_DIR/labels.json rather than inside index.jsonl: the index is
append-only and never rewritten, while labels need to be edited and deleted.
"""
import json
import os
import threading
import time

MAX_LEN = 200

_lock = threading.Lock()


def _path(cfg):
    return os.path.join(cfg.DATA_DIR, "labels.json")


def _read(cfg):
    try:
        with open(_path(cfg)) as f:
            data = json.load(f)
        return [
            l for l in data.get("labels", [])
            if isinstance(l, dict)
            and isinstance(l.get("id"), int)
            and isinstance(l.get("ts"), (int, float))
            and isinstance(l.get("text"), str)
        ]
    except (OSError, json.JSONDecodeError, AttributeError):
        # Missing file, or a hand-edited/corrupt one -- don't take the page down.
        return []


def _write(cfg, items):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    p = _path(cfg)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"labels": items}, f, indent=2)
    os.replace(tmp, p)


def _clean(text):
    text = " ".join(str(text or "").split())   # one line, collapsed whitespace
    if not text:
        return None, "label text is empty"
    if len(text) > MAX_LEN:
        return None, f"label is too long (max {MAX_LEN} characters)"
    return text, None


def load(cfg):
    """All labels, oldest first."""
    with _lock:
        items = _read(cfg)
    return sorted(items, key=lambda l: l["ts"])


def add(cfg, text, ts=None):
    """Returns (label, error). Exactly one of the two is None."""
    text, err = _clean(text)
    if err:
        return None, err
    with _lock:
        items = _read(cfg)
        label = {
            "id": max((l["id"] for l in items), default=0) + 1,
            "ts": round(ts or time.time(), 3),
            "text": text,
        }
        items.append(label)
        try:
            _write(cfg, items)
        except OSError as exc:
            return None, f"could not save label: {exc}"
    return label, None


def update(cfg, lid, text):
    """Change a label's text (its position in time stays put)."""
    text, err = _clean(text)
    if err:
        return None, err
    with _lock:
        items = _read(cfg)
        for l in items:
            if l["id"] == lid:
                l["text"] = text
                try:
                    _write(cfg, items)
                except OSError as exc:
                    return None, f"could not save label: {exc}"
                return l, None
    return None, "no such label"


def delete(cfg, lid):
    """Returns an error string, or None on success."""
    with _lock:
        items = _read(cfg)
        kept = [l for l in items if l["id"] != lid]
        if len(kept) == len(items):
            return "no such label"
        try:
            _write(cfg, kept)
        except OSError as exc:
            return f"could not save labels: {exc}"
    return None


def clear(cfg):
    """Remove every label (used by 'erase all sessions')."""
    with _lock:
        try:
            os.remove(_path(cfg))
        except FileNotFoundError:
            pass
