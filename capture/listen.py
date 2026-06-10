"""Phase 1 prototype: listen on the microphone and print identified birds.

Captures fixed-length audio segments from the mic, runs BirdNET on each
(filtered to species likely at your location/date), and prints any detections
to the terminal.

Run with:  python -m capture.listen
Stop with: Ctrl-C
"""
import datetime
import os
import sys
import tempfile

import numpy as np
import sounddevice as sd
import soundfile as sf

import config


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


def main():
    # Import here so device-listing / --help works even before the heavy
    # TensorFlow stack is importable.
    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer

    try:
        device = resolve_device(config.INPUT_DEVICE)
    except ValueError as e:
        print(f"Audio device error: {e}")
        return 1
    info = sd.query_devices(device)
    channels = min(config.CHANNELS, info["max_input_channels"])

    print("Loading BirdNET model (first run downloads/initialises, ~10-20s)...")
    analyzer = Analyzer()

    dev_name = info["name"]
    print(
        f"Listening on '{dev_name}' "
        f"@ {config.SAMPLE_RATE} Hz, {config.SEGMENT_SECONDS}s segments.\n"
        f"Location: {config.LATITUDE}, {config.LONGITUDE} | "
        f"min confidence: {config.MIN_CONFIDENCE}\n"
        f"Press Ctrl-C to stop.\n"
    )

    tmp_path = os.path.join(tempfile.gettempdir(), "bw_segment.wav")

    try:
        while True:
            audio = record_segment(device, channels)

            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak < 1e-4:
                print(f"[{datetime.datetime.now():%H:%M:%S}] (silence — check the mic)")
                continue

            sf.write(tmp_path, audio, config.SAMPLE_RATE)

            recording = Recording(
                analyzer,
                tmp_path,
                lat=config.LATITUDE,
                lon=config.LONGITUDE,
                date=datetime.datetime.now(),
                min_conf=config.MIN_CONFIDENCE,
            )
            recording.analyze()

            now = f"{datetime.datetime.now():%H:%M:%S}"
            if recording.detections:
                for d in recording.detections:
                    print(
                        f"[{now}] \U0001F426 {d['common_name']} "
                        f"({d['scientific_name']}) — {d['confidence']:.0%}"
                    )
            else:
                print(f"[{now}] ... no birds detected")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
