"""Analytics for the dashboard's Insights tab.

Pure pandas, no database and no Streamlit, so every number is unit-testable.
Each function takes ``view`` (the detections being shown: filtered or raw) and,
where coverage matters, ``raw`` (every detection, used to tell which days the
listener was actually running).

Coverage matters because the listener has been offline for weeks at a time
(late June to mid July, late September to early October). A day with no
detections at all is treated as a day the listener was down, not a silent day,
so presence is always "days heard out of days listened".

Times are naive local wall-clock times, as stored in the database.
"""
import datetime
import math
from zoneinfo import ZoneInfo

import pandas as pd

# Dawn-chorus window, relative to sunrise. Earlier calls are mostly night
# migrants passing over, which would make the chorus look like it starts hours
# before dawn; later ones are just the day going on.
DAWN_WINDOW_MIN = (-90, 240)

NIGHT_HOURS = (22, 4)  # 22:00 up to 04:00


# --- Sun ------------------------------------------------------------------

def sun_times(day, lat, lon, tz):
    """(sunrise, sunset) for ``day`` as naive local datetimes, or (None, None)
    when the sun doesn't rise or set (polar day/night).

    NOAA's solar calculator algorithm (Julian-century based), iterated so the
    sun's position is taken at the event itself: within about a minute of
    published times, which is plenty for comparing birdsong with dawn."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    midnight_utc = datetime.datetime(day.year, day.month, day.day,
                                     tzinfo=datetime.timezone.utc)
    jd_midnight = day.toordinal() + 1721424.5  # Julian day at 00:00 UTC
    rad, deg = math.radians, math.degrees

    def event_minutes(sign):
        minutes = 720.0  # start from solar noon, then refine
        for _ in range(3):
            t = (jd_midnight + minutes / 1440 - 2451545) / 36525
            l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
            m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
            e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
            c = (math.sin(rad(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
                 + math.sin(rad(2 * m)) * (0.019993 - 0.000101 * t)
                 + math.sin(rad(3 * m)) * 0.000289)
            omega = 125.04 - 1934.136 * t
            lam = l0 + c - 0.00569 - 0.00478 * math.sin(rad(omega))
            eps = (23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))
                         / 60) / 60 + 0.00256 * math.cos(rad(omega)))
            decl = math.asin(math.sin(rad(eps)) * math.sin(rad(lam)))
            y = math.tan(rad(eps / 2)) ** 2
            eqtime = 4 * deg(y * math.sin(2 * rad(l0)) - 2 * e * math.sin(rad(m))
                             + 4 * e * y * math.sin(rad(m)) * math.cos(2 * rad(l0))
                             - 0.5 * y * y * math.sin(4 * rad(l0))
                             - 1.25 * e * e * math.sin(2 * rad(m)))
            cos_ha = (math.cos(rad(90.833)) / (math.cos(rad(lat)) * math.cos(decl))
                      - math.tan(rad(lat)) * math.tan(decl))
            if not -1 <= cos_ha <= 1:
                return None
            minutes = 720 - 4 * (lon + sign * deg(math.acos(cos_ha))) - eqtime
        return minutes

    out = []
    for sign in (+1, -1):  # sunrise, sunset
        minutes = event_minutes(sign)
        if minutes is None:
            return None, None
        t = midnight_utc + datetime.timedelta(minutes=minutes)
        out.append(t.astimezone(zone).replace(tzinfo=None))
    return tuple(out)


def _clock_hours(ts):
    """Datetime(s) -> hours past local midnight as a float (for time-of-day axes)."""
    return ts.dt.hour + ts.dt.minute / 60 + ts.dt.second / 3600


# --- Coverage -------------------------------------------------------------

def listening_days(raw):
    """Sorted dates on which the listener recorded anything."""
    if raw is None or len(raw) == 0:
        return []
    return sorted(pd.to_datetime(raw["detected_at"]).dt.date.unique())


def _week_start(ts):
    """Monday of the week containing each timestamp, as a Timestamp."""
    ts = pd.to_datetime(ts)
    return (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.normalize()


def weeks_covered(raw):
    """Every Monday from the first to the last recorded week, with how many
    days the listener ran that week (0 = offline the whole week)."""
    days = listening_days(raw)
    if not days:
        return pd.DataFrame(columns=["week", "days_listened"])
    s = pd.Series(pd.to_datetime(days))
    per_week = _week_start(s).value_counts()
    weeks = pd.date_range(per_week.index.min(), per_week.index.max(), freq="7D")
    return pd.DataFrame({"week": weeks,
                         "days_listened": [int(per_week.get(w, 0)) for w in weeks]})


# --- Headline -------------------------------------------------------------

def headline(view, raw):
    """A few headline facts: days listened, species, the richest week and the
    most recent first arrival."""
    out = {"days_listened": len(listening_days(raw)),
           "species": int(view["common_name"].nunique()) if len(view) else 0,
           "peak_week": None, "peak_week_species": 0,
           "newest": None, "newest_date": None}
    if not len(view):
        return out
    wk = weekly_richness(view, raw)
    wk = wk[wk["species"] > 0]
    if len(wk):
        best = wk.sort_values(["species", "week"], ascending=[False, True]).iloc[0]
        out["peak_week"] = best["week"].date()
        out["peak_week_species"] = int(best["species"])
    first = view.groupby("common_name")["detected_at"].min()
    out["newest"] = first.idxmax()
    out["newest_date"] = first.max().date()
    return out


# --- Season ---------------------------------------------------------------

def weekly_richness(view, raw):
    """Species and detections per calendar week, with days listened, for every
    week in the recorded range (offline weeks included with zeros)."""
    weeks = weeks_covered(raw)
    if weeks.empty:
        return pd.DataFrame(columns=["week", "species", "detections",
                                     "days_listened"])
    v = view.assign(week=_week_start(view["detected_at"]))
    g = v.groupby("week").agg(species=("common_name", "nunique"),
                              detections=("id", "size"))
    out = weeks.merge(g, left_on="week", right_index=True, how="left")
    out[["species", "detections"]] = out[["species", "detections"]] \
        .fillna(0).astype(int)
    return out[["week", "species", "detections", "days_listened"]]


def presence_grid(view, raw, min_days=3):
    """Which species were heard in which weeks.

    One row per (species, week) for species heard on at least ``min_days``
    days, over every recorded week: days heard that week, days listened, and
    their ratio (``share``; None when the listener was off all week). Species
    are ordered by the date they were first heard (``order``), so arrivals read
    top to bottom."""
    cols = ["common_name", "week", "days_heard", "days_listened", "share",
            "first_heard", "order"]
    weeks = weeks_covered(raw)
    if weeks.empty or not len(view):
        return pd.DataFrame(columns=cols)
    v = view.assign(day=pd.to_datetime(view["detected_at"]).dt.normalize())
    days = v.groupby("common_name")["day"].nunique()
    keep = days[days >= min_days].index
    v = v[v["common_name"].isin(keep)]
    if not len(v):
        return pd.DataFrame(columns=cols)
    v = v.assign(week=_week_start(v["day"]))
    heard = v.groupby(["common_name", "week"])["day"].nunique() \
        .rename("days_heard").reset_index()
    first = v.groupby("common_name")["day"].min().rename("first_heard")
    species = first.sort_values().index
    grid = pd.MultiIndex.from_product([species, weeks["week"]],
                                      names=["common_name", "week"]).to_frame(index=False)
    grid = grid.merge(weeks, on="week").merge(heard, on=["common_name", "week"],
                                              how="left")
    grid["days_heard"] = grid["days_heard"].fillna(0).astype(int)
    grid["share"] = (grid["days_heard"] / grid["days_listened"]).where(
        grid["days_listened"] > 0)
    grid = grid.merge(first.reset_index(), on="common_name")
    grid["order"] = grid["common_name"].map({s: i for i, s in enumerate(species)})
    return grid.sort_values(["order", "week"]).reset_index(drop=True)[cols]


# --- Dawn -----------------------------------------------------------------

def _with_sunrise(view, lat, lon, tz):
    v = view.assign(day=pd.to_datetime(view["detected_at"]).dt.date)
    rise = {d: sun_times(d, lat, lon, tz)[0] for d in v["day"].unique()}
    v = v.assign(sunrise=pd.to_datetime(v["day"].map(rise)))
    v = v.assign(min_from_sunrise=(v["detected_at"] - v["sunrise"])
                 .dt.total_seconds() / 60)
    lo, hi = DAWN_WINDOW_MIN
    return v[(v["min_from_sunrise"] >= lo) & (v["min_from_sunrise"] <= hi)]


def dawn_chorus(view, lat, lon, tz):
    """The first call of each day in the dawn window, next to that day's
    sunrise. Columns: day, first_call, sunrise, species, min_from_sunrise,
    first_call_h, sunrise_h (clock hours, for a time-of-day axis)."""
    cols = ["day", "first_call", "sunrise", "species", "min_from_sunrise",
            "first_call_h", "sunrise_h"]
    if not len(view):
        return pd.DataFrame(columns=cols)
    v = _with_sunrise(view, lat, lon, tz)
    if not len(v):
        return pd.DataFrame(columns=cols)
    first = v.sort_values(["detected_at", "id"]).groupby("day").head(1)
    out = pd.DataFrame({
        "day": pd.to_datetime(first["day"]).values,
        "first_call": first["detected_at"].values,
        "sunrise": first["sunrise"].values,
        "species": first["common_name"].values,
        "min_from_sunrise": first["min_from_sunrise"].round(0).values,
    })
    out["first_call_h"] = _clock_hours(out["first_call"])
    out["sunrise_h"] = _clock_hours(out["sunrise"])
    return out.sort_values("day").reset_index(drop=True)[cols]


def dawn_by_month(chorus):
    """Median minutes from sunrise to the first call, per calendar month."""
    if chorus.empty:
        return pd.DataFrame(columns=["month", "median_min", "days"])
    m = chorus.assign(month=chorus["day"].dt.to_period("M").dt.start_time)
    return (m.groupby("month")
            .agg(median_min=("min_from_sunrise", "median"),
                 days=("day", "size"))
            .reset_index())


def dawn_order(view, lat, lon, tz, min_days=5):
    """For each species, the median minutes from sunrise to its first call of
    the day, over days it called in the dawn window. Species with fewer than
    ``min_days`` such days are left out. Earliest first."""
    cols = ["common_name", "median_min", "days", "q1_min", "q3_min"]
    if not len(view):
        return pd.DataFrame(columns=cols)
    v = _with_sunrise(view, lat, lon, tz)
    per_day = v.groupby(["common_name", "day"])["min_from_sunrise"].min()
    g = per_day.groupby(level="common_name")
    out = pd.DataFrame({"median_min": g.median(), "days": g.size(),
                        "q1_min": g.quantile(0.25), "q3_min": g.quantile(0.75)})
    out = out[out["days"] >= min_days].round(0)
    return out.sort_values("median_min").reset_index()[cols]


# --- Daily rhythm ---------------------------------------------------------

def daily_rhythm(view, top=12):
    """For the ``top`` most-detected species, the share of each species'
    detections falling in each hour of the day (rows sum to 1), so a quiet
    species' pattern is as visible as a loud one's."""
    cols = ["common_name", "hour", "detections", "share", "rank"]
    if not len(view):
        return pd.DataFrame(columns=cols)
    counts = view["common_name"].value_counts()
    species = list(counts.head(top).index)
    v = view[view["common_name"].isin(species)]
    hours = pd.to_datetime(v["detected_at"]).dt.hour
    g = v.groupby([v["common_name"], hours.rename("hour")]).size()
    grid = pd.MultiIndex.from_product([species, range(24)],
                                      names=["common_name", "hour"])
    g = g.reindex(grid, fill_value=0).rename("detections").reset_index()
    g["share"] = g["detections"] / g.groupby("common_name")["detections"] \
        .transform("sum")
    g["rank"] = g["common_name"].map({s: i for i, s in enumerate(species)})
    return g[cols]


def night_share(view):
    """Fraction of detections between 22:00 and 04:00."""
    if not len(view):
        return 0.0
    h = pd.to_datetime(view["detected_at"]).dt.hour
    start, end = NIGHT_HOURS
    return float(((h >= start) | (h < end)).mean())
