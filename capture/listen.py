"""Listen on the microphone, identify birds with BirdNET, and record them.

Captures fixed-length audio segments, runs BirdNET on each (filtered to species
likely at your location/date), then for every detection: prints it, saves a
short audio clip, and stores a row in the database. Old clips are pruned to stay
under the configured storage cap. A background thread recomputes the
false-positive filter flags every BW_FILTER_REFRESH seconds.

Faults that used to stop it are survived: a database or network outage queues
detections in a local outbox until the database is back, and a mic that goes
silent or errors is reopened (PortAudio restarted, device found again). If the
mic stays dead for BW_MIC_GIVE_UP seconds it exits with code 3 so the watchdog
(capture/watchdog.py) can start a fresh process.

Run with:  python -m capture.listen   (or supervised: python -m capture.watchdog)
Stop with: Ctrl-C
"""
import os
import socket
import sys
import time

import numpy as np
import sounddevice as sd

import clips_s3
import config
import db
import feed
import filter_refresh
import healthcheck
import outbox
import storage
from analysis import BirdAnalyzer, dedupe_per_species, partition_detections
from capture.recovery import EXIT_MIC, MicGuard


def resolve_device(spec):
    """Resolve BW_INPUT_DEVICE (an index, a name substring, or None) to a
    concrete input-device index. Raises a clear error if nothing matches."""
    devices = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]

    if spec is None or spec == "":
        return sd.default.device[0]

    # Numeric index?
    if str(spec).isdigit():
        idx = int(spec)
        info = sd.query_devices(idx)
        if info["max_input_channels"] < 1:
            names = ", ".join(f"[{i}] {d['name']}" for i, d in inputs)
            raise ValueError(
                f"Device [{idx}] '{info['name']}' has no input channels. "
                f"Pick an input device instead — {names}"
            )
        return idx

    # Name substring (case-insensitive).
    spec_l = str(spec).lower()
    for i, d in inputs:
        if spec_l in d["name"].lower():
            return i
    names = ", ".join(f"[{i}] {d['name']}" for i, d in inputs)
    raise ValueError(f"No input device matches '{spec}'. Available: {names}")


def record_segment(device, channels):
    """Record one segment of mono audio and return it as a float32 numpy array."""
    frames = int(config.SEGMENT_SECONDS * config.SAMPLE_RATE)
    audio = sd.rec(
        frames,
        samplerate=config.SAMPLE_RATE,
        channels=channels,
        dtype="float32",
        device=device,
    )
    # sd.wait() can block forever when the device vanishes mid-recording (USB
    # dropout, driver reset), so poll against a deadline and treat an overrun
    # as a mic fault.
    deadline = time.monotonic() + config.SEGMENT_SECONDS * 2 + 5
    while sd.get_stream().active:
        if time.monotonic() > deadline:
            sd.stop()
            raise RuntimeError("recording did not finish; the device stopped responding")
        time.sleep(0.05)
    sd.wait()  # returns at once now; re-raises any error from the stream
    # If the device gave us multiple channels, collapse to mono.
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    return audio.reshape(-1)


def open_mic():
    """Find the configured mic. Returns (device, channels, name)."""
    device = resolve_device(config.INPUT_DEVICE)
    info = sd.query_devices(device)
    return device, min(config.CHANNELS, info["max_input_channels"]), info["name"]


def reopen_mic():
    """Restart PortAudio and find the mic again. After a USB dropout or driver
    reset Windows keeps handing back a stale device (silence or errors, often
    under a new index) until PortAudio rescans, which this does without
    restarting the process."""
    try:
        sd.stop()
    except Exception:
        pass
    sd._terminate()
    sd._initialize()
    return open_mic()


_db_retry_at = 0.0


def db_call(what, fn, **kwargs):
    """Run a database call from the capture loop; True if it succeeded.

    Never raises. After a failure the database is skipped for BW_DB_RETRY
    seconds, so a dead connection costs one connect timeout a minute rather
    than one per segment."""
    global _db_retry_at
    if time.monotonic() < _db_retry_at:
        return False
    try:
        fn(**kwargs)
        return True
    except Exception as e:
        msg = str(e).strip()
        first = msg.splitlines()[0] if msg else type(e).__name__
        print(f"   ({what} failed: {first}; retrying in {config.DB_RETRY_SECONDS}s)")
        _db_retry_at = time.monotonic() + config.DB_RETRY_SECONDS
        return False


def touch_heartbeat():
    """Tell the watchdog we're still making progress (a local file, no network)."""
    try:
        with open(config.HEARTBEAT_PATH, "w") as f:
            f.write(f"{time.time():.0f}\n")
    except OSError:
        pass


_instance_lock = None


def take_instance_lock():
    """Hold an exclusive lock for this process's lifetime so a second listener
    (a stray manual run, a double-click on run_listener.vbs) can't record the
    same birds twice. Returns False if another listener holds it."""
    global _instance_lock
    f = open(os.path.join(config.BASE_DIR, "listener.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance_lock = f
    return True


def _force_utf8_output():
    """Windows consoles default to cp1252, which can't encode the 🐦 emoji or
    accented species names — printing a detection there raises
    UnicodeEncodeError and kills the listener. Force UTF-8 (replacing anything
    unencodable) so output never crashes the capture loop."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # already UTF-8, or a stream without reconfigure()


def main():
    _force_utf8_output()
    if not take_instance_lock():
        print("Another listener is already running on this machine; exiting.")
        return 2
    try:
        device, channels, mic_name = open_mic()
    except ValueError as e:
        print(f"Audio device error: {e}")
        return 1

    # An unreachable database no longer stops the listener: detections queue
    # in the outbox and setup is retried every loop until it succeeds.
    db_ready = db_call("database setup", db.init_db)
    storage.ensure_dirs()

    print("Loading BirdNET model (first run downloads/initialises, ~10-20s)...")
    analyzer = BirdAnalyzer()

    refresh_txt = (f"refreshed every {config.FILTER_REFRESH_SECONDS}s"
                   if config.FILTER_REFRESH_SECONDS > 0 else "refresh off")
    print(
        f"Listening on '{mic_name}' "
        f"@ {config.SAMPLE_RATE} Hz, {config.SEGMENT_SECONDS}s segments.\n"
        f"Location: {config.LATITUDE}, {config.LONGITUDE} | "
        f"min confidence: {config.MIN_CONFIDENCE}\n"
        f"Saving to: {config.DB_HOST}/{config.DB_NAME} (schema {config.DB_SCHEMA})\n"
        f"Clip uploads: {'on' if clips_s3.enabled() else 'off'} | "
        f"human-voice filter: {'on' if config.FILTER_HUMAN_VOICE else 'off'}\n"
        f"False-positive flags: {refresh_txt}\n"
        f"Health pings: {'on' if config.HEALTHCHECK_URL else 'off'}\n"
        f"Press Ctrl-C to stop.\n"
    )

    # Recompute the filter flags off the capture thread, so a slow database
    # round trip never costs recorded audio.
    filter_refresh.start_background()

    host = socket.gethostname()
    last_det = None  # time of the most recent detection, for the heartbeat
    mic = MicGuard(config.MIC_REOPEN_AFTER, config.MIC_GIVE_UP_SECONDS)

    def replay_outbox():
        n = outbox.flush(db.insert_detection)
        print(f"   (database back: wrote {n} queued detection(s))")

    try:
        while True:
            try:
                audio = record_segment(device, channels)
                peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            except Exception as e:  # PortAudioError, a vanished device, a hang
                print(f"[{config.now_local():%H:%M:%S}] (mic error: {e})")
                audio, peak = None, 0.0
            touch_heartbeat()

            if not db_ready:
                db_ready = db_call("database setup", db.init_db)
            if db_ready and outbox.pending():
                db_call("outbox replay", replay_outbox)

            # Heartbeat: let the dashboard see we're alive and what the mic is
            # hearing, even during long silent stretches.
            if db_ready:
                db_call("heartbeat write", db.record_heartbeat,
                        host=host, last_peak=peak, last_detection_at=last_det)

            # Refresh the public widget feed (throttled, no-op if disabled).
            feed.publish_safe()

            action = mic.check(peak >= 1e-4)
            if action == "ok":
                healthcheck.ok_safe()
            else:
                if audio is not None:
                    print(f"[{config.now_local():%H:%M:%S}] (silence — check the mic)")
                if action == "reopen":
                    try:
                        device, channels, mic_name = reopen_mic()
                        print(f"   (reopened the mic: '{mic_name}')")
                    except Exception as e:
                        print(f"   (mic reopen failed: {e})")
                        time.sleep(5)  # e.g. unplugged; don't spin
                elif action == "give_up":
                    print(f"   (mic still silent or failing after "
                          f"{config.MIC_GIVE_UP_SECONDS}s of reopening; exiting "
                          f"so the watchdog can restart the listener)")
                    return EXIT_MIC
                elif audio is None:
                    time.sleep(2)  # recording failed instantly; don't spin
                continue

            when = config.now_local()
            birds, has_voice = partition_detections(
                analyzer.analyze(audio, config.SAMPLE_RATE, when=when)
            )
            detections = dedupe_per_species(birds)

            stamp = f"{when:%H:%M:%S}"
            if has_voice:
                # Privacy hold: a human voice is in this segment, so its clips
                # stay on this machine and are never uploaded to the public S3.
                print(f"[{stamp}]    (human voice heard — clips kept local only)")
            if not detections:
                print(f"[{stamp}] ... no birds detected")
                continue

            for d in detections:
                if has_voice:
                    # Privacy hold: keep this clip on the recording machine only,
                    # never on the public bucket.
                    clip = storage.save_clip(
                        audio, config.SAMPLE_RATE, when,
                        d["common_name"], d["start_time"], d["end_time"],
                    )
                    clip_url = None
                else:
                    # Bird clips live in S3 only — encode in memory and upload,
                    # writing local disk only if the upload fails, so the clip
                    # isn't lost and the backfill tool can retry it later.
                    filename = storage.clip_filename(when, d["common_name"])
                    data = storage.encode_clip(
                        audio, config.SAMPLE_RATE,
                        d["start_time"], d["end_time"],
                    )
                    clip_url = clips_s3.upload_bytes(filename, data)
                    clip = None if clip_url else storage.save_clip(
                        audio, config.SAMPLE_RATE, when,
                        d["common_name"], d["start_time"], d["end_time"],
                    )
                row = dict(
                    detected_at=when, common_name=d["common_name"],
                    scientific_name=d["scientific_name"],
                    confidence=float(d["confidence"]),
                    lat=config.LATITUDE, lon=config.LONGITUDE,
                    clip_path=clip, clip_url=clip_url,
                )
                # Database down? Queue it locally instead of losing it (this
                # insert, unguarded, is what crashed the listener on Oct 13).
                if not (db_ready and db_call("detection write",
                                             db.insert_detection, **row)):
                    outbox.add(row)
                print(
                    f"[{stamp}] \U0001F426 {d['common_name']} "
                    f"({d['scientific_name']}) — {d['confidence']:.0%}  "
                    f"-> {clip_url or clip or 'S3'}"
                )
                last_det = when

            # Keep the *local* clip cache (human-voice holds + any upload
            # fallbacks) under the cap, and forget pruned files in the DB. S3
            # copies are permanent and never pruned.
            removed = storage.prune_to_cap()
            if removed:
                db_call("clip cleanup", db.clear_clip_paths, filenames=removed)
                print(f"   (pruned {len(removed)} local clip(s) to stay under "
                      f"{config.MAX_CLIPS_MB} MB)")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
