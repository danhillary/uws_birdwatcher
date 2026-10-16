"""Background sync: the only part of the listener that waits on the network.

The capture loop (capture/listen.py) records, runs BirdNET and saves every
detection locally: a row in the outbox and its clip in CLIPS_DIR. This thread
drains that backlog, oldest first: it uploads each bird clip to S3, writes the
row to the database, then deletes the local clip. It also does the database
setup, writes the dashboard heartbeat, publishes the phone-widget feed and
clears clip_path for clips the disk cap pruned.

A dead network or a stuck database only ever stalls this thread, so recording
never stops (it did on Oct 15, when the capture loop sat behind a database
lock for over two hours). Detections keep their recording time, so the
dashboard fills in correctly once the backlog drains.
"""
import os
import socket
import threading
import time

import clips_s3
import config
import db
import feed
import outbox

# Written by the capture loop, read here for the dashboard heartbeat.
state = {"peak": 0.0, "last_det": None}

_wake = threading.Event()
_db_retry_at = 0.0
_last_db_ok = time.monotonic()  # startup counts as healthy until proven otherwise
_pruned = []                    # pruned clip files whose rows need clip_path cleared
_pruned_lock = threading.Lock()


def wake():
    """Nudge the sync thread (a new detection is queued)."""
    _wake.set()


def healthy():
    """True while sync is keeping up: the database answered within
    BW_SYNC_STALE seconds. The capture loop sends health pings only while this
    holds, so a sync that stays broken raises an alert even though recording
    carries on."""
    return time.monotonic() - _last_db_ok < config.SYNC_STALE_SECONDS


def forget_clips(names):
    """Clips the disk cap deleted; their rows' clip_path is cleared on sync."""
    with _pruned_lock:
        _pruned.extend(names)


def db_call(what, fn, **kwargs):
    """Run a database call; True if it succeeded. Never raises. After a failure
    the database is skipped for BW_DB_RETRY seconds."""
    global _db_retry_at, _last_db_ok
    if time.monotonic() < _db_retry_at:
        return False
    try:
        fn(**kwargs)
    except Exception as e:
        msg = str(e).strip()
        first = msg.splitlines()[0] if msg else type(e).__name__
        print(f"   ({what} failed: {first}; retrying in {config.DB_RETRY_SECONDS}s)")
        _db_retry_at = time.monotonic() + config.DB_RETRY_SECONDS
        return False
    _last_db_ok = time.monotonic()
    return True


def sync_row(row, insert, put, uploads_on, now):
    """Sync one queued detection: upload its clip if it should go to S3, insert
    the row, then delete the uploaded clip locally. Raises to stop the pass;
    the row stays queued and is retried later.

    ``row`` holds insert_detection's arguments plus ``upload_clip`` (False for
    human-voice holds, which never leave this machine)."""
    row = dict(row)
    upload = row.pop("upload_clip", False)
    name = row.get("clip_path")
    path = os.path.join(config.CLIPS_DIR, name) if name else None
    if path and not os.path.exists(path):
        print(f"   (sync: clip {name} was pruned before upload; "
              f"saving the detection without audio)")
        row["clip_path"] = path = None

    url = None
    if upload and path and uploads_on:
        with open(path, "rb") as f:
            data = f.read()
        try:
            url = put(name, data)
        except Exception as e:
            waited = (now - row["detected_at"]).total_seconds()
            if waited < config.CLIP_UPLOAD_GIVE_UP_SECONDS:
                raise
            print(f"   (clip upload for {name} still failing after "
                  f"{waited / 60:.0f} min ({e}); saving the detection with the "
                  f"clip kept locally for `python -m backfill_clips`)")
    if url:
        row["clip_url"], row["clip_path"] = url, None

    insert(**row)

    if url:
        try:
            os.remove(path)
        except OSError:
            pass


def _sync_outbox():
    n = outbox.flush(lambda **row: sync_row(
        row, insert=db.insert_detection, put=clips_s3.put,
        uploads_on=clips_s3.enabled(), now=config.now_local()))
    if n > 1:
        print(f"   (synced {n} queued detections)")


def _pass(host, ready):
    if not ready:
        ready = db_call("database setup", db.init_db)
    if ready:
        # Heartbeat first: it's what tells the dashboard the listener is alive.
        db_call("heartbeat write", db.record_heartbeat, host=host,
                last_peak=state["peak"], last_detection_at=state["last_det"])
        if outbox.pending():
            db_call("sync", _sync_outbox)
        with _pruned_lock:
            names = _pruned[:]
            del _pruned[:]
        if names and not db_call("clip cleanup", db.clear_clip_paths, filenames=names):
            forget_clips(names)  # try again next pass
    feed.publish_safe()  # throttled; swallows its own errors
    return ready


def _loop(host):
    ready = False
    while True:
        _wake.wait(timeout=15)
        _wake.clear()
        try:
            ready = _pass(host, ready)
        except Exception as e:  # never let the sync thread die
            print(f"   (sync error: {e!r})")
            time.sleep(5)


def start():
    """Start the sync thread (a daemon, so it ends with the listener)."""
    thread = threading.Thread(target=_loop, args=(socket.gethostname(),),
                              name="sync", daemon=True)
    thread.start()
    return thread
