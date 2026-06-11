"""Birdwatcher dashboard (Streamlit) — live feed, daily stats, and life list.

Reads the shared PostgreSQL database written by the microphone listener, so it
works both locally and when hosted on Posit Connect Cloud (Streamlit framework,
primary file = this file).

Run locally with:
    streamlit run streamlit_app.py
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

# Ensure local modules (config, db) are importable regardless of how the host
# launches this script. Posit Connect Cloud doesn't always put the project root
# on sys.path the way a local `streamlit run` does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import config
import db

st.set_page_config(
    page_title="The Ramble Register",
    page_icon="\U0001F426",
    layout="wide",
)

# "Central Park Morning" styling: clean sans type, robin's-egg header band,
# meadow-green accents, and softly-carded metrics. The colour theme itself lives
# in .streamlit/config.toml; this just adds the finishing polish.
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    .stApp, section[data-testid="stSidebar"] { font-family: 'Inter', system-ui, sans-serif; }

    .ramble-header {
        background: linear-gradient(135deg, #eaf4f7 0%, #dcecef 55%, #d2eade 100%);
        border: 1px solid #cfe3e8;
        border-radius: 16px;
        padding: 20px 26px;
        margin-bottom: 6px;
    }
    .ramble-kicker {
        color: #4f93a6; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.16em; font-size: 0.72rem;
    }
    .ramble-title {
        font-size: 2.1rem; font-weight: 700; color: #1d3b32;
        letter-spacing: -0.02em; margin: 2px 0 0 0;
    }
    .ramble-sub { color: #3f6b5e; font-size: 0.98rem; margin-top: 4px; }
    .ramble-note { color: #6a8a7f; font-size: 0.86rem; margin-top: 6px; }

    div[data-testid="stMetric"] {
        background: #ffffff; border: 1px solid #e2eef0;
        border-left: 4px solid #4c9a6a; border-radius: 12px;
        padding: 14px 16px; box-shadow: 0 1px 3px rgba(31,61,52,0.05);
    }
    div[data-testid="stMetricLabel"] p { color: #5a7d72; font-weight: 600; }
    button[data-baseweb="tab"] { font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Best-effort table creation. On an unreachable/read-only host (or before any
# detections exist) this may fail; the reads below tolerate that and render an
# empty dashboard instead of erroring.
try:
    db.init_db()
except SQLAlchemyError:
    pass


# --- Data access (tolerant of an empty/unreachable DB) -------------------

def _read(fn, *args, default=None):
    try:
        conn = db.get_conn()
    except SQLAlchemyError:
        return default
    try:
        return fn(conn, *args)
    except SQLAlchemyError:
        return default
    finally:
        conn.close()


# Short-TTL caches so the auto-refresh doesn't hammer the DB and tab-switching
# stays snappy. 10s lines up with the live-feed refresh cadence.
@st.cache_data(ttl=10, show_spinner=False)
def load_overview():
    return _read(db.overview, default={"total": 0, "species": 0,
                                       "first_at": None, "last_at": None})


@st.cache_data(ttl=10, show_spinner=False)
def load_life_list():
    return _read(db.life_list, default=[]) or []


@st.cache_data(ttl=10, show_spinner=False)
def load_days():
    return _read(db.distinct_days, default=[]) or []


@st.cache_data(ttl=10, show_spinner=False)
def load_counts(day):
    return _read(db.counts_for_day, day, default=[]) or []


@st.cache_data(ttl=10, show_spinner=False)
def load_hourly(day):
    return _read(db.hourly_counts_for_day, day, default=[0] * 24) or [0] * 24


@st.cache_data(ttl=10, show_spinner=False)
def load_trend(days):
    return _read(db.detections_per_day, days, default=[]) or []


def load_recent(limit=100):
    # Not cached — this is the live feed; we always want the freshest rows.
    return _read(db.recent_detections, limit, default=[]) or []


def load_status():
    # Not cached — the heartbeat freshness is the whole point of this read.
    return _read(db.listener_status, default=None)


@st.cache_data(ttl=86400, show_spinner=False)
def species_photo(common_name, scientific_name):
    """Thumbnail URL for a species from Wikipedia, or None. Cached a day.
    Tries the common name first (more likely to have a page), then the
    scientific name; any failure just yields no photo."""
    for title in (common_name, scientific_name):
        if not title:
            continue
        try:
            url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote(title.replace(" ", "_")))
            req = urllib.request.Request(
                url, headers={"User-Agent": "uws_birdwatcher/1.0 (hobby project)"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.load(resp)
            thumb = (data.get("thumbnail") or {}).get("source")
            if thumb:
                return thumb
        except Exception:
            continue
    return None


# --- Formatting helpers --------------------------------------------------

def _pct(x):
    try:
        return f"{int(x * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_time(v):
    if isinstance(v, datetime.datetime):
        return v.strftime("%H:%M:%S")
    return str(v)


def _fmt_date(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%b %d, %Y")
    return str(v)


def _as_date(v):
    if isinstance(v, datetime.datetime):
        return v.date()
    return v


def _humanize_age(seconds):
    """A compact 'time ago' string: 8s, 5m, 2h, 3d."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _local_clip(name):
    """Absolute path to a clip if it exists on *this* machine, else None.
    Clips live only where they were recorded, so a cloud host has none."""
    if not name:
        return None
    path = os.path.join(config.CLIPS_DIR, os.path.basename(name))
    return path if os.path.isfile(path) else None


def _clip_src(r):
    """A playable audio source for a detection. Prefer the public S3 URL — it
    works anywhere, including the cloud-hosted dashboard — and fall back to the
    local file on the recording machine. None when neither is available (e.g. a
    clip withheld for privacy, or one not yet uploaded)."""
    return r.get("clip_url") or _local_clip(r.get("clip_path"))


# --- Sidebar -------------------------------------------------------------

st.sidebar.title("\U0001F426 The Ramble Register")
st.sidebar.caption(f"Upper West Side, NYC · {config.LATITUDE}, {config.LONGITUDE}")
live = st.sidebar.checkbox("Live updates (10s)", value=True, key="live")
show_photos = st.sidebar.checkbox("Show bird photos", value=True, key="photos")
if st.sidebar.button("Refresh now"):
    st.cache_data.clear()
    st.rerun()

ov = load_overview()
if ov.get("last_at"):
    st.sidebar.caption(f"Last detection: {_fmt_date(ov['last_at'])} "
                       f"{_fmt_time(ov['last_at'])}")

with st.sidebar.expander("\U0001F4CD The listening post", expanded=False):
    st.map(
        pd.DataFrame({"lat": [config.LATITUDE], "lon": [config.LONGITUDE]}),
        zoom=12, size=60, color="#4c9a6a",
    )
    st.caption("A window over the Upper West Side — Central Park's Ramble to the "
               "east, Riverside Park to the west.")

_REFRESH = 10 if live else None


def _chorus_note():
    """A little field-guide flavour that changes with the hour."""
    h = config.now_local().hour
    if 5 <= h < 9:
        return "Dawn chorus — the busiest singing of the day."
    if 9 <= h < 12:
        return "Late morning — still good listening."
    if 12 <= h < 17:
        return "Afternoon lull — fewer songs, but keep an ear out."
    if 17 <= h < 20:
        return "Evening chorus — a second daily peak."
    return "After dark — owls, and nocturnal migrants passing overhead."


# --- Header: summary metric cards ----------------------------------------

@st.fragment(run_every=_REFRESH)
def header_metrics():
    ov = load_overview()
    life = load_life_list()
    today = config.today_local().isoformat()
    today_total = sum(r["n"] for r in load_counts(today))

    cutoff = config.today_local() - datetime.timedelta(days=6)
    new_week = sum(1 for r in life if _as_date(r["first_seen"]) >= cutoff)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total detections", f"{ov['total']:,}")
    c2.metric("Species (life list)", ov["species"])
    c3.metric("Detections today", today_total)
    c4.metric("New species this week", new_week)


st.markdown(
    f"""
    <div class="ramble-header">
        <div class="ramble-kicker">Upper West Side · NYC</div>
        <div class="ramble-title">\U0001F426 The Ramble Register</div>
        <div class="ramble-sub">Birdsong heard from a backyard on the
            Upper West Side — Central Park &amp; Riverside, NYC.</div>
        <div class="ramble-note">{_chorus_note()}</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.write("")
header_metrics()


@st.fragment(run_every=_REFRESH)
def listener_health():
    """A traffic-light pill telling apart three failure modes: process down
    (no recent heartbeat), mic dead (heartbeat fine but audio level ~0), and
    a healthy-but-quiet listener."""
    s = load_status()
    seg = getattr(config, "SEGMENT_SECONDS", 15)
    stale_after = max(60, seg * 4)  # a couple of missed segments = trouble
    now = config.now_local()

    if not s or not s.get("updated_at"):
        bg, border, icon = "#eef1f2", "#c4cdd1", "\U000026AA"
        msg = ("Listener status unknown — no heartbeat recorded yet. Start the "
               "listener on the recording machine.")
    else:
        age = (now - s["updated_at"]).total_seconds()
        peak = s.get("last_peak") or 0.0
        last_det = s.get("last_detection_at")
        det_txt = (f" · last bird {_humanize_age((now - last_det).total_seconds())} ago"
                   if last_det else "")
        if age > stale_after:
            bg, border, icon = "#fbecec", "#c0504d", "\U0001F534"
            msg = (f"Listener offline — no heartbeat for {_humanize_age(age)}. "
                   "It may have crashed, or the machine is asleep.")
        elif peak < 1e-4:
            bg, border, icon = "#fdf6e3", "#d99a2b", "\U0001F7E1"
            msg = (f"Listener running, but the mic is silent (level {peak:.4f}) — "
                   "check that the microphone is plugged in and unmuted.")
        else:
            bg, border, icon = "#eaf7ef", "#4c9a6a", "\U0001F7E2"
            msg = (f"Listening — heartbeat {_humanize_age(age)} ago · "
                   f"mic level {peak:.3f}{det_txt}.")

    st.markdown(
        f"""
        <div style="background:{bg};border:1px solid {border};
            border-left:4px solid {border};border-radius:10px;
            padding:9px 14px;margin-top:8px;font-size:0.9rem;color:#1d3b32;">
            {icon}&nbsp; {msg}
        </div>
        """,
        unsafe_allow_html=True,
    )


listener_health()
st.divider()


# --- Reusable table builders ---------------------------------------------

def _species_table(records, extra_config=None):
    df = pd.DataFrame(records)
    colcfg = {
        "Photo": st.column_config.ImageColumn("", width="small"),
    }
    if extra_config:
        colcfg.update(extra_config)
    if not show_photos and "Photo" in df.columns:
        df = df.drop(columns=["Photo"])
        colcfg.pop("Photo", None)
    st.dataframe(df, hide_index=True, width="stretch", column_config=colcfg)


def _conf_col(label="Confidence"):
    return {label: st.column_config.ProgressColumn(
        label, min_value=0, max_value=100, format="%d%%")}


# --- Live-feed cards + clip voting ---------------------------------------

# We pull a generous window of recent rows, then collapse back-to-back repeats of
# the same species into single cards (a resident bird can otherwise fill the whole
# feed). _FEED_FETCH is how many raw detections we read; _FEED_CARDS caps how many
# collapsed cards we render (each mounts an audio element and two buttons).
_FEED_FETCH = 150
_FEED_CARDS = 30


def _group_consecutive(rows):
    """Collapse back-to-back detections of the same species into one group.

    ``rows`` is newest-first (as ``db.recent_detections`` returns them). Each
    maximal run of consecutive same-species rows becomes one group whose
    representative is the run's *highest-confidence* detection — that clip,
    confidence and id are what the card shows and what votes apply to. A species
    that goes quiet and returns later forms a separate group, so the feed stays a
    true chronological timeline rather than an all-time leaderboard.

    Returns a list of dicts: {key, rep (row), count, start, end (datetimes)}."""
    groups = []
    for r in rows:
        key = r.get("scientific_name") or r.get("common_name")
        ts = r.get("detected_at")
        if groups and groups[-1]["key"] == key:
            g = groups[-1]
            g["count"] += 1
            if ts is not None:
                if g["start"] is None or ts < g["start"]:
                    g["start"] = ts
                if g["end"] is None or ts > g["end"]:
                    g["end"] = ts
            if (r.get("confidence") or 0) > (g["rep"].get("confidence") or 0):
                g["rep"] = r
        else:
            groups.append({"key": key, "rep": r, "count": 1,
                           "start": ts, "end": ts})
    return groups


def _group_time_label(group):
    """Time text for a group's card: a single time for one detection, or an
    earliest–latest range when consecutive repeats were collapsed."""
    if group["count"] > 1 and group["start"] is not None and group["end"] is not None:
        a, b = _fmt_time(group["start"]), _fmt_time(group["end"])
        return a if a == b else f"{a}–{b}"
    return _fmt_time(group["rep"].get("detected_at"))

# A stable per-browser-session token so a visitor's repeated votes overwrite
# their own earlier vote instead of stacking. Not a login — just enough to avoid
# accidental double-counting within a session.
if "voter" not in st.session_state:
    st.session_state.voter = uuid.uuid4().hex
if "vote_error" not in st.session_state:
    st.session_state.vote_error = False


def _cast_vote(detection_id, vote, current):
    """Record or withdraw a vote. Clicking the choice you already made withdraws
    it (vote 0). Sets a flag instead of crashing if the DB user can't write."""
    new = 0 if current == vote else vote
    try:
        db.record_vote(detection_id, st.session_state.voter, new)
        st.session_state.vote_error = False
    except SQLAlchemyError:
        st.session_state.vote_error = True  # read-only DB user, almost always


def _detection_card(group, tally, mine):
    """One species group as a bordered card: photo, name (with a ×N badge when
    consecutive repeats were collapsed), the time/best-confidence line, the
    representative clip, and up/down vote buttons with a 'Disputed' flag past the
    threshold. Votes apply to the run's highest-confidence detection."""
    r = group["rep"]
    count = group["count"]
    up, down = tally.get("up", 0), tally.get("down", 0)
    disputed = (up + down) > 0 and (up - down) <= config.DISPUTED_THRESHOLD
    did = r.get("id")
    with st.container(border=True):
        if show_photos:
            pcol, head = st.columns([1, 5], vertical_alignment="center")
            photo = species_photo(r["common_name"], r["scientific_name"])
            if photo:
                pcol.image(photo, width=72)
        else:
            head = st.container()
        title = f"#### \U0001F426 {r['common_name']}"
        if count > 1:
            title += f"  `×{count}`"
        head.markdown(title)
        conf = int(r["confidence"] * 100)
        meta = (f"{_group_time_label(group)} · "
                f"{conf}% confidence{' (best)' if count > 1 else ''}")
        if disputed:
            meta += "  ·  :red[**Disputed ⚠**]"
        head.caption(meta)

        src = _clip_src(r)
        if src:
            st.audio(src)
        else:
            st.caption("_clip unavailable_")

        c_up, c_down, _rest = st.columns([1, 1, 4])
        if c_up.button(f"\U0001F44D {up}", key=f"up_{did}", width="stretch",
                       type="primary" if mine == 1 else "secondary",
                       help="This clip matches the species"):
            _cast_vote(did, 1, mine)
            st.rerun(scope="fragment")
        if c_down.button(f"\U0001F44E {down}", key=f"down_{did}", width="stretch",
                         type="primary" if mine == -1 else "secondary",
                         help="This clip doesn't match the species"):
            _cast_vote(did, -1, mine)
            st.rerun(scope="fragment")


# --- Tabs ----------------------------------------------------------------

feed_tab, stats_tab, life_tab = st.tabs(["Live feed", "Daily stats", "Life list"])

with feed_tab:
    st.subheader("Live feed")

    st.caption("Back-to-back repeats of the same species are grouped into one "
               "card (best clip shown). Tap \U0001F44D / \U0001F44E to tell us "
               "whether a clip matches the bird.")

    @st.fragment(run_every=_REFRESH)
    def live_feed():
        rows = load_recent(_FEED_FETCH)
        if not rows:
            st.info("No detections yet. Start the listener "
                    "(`python -m capture.listen`) and play some birdsong near "
                    "the mic.")
            return
        groups = _group_consecutive(rows)[:_FEED_CARDS]
        ids = [g["rep"].get("id") for g in groups]
        tallies = _read(db.vote_tallies, ids, default={}) or {}
        mine = _read(db.my_votes, st.session_state.voter, ids, default={}) or {}
        if st.session_state.vote_error:
            st.warning("Voting is unavailable — the dashboard's database user "
                       "is read-only.")
        for g in groups:
            did = g["rep"].get("id")
            _detection_card(g, tallies.get(did, {}), mine.get(did, 0))

    live_feed()

with stats_tab:
    st.subheader("Daily stats")
    days = load_days()
    today = config.today_local().isoformat()
    options = [today] + [d for d in days if d != today]
    day = st.selectbox("Day", options, index=0)

    counts = load_counts(day)
    hourly = load_hourly(day)
    total = sum(r["n"] for r in counts)

    st.write(f"**{total}** detection{'' if total == 1 else 's'} on {day} "
             f"across **{len(counts)}** species.")

    st.markdown("**Activity by hour**")
    hourly_df = pd.DataFrame({"Detections": hourly},
                             index=[f"{h:02d}" for h in range(24)])
    hourly_df.index.name = "Hour"
    st.bar_chart(hourly_df, color="#4c9a6a")

    st.markdown("**Detections — last 14 days**")
    trend = load_trend(14)
    end = config.today_local()
    span = [end - datetime.timedelta(days=i) for i in range(13, -1, -1)]
    by_day = {_as_date(r["day"]): r["n"] for r in trend}
    trend_df = pd.DataFrame({"Detections": [by_day.get(d, 0) for d in span]},
                            index=[d.strftime("%m-%d") for d in span])
    trend_df.index.name = "Day"
    st.bar_chart(trend_df, color="#6fb6c9")

    st.markdown("**Species this day**")
    if counts:
        _species_table(
            [
                {
                    "Photo": species_photo(r["common_name"], r["scientific_name"])
                    if show_photos else None,
                    "Species": r["common_name"],
                    "Scientific name": r["scientific_name"],
                    "Count": r["n"],
                    "Best confidence": int(r["best"] * 100),
                }
                for r in counts
            ],
            _conf_col("Best confidence"),
        )
    else:
        st.info("No detections on this day.")

with life_tab:
    st.subheader("Life list")
    rows = load_life_list()
    st.caption(f"{len(rows)} species heard")
    if rows:
        _species_table(
            [
                {
                    "Photo": species_photo(r["common_name"], r["scientific_name"])
                    if show_photos else None,
                    "Species": r["common_name"],
                    "Scientific name": r["scientific_name"],
                    "First heard": _fmt_date(r["first_seen"]),
                    "Last heard": _fmt_date(r["last_seen"]),
                    "Times": r["total"],
                    "Best": int(r["best"] * 100),
                }
                for r in rows
            ],
            _conf_col("Best"),
        )
    else:
        st.info("No species heard yet.")
