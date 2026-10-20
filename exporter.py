"""
Export sessions (and, optionally, labels) as a downloadable file.

Two formats:
  * json -- machine-readable: {"exported_at", "sessions": [...], "labels": [...]}
  * txt  -- human-readable: sessions in order, each label printed where it
            falls in the timeline

Built as a generator so a large export streams out one session at a time
instead of loading every log into the Pi Zero's RAM.

Labels: with "all sessions", every label is exported. With a hand-picked
set of sessions, only labels dated within that set's time span (earliest
start to latest end) are -- a note from months before the exported
sessions would just be noise.
"""
import datetime
import json

import fields
import labels


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _label_json(l):
    return {"id": l["id"], "ts": l["ts"], "time": _iso(l["ts"]), "text": l["text"]}


def build(monitor, cfg, ids=None, fmt="json", with_labels=True, with_logs=True):
    """Returns (generator_of_str, filename, mimetype).

    ids: set of session ids to export, or None for every session in the index.
    """
    extractors = fields.load(cfg)
    all_labels = labels.load(cfg) if with_labels else []

    if ids is None:
        records = monitor.iter_records()          # streamed, never held in memory
        chosen_labels = all_labels
    else:
        recs = [r for r in monitor.iter_records() if r["id"] in ids]
        records = iter(recs)
        if recs:
            lo = min(r["start"] for r in recs)
            hi = max(r["start"] + r["duration"] for r in recs)
            chosen_labels = [l for l in all_labels if lo <= l["ts"] <= hi]
        else:
            chosen_labels = []

    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d-%H%M%S")

    def entry(rec):
        text = monitor.read_session_text(rec["id"])   # None if pruned
        e = {
            "id": rec["id"],
            "status": rec["status"],
            "start": rec["start"],
            "start_time": _iso(rec["start"]),
            "duration": rec["duration"],
            "lines": rec["lines"],
            "fields": fields.extract(text, extractors),
        }
        if with_logs:
            e["log"] = text
        return e, text

    def gen_json():
        yield '{\n"exported_at": %s,\n"sessions": [' % json.dumps(now.isoformat(timespec="seconds"))
        first = True
        for rec in records:
            e, _ = entry(rec)
            yield ("" if first else ",") + "\n" + json.dumps(e)
            first = False
        yield "\n]"
        if with_labels:
            yield ',\n"labels": ' + json.dumps([_label_json(l) for l in chosen_labels], indent=1)
        yield "\n}\n"

    def label_line(l):
        return f"---- LABEL [{_iso(l['ts'])}] {l['text']} ----\n\n"

    def gen_txt():
        yield f"Serial Guardian export, {now.isoformat(timespec='seconds')}\n\n"
        pending = list(chosen_labels)    # oldest first
        for rec in records:
            while pending and pending[0]["ts"] <= rec["start"]:
                yield label_line(pending.pop(0))
            e, text = entry(rec)
            yield (f"==== #{rec['id']} {rec['status']} | {e['start_time']} | "
                   f"{rec['duration']}s | {rec['lines']} lines ====\n")
            if with_logs:
                if text is None:
                    yield "(raw log not available -- pruned)\n"
                else:
                    yield text if text.endswith("\n") else text + "\n"
            yield "\n"
        for l in pending:
            yield label_line(l)

    if fmt == "txt":
        return gen_txt(), f"serial-guardian-export-{stamp}.txt", "text/plain; charset=utf-8"
    return gen_json(), f"serial-guardian-export-{stamp}.json", "application/json; charset=utf-8"
