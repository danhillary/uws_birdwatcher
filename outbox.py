"""Local outbox for detections the database couldn't take.

When the network or database is down, the listener appends each detection here
(one JSON object per line) instead of losing it, and replays the file into the
database once it is reachable again. Clips are unaffected: a clip whose S3
upload fails already falls back to local disk (see capture.listen).
"""
import datetime
import json
import os

import config


def add(row, path=None):
    """Queue one detection: a dict of ``db.insert_detection`` keyword args."""
    rec = dict(row)
    rec["detected_at"] = rec["detected_at"].isoformat()
    with open(path or config.OUTBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def pending(path=None):
    """True if anything is waiting (a cheap check for every capture loop)."""
    return os.path.exists(path or config.OUTBOX_PATH)


def flush(insert, path=None):
    """Replay queued detections through ``insert(**row)``, oldest first.

    Stops at the first failure (re-raising it) and keeps that row and the rest
    for next time. Returns the number written."""
    path = path or config.OUTBOX_PATH
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    done = 0
    try:
        for line in lines:
            try:
                rec = json.loads(line)
                rec["detected_at"] = datetime.datetime.fromisoformat(rec["detected_at"])
            except (ValueError, KeyError, TypeError):
                print(f"   (outbox: skipping unreadable line {line[:80]!r})")
            else:
                insert(**rec)
            done += 1
    finally:
        rest = lines[done:]
        if rest:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rest)
            os.replace(tmp, path)
        else:
            os.remove(path)
    return done
