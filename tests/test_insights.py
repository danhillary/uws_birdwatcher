"""Unit tests for the Insights-tab analytics (no database needed)."""
import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import insights  # noqa: E402

NYC = (40.785, -73.975, "America/New_York")


def make(rows):
    """rows: ('YYYY-MM-DD HH:MM:SS', species[, confidence])."""
    return pd.DataFrame([
        {"id": i, "detected_at": pd.Timestamp(r[0]), "common_name": r[1],
         "confidence": r[2] if len(r) > 2 else 0.9}
        for i, r in enumerate(rows, start=1)])


def minutes(t):
    return t.hour * 60 + t.minute


# --- Sun -------------------------------------------------------------------

@pytest.mark.parametrize("day, rise, sets", [
    # NOAA solar calculator, Central Park, local clock (EDT/EST).
    (datetime.date(2026, 6, 21), "05:25", "20:31"),
    (datetime.date(2026, 10, 12), "07:04", "18:20"),
    (datetime.date(2026, 12, 21), "07:17", "16:31"),
    (datetime.date(2026, 3, 20), "06:59", "19:08"),  # equinox, during DST
])
def test_sun_times_match_published_nyc_times(day, rise, sets):
    sunrise, sunset = insights.sun_times(day, *NYC)
    for got, want in ((sunrise, rise), (sunset, sets)):
        h, m = map(int, want.split(":"))
        assert abs(minutes(got) - (h * 60 + m)) <= 1, (day, got, want)


def test_sun_times_polar_night():
    assert insights.sun_times(datetime.date(2026, 12, 21), 80.0, 0.0, "UTC") == (None, None)


# --- Coverage and season ---------------------------------------------------

RAW = make([
    ("2026-06-08 06:00:00", "A"), ("2026-06-09 06:00:00", "A"),
    ("2026-06-10 06:00:00", "B"),
    # listener off the week of Jun 15
    ("2026-06-22 06:00:00", "A"), ("2026-06-22 07:00:00", "C"),
    ("2026-06-23 06:00:00", "C"), ("2026-06-24 06:00:00", "C"),
])


def test_weeks_covered_includes_offline_weeks():
    w = insights.weeks_covered(RAW)
    assert list(w["week"].dt.strftime("%m-%d")) == ["06-08", "06-15", "06-22"]
    assert list(w["days_listened"]) == [3, 0, 3]


def test_weekly_richness_counts_species_per_week():
    w = insights.weekly_richness(RAW, RAW).set_index(
        insights.weeks_covered(RAW)["week"].dt.strftime("%m-%d"))
    assert w.loc["06-08", "species"] == 2 and w.loc["06-08", "detections"] == 3
    assert w.loc["06-15", "species"] == 0 and w.loc["06-15", "days_listened"] == 0
    assert w.loc["06-22", "species"] == 2


def test_presence_grid_shares_and_arrival_order():
    g = insights.presence_grid(RAW, RAW, min_days=1)
    # A first heard Jun 8, B Jun 10, C Jun 22: arrival order.
    order = g.drop_duplicates("common_name").sort_values("order")["common_name"]
    assert list(order) == ["A", "B", "C"]
    cell = g[(g["common_name"] == "C")
             & (g["week"] == pd.Timestamp("2026-06-22"))].iloc[0]
    assert (cell["days_heard"], cell["days_listened"], cell["share"]) == (3, 3, 1.0)
    offline = g[g["week"] == pd.Timestamp("2026-06-15")]
    assert offline["share"].isna().all()  # unknown, not zero


def test_presence_grid_min_days():
    g = insights.presence_grid(RAW, RAW, min_days=2)
    assert set(g["common_name"]) == {"A", "C"}


def test_headline():
    h = insights.headline(RAW, RAW)
    assert h["days_listened"] == 6
    assert h["species"] == 3
    assert h["newest"] == "C" and h["newest_date"] == datetime.date(2026, 6, 22)
    assert h["peak_week_species"] == 2
    assert h["peak_week"] == datetime.date(2026, 6, 8)  # ties: earliest week


# --- Dawn ------------------------------------------------------------------

def test_dawn_chorus_first_call_in_window():
    # Sunrise Jun 21 ~05:25. A 02:00 night migrant is outside the window; the
    # first dawn call is the 05:10 cardinal (~15 min before sunrise).
    view = make([("2026-06-21 02:00:00", "Night migrant"),
                 ("2026-06-21 05:10:00", "Cardinal"),
                 ("2026-06-21 05:40:00", "Robin")])
    ch = insights.dawn_chorus(view, *NYC)
    assert len(ch) == 1
    row = ch.iloc[0]
    assert row["species"] == "Cardinal"
    assert -17 <= row["min_from_sunrise"] <= -12
    assert abs(row["first_call_h"] - (5 + 10 / 60)) < 1e-6


def test_dawn_order_needs_min_days_and_sorts_earliest_first():
    rows = []
    for d in range(1, 6):
        rows.append((f"2026-06-{d:02d} 05:20:00", "Early"))
        rows.append((f"2026-06-{d:02d} 07:00:00", "Late"))
    rows.append(("2026-06-01 05:00:00", "Rare"))
    o = insights.dawn_order(make(rows), *NYC, min_days=5)
    assert list(o["common_name"]) == ["Early", "Late"]
    assert o["days"].tolist() == [5, 5]


def test_dawn_by_month():
    ch = insights.dawn_chorus(make([("2026-06-21 05:10:00", "A"),
                                    ("2026-06-22 05:20:00", "A"),
                                    ("2026-09-01 07:00:00", "A")]), *NYC)
    m = insights.dawn_by_month(ch)
    assert m["days"].tolist() == [2, 1]


# --- Daily rhythm ----------------------------------------------------------

def test_daily_rhythm_rows_sum_to_one_and_keep_top():
    view = make([("2026-06-21 06:00:00", "A"), ("2026-06-21 06:30:00", "A"),
                 ("2026-06-21 18:00:00", "A"), ("2026-06-21 12:00:00", "B")])
    r = insights.daily_rhythm(view, top=1)
    assert set(r["common_name"]) == {"A"}
    assert len(r) == 24
    assert r["share"].sum() == pytest.approx(1.0)
    assert r.set_index("hour").at[6, "share"] == pytest.approx(2 / 3)


def test_night_share():
    view = make([("2026-06-21 23:00:00", "A"), ("2026-06-21 03:59:00", "A"),
                 ("2026-06-21 04:00:00", "A"), ("2026-06-21 12:00:00", "A")])
    assert insights.night_share(view) == 0.5


def test_empty_inputs():
    empty = make([]).reindex(columns=["id", "detected_at", "common_name",
                                      "confidence"])
    assert insights.weekly_richness(empty, empty).empty
    assert insights.presence_grid(empty, empty).empty
    assert insights.dawn_chorus(empty, *NYC).empty
    assert insights.dawn_order(empty, *NYC).empty
    assert insights.daily_rhythm(empty).empty
    assert insights.headline(empty, empty)["species"] == 0
