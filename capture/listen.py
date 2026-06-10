"""Listen on the microphone, identify birds with BirdNET, and record them.

Captures fixed-length audio segments, runs BirdNET on each (filtered to species
likely at your location/date), then for every detection: prints it, saves a
short audio clip, and stores a row in the database. Old clips are pruned to stay
under the configured storage cap.

Run with:  python -m capture.listen
Stop with: Ctrl-C
"""
import socket
import sys

import numpy as np
import sounddevice as sd

import config
import db
import storage
from analysis import BirdAnalyzer, dedupe_per_species


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
    sd.wait()
    # If the device gave us multiple channels, collapse to mono.
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    return audio.reshape(-1)


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
    try:
        device = resolve_device(config.INPUT_DEVICE)
    except ValueError as e:
        print(f"Audio device error: {e}")
        return 1
    info = sd.query_devices(device)
    channels = min(config.CHANNELS, info["max_input_channels"])

    db.init_db()
    storage.ensure_dirs()

    print("Loading BirdNET model (first run downloads/initialises, ~10-20s)...")
    analyzer = BirdAnalyzer()

    print(
        f"Listening on '{info['name']}' "
        f"@ {config.SAMPLE_RATE} Hz, {config.SEGMENT_SECONDS}s segments.\n"
        f"Location: {config.LATITUDE}, {config.LONGITUDE} | "
        f"min confidence: {config.MIN_CONFIDENCE}\n"
        f"Saving to: {config.DB_HOST}/{config.DB_NAME} (schema {config.DB_SCHEMA})\n"
        f"Press Ctrl-C to stop.\n"
    )

    host = socket.gethostname()
    last_det = None  # time of the most recent detection, for the heartbeat

    try:
        while True:
            audio = record_segment(device, channels)

            peak = float(np.max(np.abs(audio))) if audio.size else 0.0

            # Heartbeat: let the dashboard see we're alive and what the mic is
            # hearing, even during long silent stretches. Never let a DB hiccup
            # kill the capture loop.
            try:
                db.record_heartbeat(host, peak, last_det)
            except Exception as e:
                print(f"   (heartbeat write failed: {e})")

            if peak < 1e-4:
                print(f"[{config.now_local():%H:%M:%S}] (silence — check the mic)")
                continue

            when = config.now_local()
            detections = dedupe_per_species(
                analyzer.analyze(audio, config.SAMPLE_RATE, when=when)
            )

            stamp = f"{when:%H:%M:%S}"
            if not detections:
                print(f"[{stamp}] ... no birds detected")
                continue

            for d in detections:
                clip = storage.save_clip(
                    audio, config.SAMPLE_RATE, when,
                    d["common_name"], d["start_time"], d["end_time"],
                )
                db.insert_detection(
                    when, d["common_name"], d["scientific_name"],
                    d["confidence"], config.LATITUDE, config.LONGITUDE, clip,
                )
                print(
                    f"[{stamp}] \U0001F426 {d['common_name']} "
                    f"({d['scientific_name']}) — {d['confidence']:.0%}  -> {clip}"
                )
                last_det = when

            # Keep clip storage under the cap; forget pruned clips in the DB.
            removed = storage.prune_to_cap()
            if removed:
                db.clear_clip_paths(removed)
                print(f"   (pruned {len(removed)} old clip(s) to stay under "
                      f"{config.MAX_CLIPS_MB} MB)")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
