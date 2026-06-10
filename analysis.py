"""Reusable BirdNET analysis, decoupled from audio capture and storage.

Keeping this separate means the same identification logic can be driven by the
live microphone listener today, or by an HTTP service later, without changes.
"""
import datetime
import os
import tempfile

import soundfile as sf

import config


class BirdAnalyzer:
    """Thin wrapper around BirdNET that analyses in-memory audio."""

    def __init__(self):
        # Imported lazily so lightweight tools (e.g. listing mics) don't pay
        # the TensorFlow import cost.
        from birdnetlib.analyzer import Analyzer
        self._analyzer = Analyzer()
        self._tmp = os.path.join(tempfile.gettempdir(), "bw_segment.wav")

    def analyze(self, audio, sample_rate, when=None):
        """Analyse a mono float32 array. Returns BirdNET detection dicts with
        keys: common_name, scientific_name, confidence, start_time, end_time."""
        from birdnetlib import Recording

        when = when or datetime.datetime.now()
        sf.write(self._tmp, audio, sample_rate)
        recording = Recording(
            self._analyzer,
            self._tmp,
            lat=config.LATITUDE,
            lon=config.LONGITUDE,
            date=when,
            min_conf=config.MIN_CONFIDENCE,
        )
        recording.analyze()
        return recording.detections


def dedupe_per_species(detections):
    """Collapse multiple windows of the same species in one segment to the
    single highest-confidence detection. Returns a list sorted by confidence."""
    best = {}
    for d in detections:
        key = d["scientific_name"]
        if key not in best or d["confidence"] > best[key]["confidence"]:
            best[key] = d
    return sorted(best.values(), key=lambda d: d["confidence"], reverse=True)
