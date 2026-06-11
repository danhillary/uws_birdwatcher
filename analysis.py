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
        keys: common_name, scientific_name, confidence, start_time, end_time.

        When human-voice filtering is on, we ask BirdNET for *all* classes
        (return_all_detections=True) so the non-bird "Human vocal" labels are
        visible — the location filter normally hides them. Each detection then
        carries an ``is_predicted_for_location_and_date`` flag we use to keep
        the usual location-filtered bird behaviour (see partition_detections)."""
        from birdnetlib import Recording

        when = when or datetime.datetime.now()
        sf.write(self._tmp, audio, sample_rate)

        # Ask BirdNET for everything down to the lowest bar in play: the
        # per-species analysis floor (we apply the real per-species bar after,
        # in filter_by_confidence) and, when filtering, the human-voice
        # threshold (so a quiet voice below MIN_CONFIDENCE still triggers the
        # privacy hold in partition_detections).
        floor = config.analysis_floor()
        if config.FILTER_HUMAN_VOICE:
            floor = min(floor, config.HUMAN_VOICE_CONF)

        kwargs = dict(
            lat=config.LATITUDE,
            lon=config.LONGITUDE,
            date=when,
            min_conf=floor,
        )
        if config.FILTER_HUMAN_VOICE:
            kwargs["return_all_detections"] = True
        try:
            recording = Recording(self._analyzer, self._tmp, **kwargs)
        except TypeError:
            # Older birdnetlib without return_all_detections: fall back to the
            # location-filtered behaviour (human filtering simply no-ops).
            kwargs.pop("return_all_detections", None)
            recording = Recording(self._analyzer, self._tmp, **kwargs)
        recording.analyze()
        return recording.detections


def filter_by_confidence(detections):
    """Drop detections that fall below their species' confidence threshold.

    Most species use the global MIN_CONFIDENCE, but noise-prone ones carry a
    higher bar (a "woodpecker" at 0.55 is far more likely a jackhammer than a
    bird) — see config.SPECIES_MIN_CONF / config.species_min_conf."""
    return [d for d in detections
            if d["confidence"] >= config.species_min_conf(d["common_name"])]


def partition_detections(detections):
    """Split raw BirdNET output into (bird_detections, human_voice_present).

    Bird detections are those predicted for the location/date (BirdNET's normal
    behaviour) at or above MIN_CONFIDENCE, with every non-bird "noise" class
    dropped. ``human_voice_present`` is True when any Human* class is heard at or
    above HUMAN_VOICE_CONF — the listener uses it to keep that segment's clips
    local rather than uploading them to the public bucket.

    Works whether or not return_all_detections was set: without it, no non-bird
    classes appear, the location flag is absent (treated as in-location), and
    this just passes the bird detections through."""
    birds = []
    human = False
    for d in detections:
        name = d.get("common_name", "")
        conf = d.get("confidence", 0.0)
        if name in config.HUMAN_LABELS:
            if conf >= config.HUMAN_VOICE_CONF:
                human = True
            continue
        if name in config.NON_BIRD_LABELS:
            continue  # dog/engine/siren/etc. — not a bird, not a privacy issue
        if not d.get("is_predicted_for_location_and_date", True):
            continue  # bird, but not expected here/now — keep prior behaviour
        if conf >= config.MIN_CONFIDENCE:
            birds.append(d)
    return birds, human


def dedupe_per_species(detections):
    """Collapse multiple windows of the same species in one segment to the
    single highest-confidence detection. Returns a list sorted by confidence."""
    best = {}
    for d in detections:
        key = d["scientific_name"]
        if key not in best or d["confidence"] > best[key]["confidence"]:
            best[key] = d
    return sorted(best.values(), key=lambda d: d["confidence"], reverse=True)
