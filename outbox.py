"""Local queue of detections waiting to be written to the database.

Every detection lands here first (one JSON object per line), written by the
capture loop with no network involved; the sync thread (uploader.py) drains it
into the database, oldest first. So a network or database outage only grows
the queue, and the queue empties once the connection returns.

The two threads share the file: ``add`` appends under a lock, and ``flush``
reads a snapshot, processes it without holding the lock (so a slow upload never
blocks recording), then removes just the rows it finished. The file is capped
at BW_OUTBOX_MAX_MB; past that the oldest rows are dropped, so a very long
outage can't fill the disk.
"""
import collections
import datetime
import json
import os
import threading

import config

_lock = threading.Lock()


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [line for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _write(path, lines):
    if not lines:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def _enforce_cap(path, max_bytes):
    """Drop the oldest rows until the file is back under 90% of the cap."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
    except OSError:
        return
    lines = _read(path)
    keep, size = [], 0
    for line in reversed(lines):
        size += len(line.encode("utf-8"))
        if size > max_bytes * 0.9:
            break
        keep.append(line)
    keep.reverse()
    _write(path, keep)
    print(f"   (outbox over {max_bytes // (1024 * 1024)} MB: dropped the "
          f"{len(lines) - len(keep)} oldest queued detection(s))")


def add(row, path=None, max_bytes=None):
    """Queue one detection: a dict of ``db.insert_detection`` keyword args,
    plus optional extras the sync thread understands (``upload_clip``)."""
    path = path or config.OUTBOX_PATH
    max_bytes = config.OUTBOX_MAX_MB * 1024 * 1024 if max_bytes is None else max_bytes
    rec = dict(row)
    rec["detected_at"] = rec["detected_at"].isoformat()
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        if max_bytes > 0:
            _enforce_cap(path, max_bytes)


def pending(path=None):
    """True if anything is waiting (a cheap check)."""
    return os.path.exists(path or config.OUTBOX_PATH)


def flush(process, path=None):
    """Hand queued detections to ``process(**row)``, oldest first.

    Stops at the first failure (re-raising it); that row and the rest stay
    queued for next time. Rows added meanwhile are kept. Returns the number
    processed."""
    path = path or config.OUTBOX_PATH
    with _lock:
        lines = _read(path)

    done = []
    try:
        for line in lines:
            try:
                rec = json.loads(line)
                rec["detected_at"] = datetime.datetime.fromisoformat(rec["detected_at"])
            except (ValueError, KeyError, TypeError):
                print(f"   (outbox: skipping unreadable line {line[:80]!r})")
            else:
                process(**rec)
            done.append(line)
    finally:
        if done:
            # Remove exactly the rows processed. Match by content rather than
            # position: the cap may have trimmed the head meanwhile.
            with _lock:
                todo = collections.Counter(done)
                rest = []
                for line in _read(path):
                    if todo[line] > 0:
                        todo[line] -= 1
                    else:
                        rest.append(line)
                _write(path, rest)
    return len(done)
