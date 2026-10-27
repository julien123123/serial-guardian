"""
Export sessions (and, optionally, labels) as a downloadable file.

Two formats:
  * json -- machine-readable: {"exported_at", "sessions": [...], "labels": [...]}
  * txt  -- human-readable: sessions in order, each label printed where it
            falls in the timeline

Built as a generator so a large export streams out one session at a time
instead of loading every log into the Pi Zero's RAM.

Labels follow the time span of the matching sessions when a status,
selection, or number range is supplied. Both metadata scans and log output stream, so
export memory does not grow with the number of matching sessions.
"""
import datetime
import json

import fields
import labels


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _label_json(l):
    return {"id": l["id"], "ts": l["ts"], "time": _iso(l["ts"]), "text": l["text"]}


def build(monitor, cfg, fmt="json", with_labels=True, with_logs=True,
          status=None, from_id=None, to_id=None, ids=None):
    """Returns (generator_of_str, filename, mimetype).

    Status and inclusive session-number bounds are intersected. None means
    no restriction for that field. Optional ids restricts output to a set of
    explicitly selected sessions without retaining their records in memory.
    """
    extractors = fields.load(cfg)
    all_labels = labels.load(cfg) if with_labels else []

    def matching_records():
        for rec in monitor.iter_records():
            if ids is not None and rec["id"] not in ids:
                continue
            if status is not None and rec["status"] != status:
                continue
            if from_id is not None and rec["id"] < from_id:
                continue
            if to_id is not None and rec["id"] > to_id:
                continue
            yield rec

    chosen_labels = all_labels
    if all_labels and (ids is not None or status is not None or from_id is not None or to_id is not None):
        # First pass reads only index metadata. No list of matching records or
        # raw logs accumulates in memory, even for an export spanning months.
        lo, hi = float("inf"), float("-inf")
        for rec in matching_records():
            lo = min(lo, rec["start"])
            hi = max(hi, rec["start"] + rec["duration"])
        chosen_labels = [l for l in all_labels if lo <= l["ts"] <= hi]
    records = matching_records()

    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d-%H%M%S")

    def entry(rec):
        text = monitor.read_session_text(rec["id"]) if with_logs or extractors else None
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
