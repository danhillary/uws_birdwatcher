"""Unit tests for the false-positive filter rules (no database needed).

Run with:  python -m pytest tests
The end-to-end check against the trial run on live data is
``python -m filter_refresh --validate``.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filters  # noqa: E402

DAY = "2026-07-01"


def make(rows):
    """rows: (timestamp or 'HH:MM:SS' on DAY, species, confidence)."""
    out = []
    for i, (ts, sp, conf) in enumerate(rows, start=1):
        if len(ts) <= 8:
            ts = f"{DAY} {ts}"
        out.append({"id": i, "detected_at": pd.Timestamp(ts),
                    "common_name": sp, "scientific_name": sp + " sci",
                    "confidence": conf})
    return pd.DataFrame(out)


def kept(result, species=None):
    d = result.detections
    if species is not None:
        d = d[d["common_name"] == species]
    return int(d["keep"].sum())


def flag(result, test, species):
    d = result.detections
    return d.loc[d["common_name"] == species, f"pass_{test}"].tolist()


def strong_days(species, n_days, conf=0.9):
    """Solo strong detections on n separate days (passes every test)."""
    return [(f"2026-06-{10 + i:02d} 07:00:00", species, conf)
            for i in range(n_days)]


# --- Test 1: anchored bouts ----------------------------------------------

def test_anchor_keeps_bout_with_one_confident_call():
    r = filters.compute(make([("07:00:00", "A", 0.4), ("07:01:00", "A", 0.65),
                              ("07:02:30", "A", 0.3)]), enabled=("anchor",))
    assert flag(r, "anchor", "A") == [True, True, True]


def test_anchor_drops_bout_without_anchor_and_splits_on_gap():
    # 07:00 + 07:01 form a bout anchored by 0.7; 07:10 is 9 minutes later,
    # its own bout, and never reaches 0.6.
    r = filters.compute(make([("07:00:00", "A", 0.7), ("07:01:00", "A", 0.3),
                              ("07:10:00", "A", 0.5)]), enabled=("anchor",))
    assert flag(r, "anchor", "A") == [True, True, False]
    assert r.detections["bout_id"].nunique() == 2


def test_anchor_bouts_are_per_species():
    # B's weak call sits inside A's bout in time but is its own species.
    r = filters.compute(make([("07:00:00", "A", 0.9), ("07:00:30", "B", 0.3)]),
                        enabled=("anchor",))
    assert flag(r, "anchor", "B") == [False]


def test_bout_gap_threshold_is_configurable():
    df = make([("07:00:00", "A", 0.9), ("07:03:00", "A", 0.3)])
    assert flag(filters.compute(df), "anchor", "A") == [True, False]
    wide = filters.compute(df, {"bout_gap_s": 300})
    assert flag(wide, "anchor", "A") == [True, True]


# --- Test 2: shadow labels -----------------------------------------------

def test_shadow_drops_weaker_label_on_same_sound():
    r = filters.compute(make([("07:00:00", "A", 0.9), ("07:00:00", "B", 0.6),
                              ("07:00:01", "C", 0.95), ("07:00:05", "D", 0.4)]),
                        enabled=("shadow",))
    d = r.detections.set_index("common_name")
    assert not d.at["A", "pass_shadow"] and d.at["A", "shadowed_by"] == "C"
    assert not d.at["B", "pass_shadow"] and d.at["B", "shadowed_by"] == "C"
    assert d.at["C", "pass_shadow"]
    assert d.at["D", "pass_shadow"]  # 5 s away: outside the 1.5 s window


def test_shadow_ignores_same_species_and_ties():
    same = filters.compute(make([("07:00:00", "A", 0.6), ("07:00:01", "A", 0.9)]),
                           enabled=("shadow",))
    assert all(same.detections["pass_shadow"])
    tie = filters.compute(make([("07:00:00", "A", 0.9), ("07:00:00", "B", 0.9)]),
                          enabled=("shadow",))
    assert all(tie.detections["pass_shadow"])


def test_shadow_exemption_for_species_present_on_its_own():
    # B is beaten by A at 07:00, but B has strong solo calls on 3 other days,
    # so it is clearly present on its own: a real mixed flock, not a shadow.
    rows = [("07:00:00", "A", 0.9), ("07:00:00", "B", 0.6)]
    rows += strong_days("B", 3, conf=0.75)
    r = filters.compute(make(rows), enabled=("shadow",))
    assert all(flag(r, "shadow", "B"))
    assert r.species.set_index("common_name").at["B", "shadow_exempt"]
    # Two solo days is not enough; and 0 days switches the exemption off.
    r2 = filters.compute(make(rows[:2] + strong_days("B", 2, 0.75)),
                         enabled=("shadow",))
    assert flag(r2, "shadow", "B") == [True, True, False]
    r3 = filters.compute(make(rows), {"shadow_exempt_days": 0},
                         enabled=("shadow",))
    assert not all(flag(r3, "shadow", "B"))


# --- Test 3: repeatability -----------------------------------------------

def test_repeat_needs_strong_calls_on_two_days():
    two = filters.compute(make(strong_days("A", 2, 0.7)))
    one = filters.compute(make(strong_days("A", 1, 0.7)
                               + [("2026-06-20 07:00:00", "A", 0.65)]))
    assert all(flag(two, "repeat", "A"))
    assert not any(flag(one, "repeat", "A"))


def test_repeat_one_strong_bout_keeps_a_flyover():
    r = filters.compute(make([("07:00:00", "A", 0.75), ("07:00:30", "A", 0.8),
                              ("07:01:00", "A", 0.72)]))
    assert all(flag(r, "repeat", "A"))
    r2 = filters.compute(make([("07:00:00", "A", 0.75), ("07:00:30", "A", 0.8)]))
    assert not any(flag(r2, "repeat", "A"))


def test_repeat_is_judged_on_rows_passing_enabled_tests_1_and_2():
    # A's second strong day is shadowed by B. With the shadow test on, that
    # day doesn't count and A fails repeatability; with it off, A passes.
    rows = strong_days("A", 1, 0.75) + [("2026-06-20 07:00:00", "A", 0.75),
                                        ("2026-06-20 07:00:00", "B", 0.9)]
    rows += strong_days("B", 2)
    on = filters.compute(make(rows), {"shadow_exempt_days": 0})
    off = filters.compute(make(rows), {"shadow_exempt_days": 0},
                          enabled=("anchor", "repeat", "daypattern"))
    assert not any(flag(on, "repeat", "A"))
    assert all(flag(off, "repeat", "A"))


# --- Test 4: round-the-clock days ----------------------------------------

def round_the_clock(species, peak, day=DAY):
    hours = [0, 1, 6, 9, 12, 15, 18, 21]  # 8 distinct hours, 2 at night
    return [(f"{day} {h:02d}:00:00", species, peak if h == 12 else 0.5)
            for h in hours]


def test_daypattern_drops_round_the_clock_day_without_a_confident_call():
    rows = round_the_clock("A", 0.75) + strong_days("A", 2)
    r = filters.compute(make(rows), enabled=("daypattern",))
    d = r.detections[r.detections["common_name"] == "A"]
    on_day = d["detected_at"].dt.strftime("%Y-%m-%d") == DAY
    assert not d.loc[on_day, "pass_daypattern"].any()
    assert d.loc[~on_day, "pass_daypattern"].all()  # other days untouched


def test_daypattern_spared_by_one_confident_call_or_too_few_hours():
    spared = filters.compute(make(round_the_clock("A", 0.85)))
    assert all(flag(spared, "daypattern", "A"))
    fewer = filters.compute(make(round_the_clock("A", 0.75)[:7]))
    assert all(flag(fewer, "daypattern", "A"))


def test_daypattern_needs_night_hours():
    rows = [(f"{h:02d}:00:00", "A", 0.5) for h in range(6, 21, 2)]  # 8 day hours
    assert all(flag(filters.compute(make(rows)), "daypattern", "A"))


# --- Combining, overrides, attribution -----------------------------------

def test_keep_combines_enabled_tests_and_attributes_first_failure():
    rows = [("07:00:00", "A", 0.3)]  # fails anchor and repeat
    r = filters.compute(make(rows))
    d = r.detections.iloc[0]
    assert not d["keep"]
    assert d["removed_by"] == "anchor"
    assert d["failed_tests"] == "anchor,repeat"
    none_on = filters.compute(make(rows), enabled=())
    assert kept(none_on) == 1


def test_overrides_win_over_tests():
    rows = [("07:00:00", "A", 0.3)] + strong_days("B", 3)
    r = filters.compute(make(rows), overrides={"A": "confirmed", "B": "rejected"})
    assert kept(r, "A") == 1
    assert kept(r, "B") == 0
    assert set(r.detections.loc[r.detections["common_name"] == "B",
                                "removed_by"]) == {"override"}
    sp = r.species.set_index("common_name")
    assert sp.at["B", "top_removed_by"] == "override"
    assert sp.at["A", "override"] == "confirmed"


def test_species_summary_counts():
    rows = [("07:00:00", "A", 0.3)] + strong_days("B", 3)
    sp = filters.compute(make(rows)).species.set_index("common_name")
    assert (sp.at["B", "raw"], sp.at["B", "kept"], sp.at["B", "pct_kept"]) == (3, 3, 100.0)
    assert pd.isna(sp.at["B", "top_removed_by"])
    assert (sp.at["A", "raw"], sp.at["A", "kept"], sp.at["A", "removed_anchor"]) == (1, 0, 1)


def test_cadence_flags_slow_regular_rhythm():
    # 12 calls exactly 90 s apart: median 90 s, CV 0 -> mechanical.
    start = pd.Timestamp(f"{DAY} 07:00:00")
    rows = [((start + pd.Timedelta(seconds=90 * i)).strftime("%Y-%m-%d %H:%M:%S"),
             "A", 0.9) for i in range(12)]
    sp = filters.compute(make(rows)).species.set_index("common_name")
    assert sp.at["A", "median_gap_s"] == 90
    assert sp.at["A", "gap_cv"] == 0
    assert sp.at["A", "cadence_flag"]


def test_empty_input():
    empty = pd.DataFrame(columns=["id", "detected_at", "common_name",
                                  "confidence"])
    r = filters.compute(empty)
    assert r.detections.empty and r.species.empty
    assert "keep" in r.detections.columns


def test_config_resolution():
    cfg = filters.resolve_config({"bout_gap_s": 300.0, "nope": 1,
                                  "anchor_conf": None})
    assert cfg["bout_gap_s"] == 300 and isinstance(cfg["bout_gap_s"], int)
    assert cfg["anchor_conf"] == 0.6
    assert "nope" not in cfg


@pytest.mark.parametrize("p", filters.PARAMS, ids=lambda p: p.name)
def test_param_defaults_within_slider_range(p):
    assert p.min <= p.default <= p.max
