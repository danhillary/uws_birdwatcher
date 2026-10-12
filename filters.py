"""False-positive filters for bird detections.

BirdNET at a fixed window produces plenty of detections that are not birds:
strings of mediocre guesses from machine noise, second labels on one sound,
one-off misfires. These tests judge the *shape* of the data (clustering,
repetition, overlap, time-of-day spread) rather than whether a species is
likely in New York, and they **flag** detections instead of deleting them. Raw
rows in ``birdwatcher.detections`` are never touched.

This module is pure logic: it takes a DataFrame of detections and a threshold
config and returns flags. It never talks to the database, so the dashboard can
recompute instantly when a slider moves, and the rules are unit-testable.
``filter_refresh`` handles loading, storing and scheduling.

The tests
---------
1. ``anchor``      Anchored bouts. Group a species' detections into bouts (gaps
                   of ``bout_gap_s`` or less); keep a bout only if some detection
                   in it reaches ``anchor_conf``.
2. ``shadow``      Shadow labels. Drop a detection if another species was
                   detected within ``shadow_window_s`` with higher confidence.
                   A species with strong (``shadow_exempt_conf``), unshadowed
                   detections on ``shadow_exempt_days`` or more days is exempt:
                   it is clearly present on its own, so overlaps are real mixed
                   flocks (starling with grackle, house sparrow). 0 days turns
                   the exemption off.
3. ``repeat``      Repeatability (per species). Keep a species only if it has
                   ``strong_conf`` detections on ``repeat_min_days`` separate
                   days, or one bout with ``repeat_min_bout`` strong detections.
                   Judged on the rows that pass whichever of tests 1 and 2 are on.
4. ``daypattern``  Round-the-clock days (per species-day, judged on raw rows).
                   Drop a species' whole day if it spans ``rtc_min_hours``
                   distinct hours, includes ``rtc_min_night_hours`` night hours,
                   and never reaches ``rtc_ceiling``.

Cadence is a diagnostic only: within runs (gaps up to ``cadence_run_gap_s``)
it reports each species' median gap and coefficient of variation, and flags a
slow, regular rhythm (median above ``cadence_min_median_s``, CV below
``cadence_max_cv``) that looks mechanical.

Manual species overrides ("confirmed" / "rejected") win over every test.

Note on timing: the listener stamps every detection in a 15-second segment
with the same time, so the shadow window effectively means "same segment".
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Test keys in evaluation order. "Removed by" names the first failing test.
TESTS = ("anchor", "shadow", "repeat", "daypattern")

TEST_LABELS = {
    "anchor": "Anchored bouts",
    "shadow": "Shadow labels",
    "repeat": "Repeatability",
    "daypattern": "Round-the-clock days",
}

TEST_HELP = {
    "anchor": "Drops runs of mediocre detections that never include one "
              "confident call.",
    "shadow": "Drops a second label on the same sound when another species "
              "scored higher at the same moment.",
    "repeat": "Drops species that never score well on more than one day.",
    "daypattern": "Drops a species' whole day when it is heard around the "
                  "clock, through the night, without one confident call "
                  "(machine noise read as a bird).",
}

OVERRIDE_STATUSES = ("confirmed", "rejected")


@dataclass(frozen=True)
class Param:
    """One tunable threshold. ``kind`` is int or float (stored as a float)."""
    name: str
    default: float
    label: str
    description: str
    test: str
    min: float
    max: float
    step: float
    kind: type = float


PARAMS = (
    Param("bout_gap_s", 120, "Bout gap (s)",
          "Detections of one species closer than this form one bout.",
          "anchor", 15, 900, 15, int),
    Param("anchor_conf", 0.6, "Anchor confidence",
          "A bout is kept only if one detection in it reaches this.",
          "anchor", 0.25, 1.0, 0.05),
    Param("shadow_window_s", 1.5, "Shadow window (s)",
          "Two species within this many seconds count as one sound.",
          "shadow", 0, 30, 0.5),
    Param("shadow_exempt_conf", 0.7, "Exemption confidence",
          "Confidence of the solo detections that exempt a species from "
          "shadowing.",
          "shadow", 0.25, 1.0, 0.05),
    Param("shadow_exempt_days", 3, "Exemption days",
          "Days with strong, unshadowed detections that exempt a species "
          "from shadowing (0 = no exemption).",
          "shadow", 0, 30, 1, int),
    Param("strong_conf", 0.7, "Strong confidence",
          "What counts as a strong detection for repeatability.",
          "repeat", 0.25, 1.0, 0.05),
    Param("repeat_min_days", 2, "Minimum strong days",
          "Distinct days with a strong detection needed to keep a species.",
          "repeat", 1, 30, 1, int),
    Param("repeat_min_bout", 3, "Minimum strong bout",
          "Or: strong detections within one bout needed to keep a species.",
          "repeat", 1, 30, 1, int),
    Param("rtc_min_hours", 8, "Round-the-clock hours",
          "Distinct hours in a day that mark a day as round-the-clock.",
          "daypattern", 2, 24, 1, int),
    Param("rtc_min_night_hours", 2, "Night hours",
          "Distinct night hours that day also needs.",
          "daypattern", 1, 12, 1, int),
    Param("rtc_night_start", 22, "Night starts (hour)",
          "First night hour (24-hour clock).",
          "daypattern", 0, 23, 1, int),
    Param("rtc_night_end", 4, "Night ends (hour)",
          "Night runs up to, not including, this hour.",
          "daypattern", 0, 23, 1, int),
    Param("rtc_ceiling", 0.8, "Round-the-clock ceiling",
          "A day is spared if any detection reaches this.",
          "daypattern", 0.25, 1.0, 0.05),
    Param("cadence_run_gap_s", 600, "Cadence run gap (s)",
          "Gaps longer than this split a species' detections into runs.",
          "cadence", 60, 3600, 60, int),
    Param("cadence_min_median_s", 60, "Cadence median gap (s)",
          "Flag species whose median gap within runs exceeds this ...",
          "cadence", 15, 600, 5),
    Param("cadence_max_cv", 1.0, "Cadence max CV",
          "... and whose gap coefficient of variation is below this.",
          "cadence", 0.1, 3.0, 0.05),
    Param("cadence_min_gaps", 10, "Cadence minimum gaps",
          "Species with fewer gaps than this get no cadence flag.",
          "cadence", 2, 100, 1, int),
)

PARAMS_BY_NAME = {p.name: p for p in PARAMS}
DEFAULTS = {p.name: p.kind(p.default) for p in PARAMS}


def resolve_config(cfg=None):
    """Defaults overlaid with ``cfg``; unknown names ignored, types coerced."""
    out = dict(DEFAULTS)
    for name, value in (cfg or {}).items():
        p = PARAMS_BY_NAME.get(name)
        if p is not None and value is not None:
            out[name] = p.kind(value)
    return out


@dataclass
class Result:
    """``detections``: one row per input detection plus flag columns.
    ``species``: one row per species with raw/kept counts and diagnostics."""
    detections: pd.DataFrame
    species: pd.DataFrame
    config: dict
    enabled: tuple


FLAG_COLUMNS = ["bout_id", "pass_anchor", "pass_shadow", "pass_repeat",
                "pass_daypattern", "shadowed_by", "keep", "removed_by",
                "failed_tests"]

SPECIES_COLUMNS = ["common_name", "scientific_name", "raw", "kept", "pct_kept",
                   "top_removed_by", "removed_anchor", "removed_shadow",
                   "removed_repeat", "removed_daypattern", "removed_override",
                   "shadow_exempt", "median_gap_s", "gap_cv", "cadence_flag",
                   "override"]


def _shadowed(times, species, conf, window):
    """For each detection, the species of the highest-confidence *other*
    species detection within ``window`` seconds that beats it, or None.

    ``times`` must be sorted ascending (seconds as float)."""
    lo = np.searchsorted(times, times - window, "left")
    hi = np.searchsorted(times, times + window, "right")
    out = np.full(len(times), None, dtype=object)
    for i in range(len(times)):
        best = conf[i]
        for j in range(lo[i], hi[i]):
            if conf[j] > best and species[j] != species[i]:
                best = conf[j]
                out[i] = species[j]
    return out


def _empty_result(df, cfg, enabled):
    det = df.copy()
    for c in FLAG_COLUMNS:
        det[c] = pd.Series(dtype=object)
    return Result(det, pd.DataFrame(columns=SPECIES_COLUMNS), cfg, enabled)


def compute(df, cfg=None, enabled=TESTS, overrides=None):
    """Flag every detection in ``df``.

    ``df`` needs ``id``, ``detected_at``, ``common_name`` and ``confidence``;
    any other columns (``scientific_name``, clip URLs) are carried through.
    ``enabled`` is the tests that count toward ``keep``; every test's flag is
    computed regardless, but test 3 is judged only on rows that pass whichever
    of tests 1 and 2 are enabled. ``overrides`` maps common name to
    "confirmed" or "rejected".
    """
    cfg = resolve_config(cfg)
    enabled = tuple(t for t in TESTS if t in set(enabled or ()))
    overrides = {k: v for k, v in (overrides or {}).items()
                 if v in OVERRIDE_STATUSES}
    if df is None or len(df) == 0:
        return _empty_result(df if df is not None else pd.DataFrame(),
                             cfg, enabled)

    d = df.copy()
    d["detected_at"] = pd.to_datetime(d["detected_at"])
    if "scientific_name" not in d.columns:
        d["scientific_name"] = None
    d = d.sort_values(["common_name", "detected_at", "id"]).reset_index(drop=True)
    sp = d["common_name"]
    day = d["detected_at"].dt.normalize()
    d["_day"] = day
    hour = d["detected_at"].dt.hour

    # --- Test 1: anchored bouts --------------------------------------------
    gap = d.groupby("common_name")["detected_at"].diff().dt.total_seconds()
    d["bout_id"] = (gap.isna() | (gap > cfg["bout_gap_s"])).cumsum().astype(int)
    bout_peak = d.groupby("bout_id")["confidence"].transform("max")
    d["pass_anchor"] = bout_peak >= cfg["anchor_conf"]

    # --- Test 2: shadow labels (on raw rows) -------------------------------
    order = np.argsort(d["detected_at"].values, kind="stable")
    secs = d["detected_at"].values.astype("datetime64[ms]").astype(np.int64) / 1000.0
    by = np.empty(len(d), dtype=object)
    by[order] = _shadowed(secs[order], sp.values[order],
                          d["confidence"].values[order], cfg["shadow_window_s"])
    d["shadowed_by"] = by
    shadowed = d["shadowed_by"].notna()
    exempt = set()
    if cfg["shadow_exempt_days"] > 0:
        solo = d[~shadowed & (d["confidence"] >= cfg["shadow_exempt_conf"])]
        solo_days = solo.groupby("common_name")["_day"].nunique()
        exempt = set(solo_days[solo_days >= cfg["shadow_exempt_days"]].index)
    d["pass_shadow"] = ~shadowed | sp.isin(exempt)

    # --- Test 3: repeatability (species; on rows passing enabled 1-2) ------
    base = pd.Series(True, index=d.index)
    for t in ("anchor", "shadow"):
        if t in enabled:
            base &= d[f"pass_{t}"]
    strong = d[base & (d["confidence"] >= cfg["strong_conf"])]
    strong_days = strong.groupby("common_name")["_day"].nunique()
    strong_bout = strong.groupby(["common_name", "bout_id"]).size() \
        .groupby(level="common_name").max()
    repeat_ok = (set(strong_days[strong_days >= cfg["repeat_min_days"]].index)
                 | set(strong_bout[strong_bout >= cfg["repeat_min_bout"]].index))
    d["pass_repeat"] = sp.isin(repeat_ok)

    # --- Test 4: round-the-clock days (species-day; raw rows) --------------
    ns, ne = cfg["rtc_night_start"], cfg["rtc_night_end"]
    is_night = ((hour >= ns) | (hour < ne)) if ns > ne else ((hour >= ns) & (hour < ne))
    keys = [sp, day]
    hours = hour.groupby(keys).transform("nunique")
    night_hours = hour.where(is_night).groupby(keys).transform("nunique")
    day_peak = d["confidence"].groupby(keys).transform("max")
    bad_day = ((hours >= cfg["rtc_min_hours"])
               & (night_hours >= cfg["rtc_min_night_hours"])
               & (day_peak < cfg["rtc_ceiling"]))
    d["pass_daypattern"] = ~bad_day

    # --- Combine -----------------------------------------------------------
    tests_keep = pd.Series(True, index=d.index)
    for t in enabled:
        tests_keep &= d[f"pass_{t}"]
    ov = sp.map(overrides)
    d["keep"] = (ov == "confirmed") | ((ov != "rejected") & tests_keep)

    failed = pd.Series("", index=d.index)
    for t in enabled:
        failed = failed.where(d[f"pass_{t}"],
                              failed + np.where(failed == "", "", ",") + t)
    d["failed_tests"] = failed.where(failed != "", None)
    first_fail = failed.str.split(",").str[0].where(failed != "", None)
    d["removed_by"] = np.where(
        d["keep"], None,
        np.where(ov == "rejected", "override", first_fail))

    species = _species_summary(d, cfg, exempt, overrides)
    d = d.drop(columns="_day").sort_values(["detected_at", "id"])         .reset_index(drop=True)
    return Result(d, species, cfg, enabled)


def _species_summary(d, cfg, exempt, overrides):
    g = d.groupby("common_name")
    s = pd.DataFrame({
        "scientific_name": g["scientific_name"].first(),
        "raw": g.size(),
        "kept": g["keep"].sum().astype(int),
    })
    s["pct_kept"] = (100.0 * s["kept"] / s["raw"]).round(1)

    removed = pd.crosstab(d["common_name"], d["removed_by"].fillna("kept"))
    for t in TESTS + ("override",):
        s[f"removed_{t}"] = removed.get(t, pd.Series(dtype=int)) \
            .reindex(s.index).fillna(0).astype(int)
    rcols = [f"removed_{t}" for t in TESTS + ("override",)]
    top = s[rcols].idxmax(axis=1).str.replace("removed_", "", regex=False)
    s["top_removed_by"] = [t if n > 0 else None
                           for t, n in zip(top, s[rcols].max(axis=1))]
    s["shadow_exempt"] = s.index.isin(exempt)

    # Cadence (diagnostic) on raw rows.
    gap = g["detected_at"].diff().dt.total_seconds()
    runs = gap[gap <= cfg["cadence_run_gap_s"]]
    gs = runs.groupby(d.loc[runs.index, "common_name"])
    stats = pd.DataFrame({"median": gs.median(), "mean": gs.mean(),
                          "std": gs.std(ddof=0), "n": gs.size()})
    s["median_gap_s"] = stats["median"].reindex(s.index)
    s["gap_cv"] = (stats["std"] / stats["mean"]).reindex(s.index).round(3)
    n = stats["n"].reindex(s.index).fillna(0)
    s["cadence_flag"] = ((n >= cfg["cadence_min_gaps"])
                         & (s["median_gap_s"] > cfg["cadence_min_median_s"])
                         & (s["gap_cv"] < cfg["cadence_max_cv"]))
    s["override"] = [overrides.get(name) for name in s.index]
    s = s.reset_index().rename(columns={"index": "common_name"})
    return s[SPECIES_COLUMNS].sort_values(["kept", "raw"], ascending=False) \
        .reset_index(drop=True)
