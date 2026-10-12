"""Unit tests for bird profiles and clip acoustics (no database or network)."""
import io
import os
import sys
import wave

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acoustics  # noqa: E402
import bird_profile as bp  # noqa: E402

NYC = (40.785, -73.975, "America/New_York")


def make(rows):
    """rows: ('YYYY-MM-DD HH:MM:SS', species[, confidence])."""
    return pd.DataFrame([
        {"id": i, "detected_at": pd.Timestamp(r[0]), "common_name": r[1],
         "confidence": r[2] if len(r) > 2 else 0.9}
        for i, r in enumerate(rows, start=1)])


def daily(species, days, hour="07:00:00", start="2026-06-01"):
    return [(f"{(pd.Timestamp(start) + pd.Timedelta(days=d)).date()} {hour}",
             species) for d in days]


# A listening season of 90 days with a resident heard every day.
SEASON = daily("Resident", range(90))


# --- At a glance / residency -----------------------------------------------

@pytest.mark.parametrize("extra, label", [
    (daily("X", range(0, 90, 3)), "Resident"),
    (daily("X", [0, 40, 89]), "Occasional"),
    (daily("X", range(50, 90, 2)), "Arrived"),
    (daily("X", range(0, 40, 2)), "Departed"),
    (daily("X", range(30, 60, 2)), "Passing through"),
    (daily("X", [45, 46]), "Rare visitor"),
])
def test_residency_labels(extra, label):
    v = make(SEASON + extra)
    assert bp.residency(v, v, "X")[0] == label


def test_at_a_glance_numbers_and_rank():
    v = make(SEASON + daily("X", [10, 11]) + [("2026-06-11 08:00:00", "X", 0.5)])
    g = bp.at_a_glance(v, v, "X")
    assert g["detections"] == 3 and g["days_heard"] == 2
    assert g["days_listened"] == 90
    assert g["best_confidence"] == 0.9
    assert (g["rank"], g["species_count"]) == (2, 2)
    assert str(g["first_heard"]) == "2026-06-11"
    assert bp.at_a_glance(v, v, "Nobody") is None


def test_latest_species_and_options():
    v = make([("2026-06-01 07:00:00", "B"), ("2026-06-02 07:00:00", "A")])
    assert bp.latest_species(v) == "A"
    assert bp.species_options(v) == ["A", "B"]


# --- Time of day -------------------------------------------------------------

def test_hourly_vs_all_shares():
    v = make([("2026-06-01 06:00:00", "A"), ("2026-06-01 06:30:00", "A"),
              ("2026-06-01 18:00:00", "B"), ("2026-06-01 18:30:00", "B")])
    h = bp.hourly_vs_all(v, "A").set_index("hour")
    assert h.at[6, "bird_share"] == 1.0
    assert h.at[6, "all_share"] == 0.5
    assert h["bird_share"].sum() == pytest.approx(1)


def test_period_callout():
    evening = daily("Owlish", range(10), hour="19:00:00")
    morning = daily("Other", range(40), hour="09:00:00")
    v = make(evening + morning)
    text = bp.period_callout(v, "Owlish")
    assert text.startswith("5.0 times as active in the evening")
    assert bp.period_callout(v, "Other") is None  # 1.25x: not notable
    night = make(daily("Night", range(3), hour="23:00:00")
                 + daily("Other", range(400), hour="09:00:00"))
    assert bp.period_callout(night, "Night").startswith("Far more active at night")


# --- Dawn --------------------------------------------------------------------

def test_dawn_rank_needs_min_days():
    rows = (daily("Early", range(6), hour="05:20:00")
            + daily("Late", range(6), hour="07:30:00")
            + daily("Once", [0], hour="06:00:00"))
    v = make(rows)
    assert bp.dawn(v, "Early", *NYC)["rank"] == 1
    late = bp.dawn(v, "Late", *NYC)
    assert (late["rank"], late["ranked"], late["days"]) == (2, 2, 6)
    once = bp.dawn(v, "Once", *NYC)
    assert once["rank"] is None and once["days"] == 1
    assert bp.dawn(v, "Missing", *NYC)["median_min"] is None


# --- Calling pattern -----------------------------------------------------------

def test_pattern_table_bouts_and_gaps():
    # Two bouts: 4 calls 20 s apart, then (10 min later) 2 calls 40 s apart.
    rows = [(f"2026-06-01 07:00:{s:02d}", "A") for s in (0, 20, 40)]
    rows += [("2026-06-01 07:01:00", "A"),
             ("2026-06-01 07:11:00", "A"), ("2026-06-01 07:11:40", "A")]
    t = bp.pattern_table(make(rows)).set_index("common_name")
    assert t.at["A", "bouts"] == 2
    assert t.at["A", "calls_per_bout"] == 3  # median of 4 and 2
    assert t.at["A", "bout_minutes"] == pytest.approx((1.0 + 40 / 60) / 2)
    # Within-run gaps: 20, 20, 20, 40 (the 10-minute gap ends the run).
    assert t.at["A", "gap_s"] == 20


def test_calling_pattern_typical_needs_min_bouts():
    rows = []
    for d in range(6):  # six one-call bouts a day apart
        rows.append((f"2026-06-{d + 1:02d} 07:00:00", "Solo"))
    bird, typical = bp.calling_pattern(make(rows), "Solo", min_bouts=5)
    assert bird["bouts"] == 6 and typical["calls_per_bout"] == 1


# --- Company -------------------------------------------------------------------

def test_companions_lift_filters_chance_overlap():
    rows = []
    for d in range(10):
        day = f"2026-06-{d + 1:02d}"
        rows += [(f"{day} 07:00:00", "A"), (f"{day} 07:05:00", "Pal")]
        rows += [(f"{day} {h:02d}:00:00", "Common") for h in range(6, 20)]
    c = bp.companions(make(rows), "A")
    assert list(c["common_name"]) == ["Pal"]
    assert c.iloc[0]["shared"] == 10 and c.iloc[0]["share"] == 1.0
    assert c.iloc[0]["lift"] > 1.25


# --- Confidence and distinctiveness --------------------------------------------

def test_confidence_vs_all_sums_to_one():
    v = make([("2026-06-01 07:00:00", "A", 0.3), ("2026-06-01 07:01:00", "B", 1.0)])
    c = bp.confidence_vs_all(v, "A")
    assert c["bird_share"].sum() == pytest.approx(1)
    assert c["all_share"].sum() == pytest.approx(1)
    assert c.iloc[-1]["all_share"] == 0.5  # 1.0 lands in the last bin


def test_distinctive_finds_the_evening_singer():
    rows = []
    for i, sp in enumerate(["A", "B", "C", "D", "E", "F"]):
        rows += daily(sp, range(25), hour=f"{8 + i:02d}:00:00")
    rows += daily("Dusk", range(25), hour="19:30:00")
    v = make(rows)
    phrases, n = bp.distinctive(v, v, "Dusk")
    assert n == 7 and "an evening singer" in phrases
    few, _ = bp.distinctive(make(rows + [("2026-06-01 07:00:00", "Rare")]),
                            v, "Rare")
    assert few is None


# --- Acoustics -----------------------------------------------------------------

def wav_bytes(freq_hz, seconds=1.0, sr=48000, amp=0.5, burst=None, hum_hz=None):
    """A sine tone. ``burst=(start, end)`` keeps it only in that window (a
    call); ``hum_hz`` adds a steady background tone throughout."""
    t = np.arange(int(seconds * sr)) / sr
    y = amp * np.sin(2 * np.pi * freq_hz * t)
    if burst:
        y = y * ((t >= burst[0]) & (t < burst[1]))
    if hum_hz:
        y = y + 0.3 * np.sin(2 * np.pi * hum_hz * t)
    x = (np.clip(y, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    return buf.getvalue()


def test_read_wav_roundtrip():
    x, sr = acoustics.read_wav(wav_bytes(1000, seconds=0.5))
    assert sr == 48000 and len(x) == 24000
    assert abs(x).max() == pytest.approx(0.5, abs=0.01)


def test_tone_lands_in_its_band():
    a = acoustics.analyze(wav_bytes(3100, seconds=5.0, burst=(2.0, 2.5)))
    db = a["spectrogram"]
    assert db.shape == (acoustics.N_BANDS, acoustics.TIME_STEPS)
    assert db.max() == 0 and db.min() >= acoustics.FLOOR_DB
    band = int(np.argmax(db.mean(axis=1)))
    assert band == 3100 // acoustics.BAND_HZ
    peak, lo, hi = acoustics.signature_stats(a["signature"])
    assert peak == pytest.approx(3125) and lo <= peak <= hi
    assert a["signature"].sum() == pytest.approx(1, abs=1e-5)
    assert a["duration_s"] == pytest.approx(5.0)


def test_signature_ignores_low_rumble():
    low = acoustics.analyze(wav_bytes(200, seconds=5.0, burst=(1, 1.5)))["signature"]
    centres = acoustics.band_centres_hz()
    assert low[centres < acoustics.SIGNATURE_MIN_HZ].sum() == 0


def test_signature_ignores_steady_background():
    # A loud steady 1 kHz hum under a short 6 kHz call: only the call counts.
    sig = acoustics.analyze(wav_bytes(6100, seconds=5.0, burst=(2.0, 2.3),
                                      hum_hz=1000))["signature"]
    peak, _, _ = acoustics.signature_stats(sig)
    assert peak == pytest.approx(6125)
    hum_band = 1000 // acoustics.BAND_HZ
    assert sig[hum_band] < 0.01
    steady = acoustics.analyze(wav_bytes(3000, seconds=5.0))["signature"]
    assert steady.sum() == 0  # no bursts at all


def test_mean_signature():
    a = np.array([1.0, 0, 0])
    b = np.array([0, 1.0, 0])
    m = acoustics.mean_signature([a, b, None])
    assert m.tolist() == [0.5, 0.5, 0]
    assert acoustics.mean_signature([]) is None


def test_fetch_rejects_non_http():
    with pytest.raises(ValueError):
        acoustics.fetch("file:///etc/passwd")
