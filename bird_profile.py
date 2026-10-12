"""Per-bird analytics for the dashboard's bird profiles.

Pure pandas, no database and no Streamlit, so every number is unit-testable.
Each function takes ``view`` (the detections being shown: filtered or raw) and,
where coverage matters, ``raw`` (every detection, used to tell which days the
listener ran). Most sections compare one bird with all the others, because
"how is this bird different" is the question a profile answers.
"""
import numpy as np
import pandas as pd

import insights

# Parts of the day used for "when is it active" comparisons (local hours,
# start inclusive, end exclusive; night wraps past midnight).
PERIODS = (("dawn", 4, 8), ("morning", 8, 12), ("afternoon", 12, 17),
           ("evening", 17, 21), ("night", 21, 4))

# A species needs this many detections before it is ranked against others.
MIN_DETECTIONS_TO_COMPARE = 20

# Arrived/departed means first or last heard this many days inside the
# listening period.
SEASON_EDGE_DAYS = 21


def _of(view, name):
    return view[view["common_name"] == name]


def _in_period(hours, start, end):
    return (hours >= start) & (hours < end) if start < end else \
        (hours >= start) | (hours < end)


def species_options(view):
    """Species that can be profiled, alphabetically."""
    return sorted(view["common_name"].dropna().unique())


def latest_species(view):
    """The species of the most recent detection, or None."""
    if not len(view):
        return None
    return view.sort_values(["detected_at", "id"]).iloc[-1]["common_name"]


# --- At a glance ----------------------------------------------------------

def residency(view, raw, name):
    """A plain-language status: (label, explanation).

    Resident / Occasional (heard through the whole listening period, in most
    or only some weeks), Arrived / Departed (first or last heard well inside
    it), Passing through (both), Rare visitor (one or two days)."""
    rows = _of(view, name)
    days = insights.listening_days(raw)
    if not len(rows) or not days:
        return None, None
    heard = pd.to_datetime(rows["detected_at"]).dt.date
    first, last, n_days = heard.min(), heard.max(), heard.nunique()
    if n_days <= 2:
        return "Rare visitor", f"Heard on {n_days} day{'' if n_days == 1 else 's'}."
    arrived = (first - days[0]).days > SEASON_EDGE_DAYS
    departed = (days[-1] - last).days > SEASON_EDGE_DAYS
    weeks = insights.weeks_covered(raw)
    listening_weeks = int((weeks["days_listened"] > 0).sum())
    heard_weeks = insights._week_start(pd.Series(pd.to_datetime(heard))).nunique()
    fmt = lambda d: f"{d:%b} {d.day}"  # noqa: E731
    if arrived and departed:
        return "Passing through", f"Heard from {fmt(first)} to {fmt(last)}."
    if arrived:
        return "Arrived", f"First heard {fmt(first)} and still being heard."
    if departed:
        return "Departed", f"Last heard {fmt(last)}."
    label = "Resident" if heard_weeks >= 0.5 * listening_weeks else "Occasional"
    return label, (f"Heard in {heard_weeks} of the {listening_weeks} weeks the "
                   "listener ran.")


def at_a_glance(view, raw, name):
    """Headline numbers for one bird, and its rank by detections."""
    rows = _of(view, name)
    counts = view["common_name"].value_counts()
    days_listened = len(insights.listening_days(raw))
    if not len(rows):
        return None
    t = pd.to_datetime(rows["detected_at"])
    label, why = residency(view, raw, name)
    return {
        "detections": len(rows),
        "days_heard": int(t.dt.date.nunique()),
        "days_listened": days_listened,
        "first_heard": t.min().date(),
        "last_heard": t.max().date(),
        "best_confidence": float(rows["confidence"].max()),
        "rank": int((counts > counts[name]).sum()) + 1,
        "species_count": int(len(counts)),
        "status": label,
        "status_detail": why,
    }


def weekly(view, raw, name):
    """Weeks heard for one bird: days heard, days listened and their ratio
    (None for weeks the listener was off)."""
    grid = insights.presence_grid(_of(view, name), raw, min_days=1)
    return grid[["week", "days_heard", "days_listened", "share"]] \
        .reset_index(drop=True)


# --- Time of day ----------------------------------------------------------

def hourly_vs_all(view, name):
    """Share of this bird's detections in each hour, next to the share of
    every bird's detections (the 'average bird')."""
    hours = pd.to_datetime(view["detected_at"]).dt.hour
    allh = hours.value_counts().reindex(range(24), fill_value=0)
    birdh = hours[view["common_name"] == name].value_counts() \
        .reindex(range(24), fill_value=0)
    return pd.DataFrame({
        "hour": range(24),
        "detections": birdh.values,
        "bird_share": birdh.values / max(birdh.sum(), 1),
        "all_share": allh.values / max(allh.sum(), 1),
    })


def period_shares(view, name=None):
    """Share of detections in each part of the day, for one bird or all."""
    v = view if name is None else _of(view, name)
    hours = pd.to_datetime(v["detected_at"]).dt.hour
    n = max(len(v), 1)
    return {p: float(_in_period(hours, s, e).sum()) / n for p, s, e in PERIODS}


def period_callout(view, name, min_share=0.15, min_ratio=1.5):
    """The part of the day this bird favours most relative to the average
    bird, as a sentence, or None if nothing stands out."""
    mine, everyone = period_shares(view, name), period_shares(view)
    best, ratio = None, 0.0
    for p, _, _ in PERIODS:
        if mine[p] >= min_share and everyone[p] > 0:
            r = mine[p] / everyone[p]
            if r > ratio:
                best, ratio = p, r
    if best is None or ratio < min_ratio:
        return None
    where = "at night" if best == "night" else f"in the {best}"
    how = "Far more" if ratio >= 10 else f"{ratio:.1f} times as"
    return (f"{how} active {where} {'than' if ratio >= 10 else 'as'} the "
            f"average bird ({mine[best]:.0%} of its calls, against "
            f"{everyone[best]:.0%}).")


# --- Dawn -----------------------------------------------------------------

def dawn(view, name, lat, lon, tz, min_days=5):
    """When this bird usually starts at dawn and its place in the dawn order.
    Returns days (dawn days heard), median_min (None if never at dawn), rank
    and ranked (None unless it has ``min_days`` dawn days)."""
    order = insights.dawn_order(view, lat, lon, tz, min_days=1)
    row = order[order["common_name"] == name]
    if row.empty:
        return {"days": 0, "median_min": None, "rank": None, "ranked": None}
    ranked = order[order["days"] >= min_days].reset_index(drop=True)
    r = row.iloc[0]
    rank = None
    if r["days"] >= min_days:
        rank = int(ranked.index[ranked["common_name"] == name][0]) + 1
    return {"days": int(r["days"]), "median_min": float(r["median_min"]),
            "rank": rank, "ranked": len(ranked)}


# --- Calling pattern ------------------------------------------------------

def pattern_table(view, bout_gap_s=120, run_gap_s=600):
    """Per species: bouts (runs with gaps of ``bout_gap_s`` or less), median
    calls per bout, median bout length (minutes, bouts of 2+ calls), median gap
    between calls within runs (gaps of ``run_gap_s`` or less) and its
    coefficient of variation (lower = more regular)."""
    cols = ["common_name", "detections", "bouts", "calls_per_bout",
            "bout_minutes", "gap_s", "gap_cv"]
    if not len(view):
        return pd.DataFrame(columns=cols)
    v = view.sort_values(["common_name", "detected_at", "id"])
    t = pd.to_datetime(v["detected_at"])
    gap = t.groupby(v["common_name"]).diff().dt.total_seconds()
    bout = (gap.isna() | (gap > bout_gap_s)).cumsum()
    b = pd.DataFrame({"sp": v["common_name"].values, "bout": bout.values,
                      "t": t.values})
    per_bout = b.groupby(["sp", "bout"]).agg(
        calls=("t", "size"), start=("t", "min"), end=("t", "max"))
    per_bout["minutes"] = (per_bout["end"] - per_bout["start"]) \
        .dt.total_seconds() / 60
    g = per_bout.groupby(level="sp")
    runs = gap[gap <= run_gap_s]
    rg = runs.groupby(v.loc[runs.index, "common_name"])
    out = pd.DataFrame({
        "detections": v.groupby("common_name").size(),
        "bouts": g.size(),
        "calls_per_bout": g["calls"].median(),
        "bout_minutes": per_bout[per_bout["calls"] > 1]
        .groupby(level="sp")["minutes"].median(),
        "gap_s": rg.median(),
        "gap_cv": rg.std(ddof=0) / rg.mean(),
    })
    return out.rename_axis("common_name").reset_index()[cols]


def calling_pattern(view, name, bout_gap_s=120, run_gap_s=600, min_bouts=5):
    """This bird's calling pattern next to the typical bird's (the median
    over species with at least ``min_bouts`` bouts). Returns (bird, typical)
    dicts, or (None, typical) when the bird isn't in ``view``."""
    t = pattern_table(view, bout_gap_s, run_gap_s)
    metrics = ["calls_per_bout", "bout_minutes", "gap_s", "gap_cv"]
    typical = t[t["bouts"] >= min_bouts][metrics].median().to_dict()
    row = t[t["common_name"] == name]
    bird = None if row.empty else {k: row.iloc[0][k] for k in
                                   ["bouts"] + metrics}
    return bird, typical


# --- Company --------------------------------------------------------------

def companions(view, name, window="15min", top=6, min_shared=3, min_lift=1.25):
    """Species heard in the same ``window`` as this bird noticeably more often
    than chance, most over-represented first: shared windows, share of this
    bird's windows, and lift (that share over how often the species is heard
    in any window). Species shared fewer than ``min_shared`` times or with a
    lift under ``min_lift`` are left out, so an empty result means no species
    keeps it company more than usual."""
    cols = ["common_name", "shared", "share", "lift"]
    if not len(view):
        return pd.DataFrame(columns=cols)
    bucket = pd.to_datetime(view["detected_at"]).dt.floor(window)
    pairs = pd.DataFrame({"sp": view["common_name"].values,
                          "b": bucket.values}).drop_duplicates()
    mine = set(pairs.loc[pairs["sp"] == name, "b"])
    if not mine:
        return pd.DataFrame(columns=cols)
    total = pairs["b"].nunique()
    others = pairs[pairs["sp"] != name]
    shared = others[others["b"].isin(mine)].groupby("sp").size()
    overall = others.groupby("sp").size() / total
    out = pd.DataFrame({"shared": shared})
    out["share"] = out["shared"] / len(mine)
    out["lift"] = out["share"] / overall.reindex(out.index)
    out = out[(out["shared"] >= min_shared) & (out["lift"] >= min_lift)]
    return (out.sort_values(["lift", "shared"], ascending=False).head(top)
            .rename_axis("common_name").reset_index()[cols])


# --- Confidence -----------------------------------------------------------

def confidence_vs_all(view, name, step=0.05, low=0.25):
    """Confidence distribution (share per bin) for this bird and for all."""
    edges = np.round(np.arange(low, 1.0 + step, step), 2)
    edges[-1] = 1.0001

    def dist(c):
        h, _ = np.histogram(c, bins=edges)
        return h / max(h.sum(), 1)

    return pd.DataFrame({
        "bin_start": edges[:-1],
        "bird_share": dist(_of(view, name)["confidence"]),
        "all_share": dist(view["confidence"]),
    })


# --- What stands out ------------------------------------------------------

_PHRASES = {
    ("dawn", "high"): "a dawn singer",
    ("evening", "high"): "an evening singer",
    ("afternoon", "high"): "busiest in the afternoon",
    ("night", "high"): "often heard at night",
    ("days_share", "high"): "heard on most days",
    ("days_share", "low"): "seldom heard",
    ("confidence", "high"): "easy for the model to recognise",
    ("confidence", "low"): "hard for the model to recognise",
    ("calls_per_bout", "high"): "calls in long bouts",
    ("calls_per_bout", "low"): "calls in short bursts",
    ("gap_s", "low"): "calls in quick succession",
    ("gap_s", "high"): "spaces its calls out",
}


def features(view, raw, bout_gap_s=120, run_gap_s=600):
    """One row per species with enough detections to compare: time-of-day
    shares, share of listening days heard, median confidence and calling
    pattern."""
    counts = view["common_name"].value_counts()
    names = list(counts[counts >= MIN_DETECTIONS_TO_COMPARE].index)
    if not names:
        return pd.DataFrame()
    days_listened = max(len(insights.listening_days(raw)), 1)
    v = view[view["common_name"].isin(names)]
    t = pd.to_datetime(v["detected_at"])
    f = pd.DataFrame(index=pd.Index(names, name="common_name"))
    for p, s, e in PERIODS:
        f[p] = _in_period(t.dt.hour, s, e).groupby(v["common_name"]).mean()
    f["days_share"] = t.dt.date.groupby(v["common_name"]).nunique() / days_listened
    f["confidence"] = v.groupby("common_name")["confidence"].median()
    pt = pattern_table(v, bout_gap_s, run_gap_s).set_index("common_name")
    f["calls_per_bout"] = pt["calls_per_bout"]
    f["gap_s"] = pt["gap_s"]
    return f


def distinctive(view, raw, name, top=3, cut=0.1, **pattern_kw):
    """Up to ``top`` short phrases for what sets this bird apart from the
    others (features where it sits in the top or bottom ``cut`` of species).
    Returns (phrases, number of species compared); phrases is None when the
    bird has too few detections to compare."""
    f = features(view, raw, **pattern_kw)
    if name not in f.index or len(f) < 5:
        return None, len(f)
    found = []
    for col in f.columns:
        col_vals = f[col].dropna()
        if name not in col_vals.index or len(col_vals) < 5:
            continue
        x = col_vals[name]
        others = col_vals.drop(name)
        pct = float((others < x).mean())  # share of other species below it
        for side, extreme in (("high", pct >= 1 - cut), ("low", pct <= cut)):
            if extreme and (col, side) in _PHRASES:
                found.append((abs(pct - 0.5), _PHRASES[(col, side)]))
    found.sort(key=lambda p: -p[0])
    return [p for _, p in found[:top]], len(f)
