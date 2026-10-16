"""Listen on the microphone, identify birds with BirdNET, and record them.

Captures fixed-length audio segments, runs BirdNET on each (filtered to species
likely at your location/date), then for every detection: prints it, saves a
short audio clip and queues the detection in the local outbox. Recording is
local-first: this loop never touches the network. A background sync thread
(uploader.py) uploads clips to S3, writes detections to the database and
deletes the uploaded local clips, so a network or database outage only grows
the backlog. Local clips are pruned to stay under BW_MAX_CLIPS_MB. Another
background thread recomputes the false-positive filter flags every
BW_FILTER_REFRESH seconds.

A mic that goes silent or errors is reopened (PortAudio restarted, device found
again). If it stays dead for BW_MIC_GIVE_UP seconds the listener exits with
code 3 so the watchdog (capture/watchdog.py) can start a fresh process.

Run with:  python -m capture.listen   (or supervised: python -m capture.watchdog)
Stop with: Ctrl-C
"""
import faulthandler
import os
import sys
import time

import config

# If startup or a loop stalls long enough for the watchdog to kill us, dump
# every thread's stack to the log first, so a hang says where it was stuck.
# (Some Oct 14 restarts hung before printing anything.) Startup gets most of
# the watchdog's first-heartbeat allowance; touch_heartbeat() re-arms it with
# a shorter fuse each loop. It's armed before the remaining imports because
# importing sounddevice starts PortAudio, which can block on a wedged device.
faulthandler.dump_traceback_later(240)

import numpy as np
import sounddevice as sd

import clips_s3
import filter_refresh
import healthcheck
import outbox
import storage
import uploader
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


def touch_heartbeat():
    """Tell the watchdog we're still making progress (a local file, no network)."""
    faulthandler.dump_traceback_later(max(30, config.HANG_SECONDS - 30))
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

    storage.ensure_dirs()
    # Database setup, uploads, heartbeat and feed all happen on the sync
    # thread; recording starts without waiting for any of them.
    uploader.start()

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

    mic = MicGuard(config.MIC_REOPEN_AFTER, config.MIC_GIVE_UP_SECONDS)

    try:
        while True:
            try:
                audio = record_segment(device, channels)
                peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            except Exception as e:  # PortAudioError, a vanished device, a hang
                print(f"[{config.now_local():%H:%M:%S}] (mic error: {e})")
                audio, peak = None, 0.0
            touch_heartbeat()
            uploader.state["peak"] = peak  # the sync thread's dashboard heartbeat

            action = mic.check(peak >= 1e-4)
            if action == "ok":
                # Alive = hearing sound *and* syncing; if sync stays broken the
                # pings stop and you're alerted, though recording continues.
                if uploader.healthy():
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
                # Local first: save the clip and queue the detection; the sync
                # thread uploads and writes it. Human-voice clips are a privacy
                # hold: they stay on this machine and never go to the bucket.
                clip = storage.save_clip(
                    audio, config.SAMPLE_RATE, when,
                    d["common_name"], d["start_time"], d["end_time"],
                )
                outbox.add(dict(
                    detected_at=when, common_name=d["common_name"],
                    scientific_name=d["scientific_name"],
                    confidence=float(d["confidence"]),
                    lat=config.LATITUDE, lon=config.LONGITUDE,
                    clip_path=clip, clip_url=None,
                    upload_clip=clips_s3.enabled() and not has_voice,
                ))
                print(
                    f"[{stamp}] \U0001F426 {d['common_name']} "
                    f"({d['scientific_name']}) — {d['confidence']:.0%}  "
                    f"-> {clip}{' (kept local)' if has_voice else ''}"
                )
                uploader.state["last_det"] = when
            uploader.wake()

            # Keep local clips (upload backlog, human-voice holds, failed-upload
            # fallbacks) under the cap; the sync thread clears clip_path for
            # pruned files. S3 copies are permanent and never pruned.
            removed = storage.prune_to_cap()
            if removed:
                uploader.forget_clips(removed)
                print(f"   (pruned {len(removed)} local clip(s) to stay under "
                      f"{config.MAX_CLIPS_MB} MB)")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
