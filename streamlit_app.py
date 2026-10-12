"""Birdwatcher dashboard (Streamlit) — live feed, daily stats, life list,
bird profiles, seasonal and daily insights, and the false-positive filter
report.

Reads the shared PostgreSQL database written by the microphone listener, so it
works both locally and when hosted on Posit Connect Cloud (Streamlit framework,
primary file = this file).

Every view except the live feed reads one cached copy of all detections and
runs the false-positive filters (filters.py) on it in-process, so the raw vs
filtered switch, per-test toggles and admin threshold sliders respond
instantly. The live feed reads the newest rows straight from the database.

Run locally with:
    streamlit run streamlit_app.py
"""
import datetime
import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# Ensure local modules (config, db) are importable regardless of how the host
# launches this script. Posit Connect Cloud doesn't always put the project root
# on sys.path the way a local `streamlit run` does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import acoustics
import bird_profile
import config
import db
import filter_refresh
import filters
import insights

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

# Best-effort table creation, once per server process (it is a dozen round
# trips, too slow to repeat on every interaction). On an unreachable/read-only
# host (or before any detections exist) this may fail; the reads below tolerate
# that and render an empty dashboard instead of erroring.
@st.cache_resource(show_spinner=False)
def _init_db_once():
    try:
        db.init_db()
    except SQLAlchemyError:
        pass


_init_db_once()


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


# Every view except the live feed works from one copy of all detections,
# refreshed once a minute (the live feed below is uncached and always fresh).
# The filters run in-process on that copy, cached per filter setting.
_EMPTY_DETECTIONS = pd.DataFrame({
    "id": pd.Series(dtype="int64"),
    "detected_at": pd.Series(dtype="datetime64[ns]"),
    "common_name": pd.Series(dtype=object),
    "scientific_name": pd.Series(dtype=object),
    "confidence": pd.Series(dtype="float64"),
    "clip_path": pd.Series(dtype=object),
    "clip_url": pd.Series(dtype=object),
})


@st.cache_data(ttl=5, show_spinner=False)
def load_data_version():
    """The newest detection id: a cheap query that every filtered view keys
    its caches on, so a new detection is loaded and judged within seconds."""
    return _read(db.latest_detection_id, default=0) or 0


@st.cache_data(ttl=3600, max_entries=3, show_spinner=False)
def load_detections(version):
    """All detections as of ``version`` (the newest id); see load_data_version."""
    df = _read(db.all_detections, default=None)
    return _EMPTY_DETECTIONS.copy() if df is None or df.empty else df


@st.cache_data(ttl=60, show_spinner=False)
def load_stored_config():
    """Thresholds saved in the database, with defaults for anything unsaved."""
    return filters.resolve_config(_read(db.filter_config, default={}) or {})


@st.cache_data(ttl=60, show_spinner=False)
def load_overrides():
    return _read(db.species_overrides, default=[]) or []


@st.cache_data(ttl=60, show_spinner=False)
def load_last_refresh():
    return _read(db.last_filter_refresh, default=None)


@st.cache_data(ttl=3600, max_entries=20, show_spinner=False)
def compute_filters(cfg_items, enabled, override_items, version):
    """filters.compute on the cached detections, keyed by the filter setting
    and the data version."""
    return filters.compute(load_detections(version), dict(cfg_items), enabled,
                           dict(override_items))


def load_recent(limit=100):
    # Not cached — this is the live feed; we always want the freshest rows.
    return _read(db.recent_detections, limit, default=[]) or []


def load_status():
    # Not cached — the heartbeat freshness is the whole point of this read.
    return _read(db.listener_status, default=None)


def _wiki_json(path_title, endpoint="summary"):
    """Fetch a Wikipedia REST JSON document for a page title, or {} on any
    failure. ``endpoint`` is 'summary' (page text/thumbnail/link) or
    'media-list' (images and audio on the page)."""
    url = (f"https://en.wikipedia.org/api/rest_v1/page/{endpoint}/"
           + urllib.parse.quote(path_title.replace(" ", "_")))
    req = urllib.request.Request(
        url, headers={"User-Agent": "uws_birdwatcher/1.0 (hobby project)"}
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.load(resp)


@st.cache_data(ttl=86400, show_spinner=False)
def species_photo(common_name, scientific_name):
    """Thumbnail URL for a species from Wikipedia, or None. Cached a day.
    Tries the common name first (more likely to have a page), then the
    scientific name; any failure just yields no photo."""
    for title in (common_name, scientific_name):
        if not title:
            continue
        try:
            data = _wiki_json(title)
            thumb = (data.get("thumbnail") or {}).get("source")
            if thumb:
                return thumb
        except Exception:
            continue
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def species_reference(common_name, scientific_name):
    """A Wikipedia page link and a reference song recording for a species, so a
    voter can compare our clip against a known example before voting. Returns
    {"page": url|None, "audio": url|None}; any failure yields empty fields.
    Cached a day.

    The audio is whatever recording the species' Wikipedia page carries
    (typically a Xeno-canto clip hosted on Wikimedia Commons). It's usually Ogg,
    which plays in Chrome/Firefox/Android; Safari/iOS users can use the link
    instead. We resolve the page's File: title to a playable URL via Wikimedia's
    Special:FilePath redirect."""
    out = {"page": None, "audio": None}
    for title in (common_name, scientific_name):
        if not title:
            continue
        try:
            if not out["page"]:
                summ = _wiki_json(title, "summary")
                out["page"] = (((summ.get("content_urls") or {}).get("desktop")
                                or {}).get("page"))
            if not out["audio"]:
                ml = _wiki_json(title, "media-list")
                for m in ml.get("items", []):
                    name = m.get("title", "")
                    if m.get("type") == "audio" and name.startswith("File:"):
                        fn = name.split("File:", 1)[1]
                        out["audio"] = (
                            "https://commons.wikimedia.org/wiki/Special:FilePath/"
                            + urllib.parse.quote(fn))
                        break
        except Exception:
            pass
        if out["page"] and out["audio"]:
            break
    return out


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

_last_at = load_detections(load_data_version())["detected_at"].max()
if pd.notna(_last_at):
    _last_at = _last_at.to_pydatetime()
    st.sidebar.caption(f"Last detection: {_fmt_date(_last_at)} "
                       f"{_fmt_time(_last_at)}")

# --- False-positive filter controls --------------------------------------
# Detections are flagged, never deleted. Filtered is the default view; Raw
# shows everything the listener recorded. The per-test switches are public;
# threshold sliders and species overrides need the admin passphrase.

st.sidebar.markdown("**Detections**")
st.sidebar.radio(
    "Detections", ["Filtered", "Raw"], key="data_mode", horizontal=True,
    label_visibility="collapsed",
    help="Filtered hides detections that look like false positives "
         "(see the Filter report tab). Raw shows everything recorded.")
filtered_mode = st.session_state.get("data_mode", "Filtered") == "Filtered"

with st.sidebar.expander("Filter tests", expanded=False):
    for _t in filters.TESTS:
        st.checkbox(filters.TEST_LABELS[_t], value=True, key=f"test_{_t}",
                    help=filters.TEST_HELP[_t], disabled=not filtered_mode)
    st.caption("Each test flags detections; nothing is deleted. Details in "
               "the Filter report tab.")


def _is_admin():
    return bool(config.ADMIN_PASSWORD) and st.session_state.get("admin_ok", False)


if config.ADMIN_PASSWORD:
    with st.sidebar.expander("Admin", expanded=False):
        if _is_admin():
            st.caption("Signed in. Threshold sliders and species overrides "
                       "are in the Filter report tab.")
            if st.button("Sign out", key="admin_signout"):
                st.session_state.admin_ok = False
                st.rerun()
        else:
            with st.form("admin_login", clear_on_submit=True):
                pw = st.text_input("Passphrase", type="password")
                if st.form_submit_button("Sign in"):
                    if hmac.compare_digest(pw.encode(),
                                           config.ADMIN_PASSWORD.encode()):
                        st.session_state.admin_ok = True
                        st.rerun()
                    st.error("Wrong passphrase.")


def effective_config():
    """Stored thresholds, overlaid with the admin's unsaved slider values."""
    stored = load_stored_config()
    if not _is_admin():
        return stored
    return {name: st.session_state.get(f"cfg_{name}", value)
            for name, value in stored.items()}


def enabled_tests():
    return tuple(t for t in filters.TESTS
                 if st.session_state.get(f"test_{t}", True))


def _filter_key():
    """The current filter setting as hashable cache-key parts:
    (thresholds, enabled tests, overrides, data version)."""
    overrides = {r["common_name"]: r["status"] for r in load_overrides()}
    return (tuple(sorted(effective_config().items())), enabled_tests(),
            tuple(sorted(overrides.items())), load_data_version())


def current_filters():
    """(filters.Result for the current settings, the rows to display).

    The rows are the kept detections in Filtered mode and all of them in Raw.
    """
    res = compute_filters(*_filter_key())
    det = res.detections
    view = det[det["keep"].astype(bool)] if filtered_mode else det
    return res, view


def _test_label(key):
    if not isinstance(key, str):  # None / NaN / pd.NA: nothing removed it
        return ""
    if key == "override":
        return "Manual override"
    return filters.TEST_LABELS.get(key, str(key))

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
    res, view = current_filters()
    det = res.detections
    today = config.today_local()
    today_total = int((view["detected_at"].dt.date == today).sum())

    cutoff = today - datetime.timedelta(days=6)
    first_seen = view.groupby("common_name")["detected_at"].min()
    new_week = int((first_seen.dt.date >= cutoff).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total detections", f"{len(view):,}")
    c2.metric("Species (life list)", view["common_name"].nunique())
    c3.metric("Detections today", today_total)
    c4.metric("New species this week", new_week)
    # Always draw this caption: an element that disappears inside a fragment
    # can linger on screen as a stale copy.
    if filtered_mode:
        st.caption(
            f"Filtered: {len(view):,} of {len(det):,} detections and "
            f"{view['common_name'].nunique()} of {det['common_name'].nunique()} "
            "species pass the false-positive filters. Choose Raw in the "
            "sidebar to see everything.")
    else:
        st.caption("Raw: every detection the listener recorded, including "
                   "likely false positives.")


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

def _species_table(records, extra_config=None, select_key=None):
    """A species table. With ``select_key``, selecting a row opens that bird's
    profile in a pop-up (the table then redraws unselected, so the same row
    can be picked again)."""
    df = pd.DataFrame(records)
    colcfg = {
        "Photo": st.column_config.ImageColumn("", width="small"),
    }
    if extra_config:
        colcfg.update(extra_config)
    if not show_photos and "Photo" in df.columns:
        df = df.drop(columns=["Photo"])
        colcfg.pop("Photo", None)
    if not select_key or "Species" not in df.columns:
        st.dataframe(df, hide_index=True, width="stretch", column_config=colcfg)
        return
    version = st.session_state.get(f"{select_key}_v", 0)
    key = f"{select_key}_{version}"
    names = list(df["Species"])

    def _picked():
        rows = st.session_state[key].selection.rows
        if rows:
            st.session_state.open_profile = names[rows[0]]
            st.session_state[f"{select_key}_v"] = version + 1

    st.dataframe(df, hide_index=True, width="stretch", column_config=colcfg,
                 key=key, on_select=_picked, selection_mode="single-row")
    st.caption("Select a row to open that bird's profile.")


def _conf_col(label="Confidence"):
    return {label: st.column_config.ProgressColumn(
        label, min_value=0, max_value=100, format="%d%%")}


# --- Live-feed cards + clip voting ---------------------------------------

# We pull a generous window of recent rows, then collapse back-to-back repeats of
# the same species into single cards (a resident bird can otherwise fill the whole
# feed). _FEED_FETCH is how many raw detections we read (more in Filtered mode,
# where roughly half are hidden); _FEED_CARDS caps how many collapsed cards we
# render (each mounts an audio element and two buttons).
_FEED_FETCH = 150
_FEED_FETCH_FILTERED = 400
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


def _detection_card(group, tally, mine, removed_by=None):
    """One species group as a bordered card: photo, name (with a ×N badge when
    consecutive repeats were collapsed), the time/best-confidence line, the
    representative clip, and up/down vote buttons with a 'Disputed' flag past the
    threshold. Votes apply to the run's highest-confidence detection.
    ``removed_by`` names the filter test that flags the shown detection, if any."""
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
        if removed_by:
            meta += f"  ·  :orange[Filtered out: {_test_label(removed_by)}]"
        head.caption(meta)

        src = _clip_src(r)
        if src:
            st.audio(src)
        else:
            st.caption("_clip unavailable_")

        # Field-guide context so voters have something to judge against: a link
        # to read about the bird, and a known reference recording to compare with
        # the clip above. A keyed toggle (not an expander) keeps the reference
        # open across the feed's 10s auto-refresh.
        ref = species_reference(r["common_name"], r["scientific_name"])
        if ref.get("page"):
            st.markdown(f"[\U0001F4D6 About the {r['common_name']} "
                        f"↗]({ref['page']})")
        if ref.get("audio"):
            if st.toggle("\U0001F50A Hear a reference recording",
                         key=f"ref_{did}",
                         help="A known example of this species — compare it with "
                              "the clip above before you vote."):
                st.audio(ref["audio"])
                st.caption("Reference recording via Wikimedia Commons · "
                           f"[open ↗]({ref['audio']}) (Ogg — use the link on "
                           "Safari/iOS).")

        c_up, c_down, c_prof, _rest = st.columns([1, 1, 1.4, 2.6])
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
        if c_prof.button("\U0001F426 Profile", key=f"prof_{did}", width="stretch",
                         help=f"Open the {r['common_name']} profile"):
            st.session_state.open_profile = r["common_name"]
            st.rerun()  # whole app, so the pop-up opens outside this fragment


# --- Tabs ----------------------------------------------------------------

feed_tab, stats_tab, life_tab, birds_tab, insights_tab, report_tab = st.tabs(
    ["Live feed", "Daily stats", "Life list", "Birds", "Insights",
     "Filter report"],
    key="main_tab", on_change="rerun",
    default="Birds" if st.query_params.get("bird") else None)

with feed_tab:
    st.subheader("Live feed")

    st.caption("Back-to-back repeats of the same species are grouped into one "
               "card (best clip shown). Tap \U0001F44D / \U0001F44E to tell us "
               "whether a clip matches the bird.")

    @st.fragment(run_every=_REFRESH)
    def live_feed():
        raw_rows = load_recent(_FEED_FETCH_FILTERED if filtered_mode
                               else _FEED_FETCH)
        # Judge every row with the same filters as the rest of the page. A row
        # newer than the cached data has no verdict yet: hidden in Filtered
        # mode until it is judged (seconds), shown unmarked in Raw.
        det = current_filters()[0].detections
        kept = dict(zip(det["id"], det["keep"].astype(bool)))
        why = dict(zip(det["id"], det["removed_by"]))
        if filtered_mode:
            rows = [r for r in raw_rows if kept.get(r["id"], False)]
            hidden = len(raw_rows) - len(rows)
            # Always draw this caption: an element that disappears inside a
            # fragment can linger on screen as a stale copy.
            st.caption(
                f"Filtered: {hidden:,} of the latest {len(raw_rows):,} detections "
                "are hidden as likely false positives. Choose Raw in the "
                "sidebar to see them, or open the Filter report tab.")
        else:
            rows = raw_rows
            st.caption("Raw: every detection as it arrives. Cards the "
                       "false-positive filters would hide are marked.")
        if not rows:
            st.info("No detections yet. Start the listener "
                    "(`python -m capture.listen`) and play some birdsong near "
                    "the mic." if not raw_rows else
                    "Nothing recent has passed the filters yet.")
            return
        groups = _group_consecutive(rows)[:_FEED_CARDS]
        ids = [g["rep"].get("id") for g in groups]
        tallies = _read(db.vote_tallies, ids, default={}) or {}
        mine = _read(db.my_votes, st.session_state.voter, ids, default={}) or {}
        if st.session_state.vote_error:
            st.warning("Voting is unavailable — the dashboard's database user "
                       "is read-only.")
        removed = {} if filtered_mode else {
            i: why.get(i) for i in ids if i in kept and not kept[i]}
        for g in groups:
            did = g["rep"].get("id")
            _detection_card(g, tallies.get(did, {}), mine.get(did, 0),
                            removed.get(did))

    live_feed()

with stats_tab:
    st.subheader("Daily stats")
    res, view = current_filters()
    det = res.detections
    today = config.today_local()
    # Offer every day the listener recorded, even if the filters empty it.
    days = sorted(set(det["detected_at"].dt.date), reverse=True)
    options = [today] + [d for d in days if d != today]
    day = st.selectbox("Day", options, index=0,
                       format_func=lambda d: d.isoformat())

    day_rows = view[view["detected_at"].dt.date == day]
    counts = (day_rows
              .groupby(["common_name", "scientific_name"], dropna=False)
              .agg(n=("id", "size"), best=("confidence", "max"))
              .reset_index()
              .sort_values(["n", "best"], ascending=False))
    total = len(day_rows)

    st.write(f"**{total}** detection{'' if total == 1 else 's'} on "
             f"{day.isoformat()} across **{len(counts)}** species.")
    if filtered_mode:
        flagged = int((det["detected_at"].dt.date == day).sum()) - total
        if flagged > 0:
            st.caption(f"{flagged:,} more detection{'' if flagged == 1 else 's'} "
                       "that day were flagged as likely false positives.")

    st.markdown("**Activity by hour**")
    hourly = day_rows["detected_at"].dt.hour.value_counts() \
        .reindex(range(24), fill_value=0)
    hourly_df = pd.DataFrame({"Detections": hourly.values},
                             index=[f"{h:02d}" for h in range(24)])
    hourly_df.index.name = "Hour"
    st.bar_chart(hourly_df, color="#4c9a6a")

    st.markdown("**Detections — last 14 days**")
    by_day = view["detected_at"].dt.date.value_counts()
    span = [today - datetime.timedelta(days=i) for i in range(13, -1, -1)]
    trend_df = pd.DataFrame({"Detections": [int(by_day.get(d, 0)) for d in span]},
                            index=[d.strftime("%m-%d") for d in span])
    trend_df.index.name = "Day"
    st.bar_chart(trend_df, color="#6fb6c9")

    st.markdown("**Species this day**")
    if len(counts):
        _species_table(
            [
                {
                    "Photo": species_photo(r.common_name, r.scientific_name)
                    if show_photos else None,
                    "Species": r.common_name,
                    "Scientific name": r.scientific_name,
                    "Count": int(r.n),
                    "Best confidence": int(r.best * 100),
                }
                for r in counts.itertuples(index=False)
            ],
            _conf_col("Best confidence"),
            select_key="day_pick",
        )
    else:
        st.info("No detections on this day.")

with life_tab:
    st.subheader("Life list")
    res, view = current_filters()
    life = (view.groupby("common_name")
            .agg(scientific_name=("scientific_name", "first"),
                 total=("id", "size"),
                 first_seen=("detected_at", "min"),
                 last_seen=("detected_at", "max"),
                 best=("confidence", "max"))
            .reset_index()
            .sort_values("first_seen"))
    st.caption(f"{len(life)} species heard"
               + (" (filtered)" if filtered_mode else ""))
    if len(life):
        _species_table(
            [
                {
                    "Photo": species_photo(r.common_name, r.scientific_name)
                    if show_photos else None,
                    "Species": r.common_name,
                    "Scientific name": r.scientific_name,
                    "First heard": _fmt_date(r.first_seen.to_pydatetime()),
                    "Last heard": _fmt_date(r.last_seen.to_pydatetime()),
                    "Times": int(r.total),
                    "Best": int(r.best * 100),
                }
                for r in life.itertuples(index=False)
            ],
            _conf_col("Best"),
            select_key="life_pick",
        )
    else:
        st.info("No species heard yet.")

    if filtered_mode:
        sp = res.species
        dropped = sp[sp["kept"] == 0]
        if len(dropped):
            best = res.detections.groupby("common_name")["confidence"].max()
            with st.expander(f"{len(dropped)} more species were heard but "
                             "removed by the filters"):
                st.caption("Likely false positives, or species without enough "
                           "confident calls to be sure. Their detections are "
                           "kept in the database; the Filter report tab shows "
                           "why each was removed.")
                st.dataframe(
                    pd.DataFrame({
                        "Species": dropped["common_name"],
                        "Detections": dropped["raw"],
                        "Best": [int(best.get(n, 0) * 100)
                                 for n in dropped["common_name"]],
                        "Removed mostly by": [_test_label(t) for t in
                                              dropped["top_removed_by"]],
                    }).sort_values("Detections", ascending=False),
                    hide_index=True, width="stretch",
                    column_config=_conf_col("Best"),
                )


# --- Insights ------------------------------------------------------------
# Seasonal and daily patterns, computed by insights.py from the same rows the
# rest of the page shows (filtered or raw). Charts use the dashboard palette:
# meadow green for data, a muted sage for reference marks (sunrise), and one
# light-to-dark green ramp for every heatmap. Each chart has a hover tooltip and
# a data table underneath.

_MUTED = "#5a7d72"
_GREEN = "#4c9a6a"
_SURFACE = "#fbfdfe"
_OFFLINE = "#e6ecee"
_RAMP = ["#e3f1e8", "#b5dbc2", "#7fbd96", "#4c9a6a", "#2f7350", "#1d4e36"]
_WHISKER = "#b5dbc2"


@st.cache_data(ttl=60, show_spinner=False)
def load_insights(key, filtered, min_days):
    res = compute_filters(*key)
    det = res.detections
    view = det[det["keep"].astype(bool)] if filtered else det
    lat, lon, tz = config.LATITUDE, config.LONGITUDE, config.TIMEZONE
    chorus = insights.dawn_chorus(view, lat, lon, tz)
    # A continuous sunrise line over the whole season, not just listening days.
    sun = pd.DataFrame(columns=["day", "sunrise_h"])
    if len(det):
        days = pd.date_range(det["detected_at"].min().normalize(),
                             det["detected_at"].max().normalize(), freq="D")
        rises = pd.to_datetime([insights.sun_times(d.date(), lat, lon, tz)[0]
                                for d in days])
        sun = pd.DataFrame({"day": days,
                            "sunrise_h": rises.hour + rises.minute / 60})
    return {
        "headline": insights.headline(view, det),
        "weekly": insights.weekly_richness(view, det),
        "grid": insights.presence_grid(view, det, min_days=min_days),
        "chorus": chorus,
        "by_month": insights.dawn_by_month(chorus),
        "order": insights.dawn_order(view, lat, lon, tz),
        "rhythm": insights.daily_rhythm(view),
        "sun": sun,
    }


def _fmt_day(d):
    """'Sep 7' — month and day without zero padding (portable strftime)."""
    return f"{d:%b} {d.day}"


def _week_label(weeks):
    weeks = pd.to_datetime(pd.Series(weeks))
    return weeks.dt.strftime("%b ") + weeks.dt.day.astype(str)


def _rel_sunrise(minutes):
    m = int(round(minutes))
    if m == 0:
        return "at sunrise"
    return f"{abs(m)} min {'before' if m < 0 else 'after'} sunrise"


_HOUR_LABEL = "datum.value + ':00'"


def _insights_presence(data):
    g = data["grid"].copy()
    if g.empty:
        st.info("Not enough detections yet.")
        return
    g["week_label"] = _week_label(g["week"]).values
    weeks = list(dict.fromkeys(g.sort_values("week")["week_label"]))
    species = list(g.sort_values("order")["common_name"].drop_duplicates())
    g["heard"] = [
        "Listener offline" if l == 0 else f"{h} of {l} days listened"
        for h, l in zip(g["days_heard"], g["days_listened"])]
    x = alt.X("week_label:O", title=None, scale=alt.Scale(domain=weeks),
              axis=alt.Axis(labelAngle=-45, ticks=False, domain=False))
    y = alt.Y("common_name:N", title=None, scale=alt.Scale(domain=species),
              axis=alt.Axis(ticks=False, domain=False, labelOverlap=False,
                            labelLimit=220))
    tip = [alt.Tooltip("common_name:N", title="Species"),
           alt.Tooltip("week_label:N", title="Week of"),
           alt.Tooltip("heard:N", title="Heard on")]
    offline = alt.Chart(g[g["days_listened"] == 0]).mark_rect(
        color=_OFFLINE, stroke=_SURFACE, strokeWidth=2).encode(x=x, y=y, tooltip=tip)
    heard = alt.Chart(g[g["days_heard"] > 0]).mark_rect(
        stroke=_SURFACE, strokeWidth=2, cornerRadius=2).encode(
        x=x, y=y, tooltip=tip,
        color=alt.Color("share:Q", title="Share of days heard",
                        scale=alt.Scale(domain=[0, 1], range=_RAMP),
                        legend=alt.Legend(format="%", orient="top",
                                          gradientLength=160)))
    st.altair_chart(alt.layer(offline, heard).properties(
        height=alt.Step(22)), width="stretch")


def _insights_weekly(data):
    w = data["weekly"].copy()
    if w.empty:
        return
    w["week_label"] = _week_label(w["week"]).values
    weeks = list(w["week_label"])
    top = max(int(w["species"].max()), 1)
    w["bar"] = w["species"]
    w["label_y"] = top * 0.02
    w["note"] = ["Listener offline" if l == 0 else f"{l} of 7 days"
                 for l in w["days_listened"]]
    x = alt.X("week_label:O", title=None, scale=alt.Scale(domain=weeks),
              axis=alt.Axis(labelAngle=-45, ticks=False))
    y = alt.Y("bar:Q", title="Species heard",
              axis=alt.Axis(tickMinStep=5, gridColor="#e2eef0"))
    tip = [alt.Tooltip("week_label:N", title="Week of"),
           alt.Tooltip("species:Q", title="Species"),
           alt.Tooltip("detections:Q", title="Detections", format=","),
           alt.Tooltip("note:N", title="Listening")]
    bars = alt.Chart(w[w["days_listened"] > 0]).mark_bar(
        color=_GREEN, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=x, y=y, tooltip=tip)
    off = alt.Chart(w[w["days_listened"] == 0]).mark_text(
        text="offline", angle=270, align="left", baseline="middle",
        color=_MUTED, fontSize=11).encode(
        x=x, y=alt.Y("label_y:Q"),
        tooltip=[alt.Tooltip("week_label:N", title="Week of"),
                 alt.Tooltip("note:N", title="Listening")])
    st.altair_chart(alt.layer(bars, off).properties(height=260), width="stretch")


def _insights_dawn(data):
    ch, sun = data["chorus"].copy(), data["sun"]
    if ch.empty:
        st.info("No dawn detections yet.")
        return
    ch["time"] = ch["first_call"].dt.strftime("%H:%M")
    ch["sunrise_t"] = ch["sunrise"].dt.strftime("%H:%M")
    ch["relative"] = [_rel_sunrise(m) for m in ch["min_from_sunrise"]]
    lo = int(min(ch["first_call_h"].min(), sun["sunrise_h"].min()))
    hi = int(max(ch["first_call_h"].max(), sun["sunrise_h"].max())) + 1
    # Both layers carry a "series" column so they share one legend.
    color = alt.Color("series:N", title=None,
                      scale=alt.Scale(domain=["First call of the day", "Sunrise"],
                                      range=[_GREEN, _MUTED]),
                      legend=alt.Legend(orient="top"))
    ch["series"] = "First call of the day"
    sun = sun.assign(series="Sunrise")
    x = alt.X("day:T", title=None, axis=alt.Axis(format="%b %d", gridColor="#e2eef0"))
    line = alt.Chart(sun).mark_line(strokeWidth=2).encode(
        x=x, y=alt.Y("sunrise_h:Q", scale=alt.Scale(domain=[lo, hi], zero=False)),
        color=color)
    dots = alt.Chart(ch).mark_circle(size=64, opacity=1, stroke=_SURFACE,
                                     strokeWidth=2).encode(
        x=x,
        y=alt.Y("first_call_h:Q", title="Time of day",
                scale=alt.Scale(domain=[lo, hi], zero=False),
                axis=alt.Axis(values=list(range(lo, hi + 1)),
                              labelExpr=_HOUR_LABEL, gridColor="#e2eef0")),
        color=color,
        tooltip=[alt.Tooltip("day:T", title="Date", format="%b %d"),
                 alt.Tooltip("time:N", title="First call"),
                 alt.Tooltip("species:N", title="Species"),
                 alt.Tooltip("sunrise_t:N", title="Sunrise"),
                 alt.Tooltip("relative:N", title="Relative")])
    st.altair_chart(alt.layer(line, dots).properties(height=300), width="stretch")


def _insights_order(data):
    o = data["order"].copy()
    if o.empty:
        st.info("Not enough dawn detections yet.")
        return
    o["relative"] = [_rel_sunrise(m) for m in o["median_min"]]
    species = list(o["common_name"])
    y = alt.Y("common_name:N", title=None, scale=alt.Scale(domain=species),
              axis=alt.Axis(ticks=False, domain=False, labelOverlap=False,
                            labelLimit=220))
    xs = alt.Scale(domain=[min(-30, o["q1_min"].min() - 10), o["q3_min"].max() + 10])
    tip = [alt.Tooltip("common_name:N", title="Species"),
           alt.Tooltip("relative:N", title="Usually starts"),
           alt.Tooltip("days:Q", title="Days")]
    spread = alt.Chart(o).mark_rule(color=_WHISKER, strokeWidth=6,
                                    strokeCap="round").encode(
        x=alt.X("q1_min:Q", scale=xs), x2="q3_min:Q", y=y, tooltip=tip)
    dots = alt.Chart(o).mark_circle(size=90, color=_GREEN, opacity=1,
                                    stroke=_SURFACE, strokeWidth=2).encode(
        x=alt.X("median_min:Q", scale=xs,
                title="Minutes after sunrise of the first call of the day",
                axis=alt.Axis(gridColor="#e2eef0")),
        y=y, tooltip=tip)
    zero = pd.DataFrame({"x": [0], "label": ["Sunrise"]})
    rule = alt.Chart(zero).mark_rule(color=_MUTED, strokeDash=[4, 4]).encode(
        x=alt.X("x:Q", scale=xs))
    label = alt.Chart(zero).mark_text(color=_MUTED, align="left", dx=4, dy=-4,
                                      baseline="bottom", fontSize=11).encode(
        x=alt.X("x:Q", scale=xs), y=alt.value(0), text="label:N")
    st.altair_chart(alt.layer(spread, rule, dots, label).properties(
        height=alt.Step(26)), width="stretch")


def _insights_rhythm(data):
    r = data["rhythm"].copy()
    if r.empty:
        return
    species = list(r.sort_values("rank")["common_name"].drop_duplicates())
    r["hours"] = [f"{h:02d}:00–{(h + 1) % 24:02d}:00" for h in r["hour"]]
    st.altair_chart(alt.Chart(r).mark_rect(
        stroke=_SURFACE, strokeWidth=2, cornerRadius=2).encode(
        x=alt.X("hour:O", title="Hour of day",
                axis=alt.Axis(values=list(range(0, 24, 3)), labelExpr=_HOUR_LABEL,
                              labelAngle=0, ticks=False, domain=False)),
        y=alt.Y("common_name:N", title=None, scale=alt.Scale(domain=species),
                axis=alt.Axis(ticks=False, domain=False, labelOverlap=False,
                              labelLimit=220)),
        color=alt.Color("share:Q", title="Share of its detections",
                        scale=alt.Scale(domain=[0, float(r["share"].max() or 1)],
                                        range=_RAMP),
                        legend=alt.Legend(format="%", orient="top",
                                          gradientLength=160)),
        tooltip=[alt.Tooltip("common_name:N", title="Species"),
                 alt.Tooltip("hours:N", title="Hour"),
                 alt.Tooltip("detections:Q", title="Detections", format=","),
                 alt.Tooltip("share:Q", title="Share", format=".0%")],
    ).properties(height=alt.Step(26)), width="stretch")


with insights_tab:
    st.subheader("Insights")
    st.caption("Seasonal and daily patterns in what the window hears. "
               + ("Built from the filtered detections; choose Raw in the "
                  "sidebar to include likely false positives."
                  if filtered_mode else
                  "Built from every raw detection, likely false positives "
                  "included; choose Filtered in the sidebar for the cleaner "
                  "picture."))
    min_days = st.session_state.get("ins_min_days", 3)
    data = load_insights(_filter_key(), filtered_mode, min_days)
    h = data["headline"]

    k1, k2, k3 = st.columns(3)
    k1.metric("Days listened", h["days_listened"],
              help="Days the listener recorded anything. It was offline from "
                   "late June to mid July and in late September.")
    k2.metric("Species", h["species"])
    k3.metric("Most species in a week", h["peak_week_species"],
              help=(f"Week of {_fmt_day(h['peak_week'])}"
                    if h["peak_week"] else None))
    if h["newest"]:
        st.markdown(f"Newest arrival: **{h['newest']}**, first heard "
                    f"{_fmt_day(h['newest_date'])}.")

    st.markdown("#### Who's here when")
    st.caption("Each row is a species, in the order it was first heard; each "
               "column is a week. Darker means heard on more of the days the "
               "listener ran that week. Gray weeks are when the listener was "
               "offline. Rows that only light up from mid August on are fall "
               "migrants passing through.")
    st.select_slider(
        "Show species heard on at least",
        options=[1, 2, 3, 5, 10, 20], value=3, key="ins_min_days",
        format_func=lambda n: f"{n} day{'' if n == 1 else 's'}")
    _insights_presence(data)
    with st.expander("Data: weeks heard per species"):
        g = data["grid"]
        if len(g):
            st.dataframe(
                g[g["days_heard"] > 0].assign(week=lambda d: d["week"].dt.date)
                [["common_name", "week", "days_heard", "days_listened"]]
                .rename(columns={"common_name": "Species", "week": "Week of",
                                 "days_heard": "Days heard",
                                 "days_listened": "Days listened"}),
                hide_index=True, width="stretch")

    st.markdown("#### Species per week")
    wk = data["weekly"]
    if h["peak_week"]:
        st.caption(f"The richest week was the week of {_fmt_day(h['peak_week'])}, "
                   f"with {h['peak_week_species']} species. Weeks marked "
                   "offline had no listening; hover a bar for how many days "
                   "the listener ran that week.")
    _insights_weekly(data)
    with st.expander("Data: species per week"):
        if len(wk):
            st.dataframe(wk.assign(week=wk["week"].dt.date).rename(columns={
                "week": "Week of", "species": "Species",
                "detections": "Detections", "days_listened": "Days listened"}),
                hide_index=True, width="stretch")

    st.markdown("#### The dawn chorus follows the sun")
    bm = data["by_month"]
    if len(bm):
        parts = [f"{m:%B}: {_rel_sunrise(v)}"
                 for m, v in zip(bm["month"], bm["median_min"])]
        st.caption("Each dot is the first call of a day (between 90 minutes "
                   "before and 4 hours after sunrise, which leaves out night "
                   "migrants passing over); the line is sunrise. Typical first "
                   "call by month: " + "; ".join(parts) + ".")
    _insights_dawn(data)
    with st.expander("Data: first call each day"):
        ch = data["chorus"]
        if len(ch):
            st.dataframe(pd.DataFrame({
                "Date": ch["day"].dt.date,
                "First call": ch["first_call"].dt.strftime("%H:%M"),
                "Species": ch["species"],
                "Sunrise": ch["sunrise"].dt.strftime("%H:%M"),
                "Minutes from sunrise": ch["min_from_sunrise"].astype(int),
            }), hide_index=True, width="stretch")

    st.markdown("#### Who sings first")
    od = data["order"]
    if len(od):
        lead = od.iloc[0]
        st.caption(f"When each species makes its first call of the day, "
                   f"relative to sunrise (species heard at dawn on 5 or more "
                   f"days). {lead['common_name']} usually starts first, "
                   f"{_rel_sunrise(lead['median_min'])}. The dot is the "
                   "typical day; the bar spans the middle half of days.")
    _insights_order(data)
    with st.expander("Data: dawn order"):
        st.dataframe(od.rename(columns={
            "common_name": "Species", "median_min": "Median minutes after sunrise",
            "days": "Days", "q1_min": "Early days (25th pct)",
            "q3_min": "Late days (75th pct)"}), hide_index=True, width="stretch")

    st.markdown("#### Daily rhythm")
    st.caption("When each of the most-heard species calls, as a share of its "
               "own detections, so a quiet species' pattern is as clear as a "
               "loud one's. Hours are local time.")
    _insights_rhythm(data)
    with st.expander("Data: detections by hour"):
        rh = data["rhythm"]
        if len(rh):
            st.dataframe(rh.pivot(index="common_name", columns="hour",
                                  values="detections")
                         .reindex(list(rh.sort_values("rank")["common_name"]
                                       .drop_duplicates()))
                         .rename_axis(index="Species", columns="Hour"),
                         width="stretch")


# --- Bird profiles -------------------------------------------------------
# One bird at a time: what it is, when and how it calls, what it sounds like,
# and how it differs from the others. Shown in the Birds tab (searchable, with a
# shareable ?bird= link) and in a pop-up opened by clicking a bird elsewhere.
# Like every view, it reads the filtered rows in Filtered mode.


@st.cache_data(ttl=86400, show_spinner=False)
def species_summary(common_name, scientific_name):
    """A short Wikipedia summary and page link for a species, or {}."""
    for title in (common_name, scientific_name):
        if not title:
            continue
        try:
            data = _wiki_json(title, "summary")
        except Exception:
            continue
        text = (data.get("extract") or "").strip()
        page = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
        if text:
            return {"extract": text, "page": page}
    return {}


def _all_about_birds_url(common_name):
    slug = re.sub(r"[^A-Za-z0-9 \-]", "", common_name).strip().replace(" ", "_")
    return "https://www.allaboutbirds.org/guide/" + urllib.parse.quote(slug)


def _short(text, limit=420):
    """The first sentences of ``text`` up to about ``limit`` characters."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind(". ")
    return cut[:end + 1] if end > 120 else cut.rstrip() + "…"


@st.cache_data(ttl=3600, max_entries=60, show_spinner=False)
def load_profile(key, filtered, name):
    res = compute_filters(*key)
    det = res.detections
    view = det[det["keep"].astype(bool)] if filtered else det
    cfg = res.config
    pk = {"bout_gap_s": cfg["bout_gap_s"], "run_gap_s": cfg["cadence_run_gap_s"]}
    lat, lon, tz = config.LATITUDE, config.LONGITUDE, config.TIMEZONE
    mine = view[view["common_name"] == name]
    clips = (mine[mine["clip_url"].notna()]
             .sort_values(["confidence", "detected_at"], ascending=False)
             .head(10)[["id", "detected_at", "confidence", "clip_url"]])
    # One clip per well-recorded species for the all-bird pitch baseline,
    # chosen from detections before today so the set (and its cache) holds
    # steady through the day.
    before = view[(view["clip_url"].notna())
                  & (view["detected_at"].dt.date < config.today_local())]
    counts = before["common_name"].value_counts()
    best = (before[before["common_name"].isin(counts[counts >= 5].index)]
            .sort_values(["confidence", "id"], ascending=False)
            .drop_duplicates("common_name"))
    srow = res.species[res.species["common_name"] == name]
    return {
        "scientific": mine["scientific_name"].dropna().iloc[0]
        if mine["scientific_name"].notna().any() else None,
        "glance": bird_profile.at_a_glance(view, det, name),
        "weekly": bird_profile.weekly(view, det, name),
        "hourly": bird_profile.hourly_vs_all(view, name),
        "callout": bird_profile.period_callout(view, name),
        "dawn": bird_profile.dawn(view, name, lat, lon, tz),
        "pattern": bird_profile.calling_pattern(view, name, **pk),
        "companions": bird_profile.companions(view, name),
        "confidence": bird_profile.confidence_vs_all(view, name),
        "distinctive": bird_profile.distinctive(view, det, name, **pk),
        "filter": srow.iloc[0].to_dict() if len(srow) else None,
        "clips": clips.to_dict("records"),
        "baseline_urls": tuple(best.sort_values("common_name")["clip_url"]),
    }


def _analyze_url(url):
    try:
        return acoustics.analyze(acoustics.fetch(url))
    except Exception:
        return None


@st.cache_data(ttl=86400, max_entries=120, show_spinner=False)
def load_clip_sounds(urls):
    """Spectrogram and pitch signature for each clip URL (None on failure)."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(_analyze_url, urls))


@st.cache_data(ttl=86400, max_entries=4, show_spinner=False)
def load_baseline_signature(urls):
    """The all-bird pitch signature: the mean over one clip per species."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [a for a in pool.map(_analyze_url, urls) if a]
    return acoustics.mean_signature([a["signature"] for a in found])


_AVERAGE = "#9aa9a4"  # the all-bird average in comparison charts


def _vs_all_color():
    return alt.Color("series:N", title=None,
                     scale=alt.Scale(domain=["This bird", "All birds"],
                                     range=[_GREEN, _AVERAGE]),
                     legend=alt.Legend(orient="top"))


def _vs_all_dash():
    return alt.StrokeDash("series:N", legend=None,
                          scale=alt.Scale(domain=["This bird", "All birds"],
                                          range=[[1, 0], [5, 4]]))


_STATUS_COLOURS = {
    "Resident": ("#eaf7ef", "#4c9a6a"), "Occasional": ("#eef3f5", "#5a7d72"),
    "Arrived": ("#eaf4f7", "#4f93a6"), "Departed": ("#f4f1ea", "#8a7a52"),
    "Passing through": ("#eaf4f7", "#4f93a6"),
    "Rare visitor": ("#f4f1ea", "#8a7a52"),
}


def _khz(hz):
    return f"{hz / 1000:.1f} kHz"


def _profile_header(name, p, res, filtered, where):
    sci = p["scientific"]
    g = p["glance"]
    photo = species_photo(name, sci) if show_photos else None
    left, right = st.columns([1, 4], vertical_alignment="top")
    if photo:
        left.image(photo, width=180)
    with right:
        st.markdown(f"### {name}")
        bits = [f"*{sci}*"] if sci else []
        if g and g["status"]:
            bg, fg = _STATUS_COLOURS.get(g["status"], ("#eef3f5", "#5a7d72"))
            bits.append(
                f"<span style='background:{bg};color:{fg};border:1px solid {fg};"
                f"border-radius:999px;padding:1px 10px;font-size:0.82rem;"
                f"font-weight:600'>{g['status']}</span> "
                f"<span style='color:#5a7d72;font-size:0.88rem'>"
                f"{g['status_detail']}</span>")
        st.markdown(" &nbsp; ".join(bits), unsafe_allow_html=True)

        phrases, compared = p["distinctive"]
        if phrases:
            st.markdown("**What stands out:** " + ", ".join(phrases)
                        + f" (compared with the {compared} birds heard often "
                        "enough to compare).")
        elif phrases is None:
            st.caption("Too few detections to compare with the other birds.")
        else:
            st.markdown("**What stands out:** nothing unusual; it is fairly "
                        "typical of the birds heard here.")

    f = p["filter"]
    if f is not None:
        removed = int(f["raw"]) - int(f["kept"])
        if not filtered and int(f["kept"]) == 0:
            st.warning(
                "The false-positive filters remove every detection of this "
                f"species (mostly {_test_label(f['top_removed_by']).lower()}). "
                "It is shown because Raw is selected in the sidebar.")
        elif filtered and removed > 0:
            st.caption(f"Based on the {int(f['kept']):,} detections that pass "
                       f"the filters; {removed:,} more were flagged as likely "
                       "false positives.")

    info = species_summary(name, sci)
    if info.get("extract"):
        st.markdown(_short(info["extract"]))
    links = []
    if info.get("page"):
        links.append(f"[Wikipedia ↗]({info['page']})")
    links.append(f"[All About Birds ↗]({_all_about_birds_url(name)})")
    st.markdown(" · ".join(links))
    ref = species_reference(name, sci)
    if ref.get("audio") and st.toggle("\U0001F50A Hear a reference recording",
                                      key=f"{where}_ref_{name}"):
        st.audio(ref["audio"])
        st.caption("Via Wikimedia Commons · "
                   f"[open ↗]({ref['audio']}) (Ogg — use the link on Safari/iOS).")


def _profile_tiles(g):
    c = st.columns(5)
    c[0].metric("Detections", f"{g['detections']:,}",
                help=f"Number {g['rank']} of {g['species_count']} birds by "
                     "detections.")
    c[1].metric(f"Days heard (of {g['days_listened']})", g["days_heard"],
                help="Days it was heard, out of days the listener ran.")
    c[2].metric("First heard", _fmt_day(g["first_heard"]))
    c[3].metric("Last heard", _fmt_day(g["last_heard"]))
    c[4].metric("Best confidence", f"{g['best_confidence']:.0%}")


def _profile_season(p, where):
    w = p["weekly"].copy()
    if w.empty:
        return
    st.markdown("#### Its season")
    st.caption("Share of each week's listening days it was heard.")
    w["week_label"] = _week_label(w["week"]).values
    weeks = list(w["week_label"])
    w["pct"] = w["share"].fillna(0)
    w["heard"] = [("Listener offline" if l == 0 else f"{h} of {l} days")
                  for h, l in zip(w["days_heard"], w["days_listened"])]
    x = alt.X("week_label:O", title=None, scale=alt.Scale(domain=weeks),
              axis=alt.Axis(labelAngle=-45, ticks=False))
    tip = [alt.Tooltip("week_label:N", title="Week of"),
           alt.Tooltip("heard:N", title="Heard on")]
    bars = alt.Chart(w[w["days_heard"] > 0]).mark_bar(
        color=_GREEN, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=x, tooltip=tip,
        y=alt.Y("pct:Q", title="Days heard", scale=alt.Scale(domain=[0, 1]),
                axis=alt.Axis(format="%", gridColor="#e2eef0", tickCount=4)))
    off = alt.Chart(w[w["days_listened"] == 0].assign(y=0.02)).mark_text(
        text="offline", angle=270, align="left", baseline="middle",
        color=_MUTED, fontSize=11).encode(x=x, y="y:Q", tooltip=tip)
    st.altair_chart(alt.layer(bars, off).properties(height=200),
                    width="stretch", key=f"{where}_season")


def _profile_day(p, where):
    h = p["hourly"]
    st.markdown("#### Its day, against the average bird")
    if p["callout"]:
        st.caption(p["callout"])
    long = pd.concat([
        pd.DataFrame({"hour": h["hour"], "share": h["bird_share"],
                      "series": "This bird"}),
        pd.DataFrame({"hour": h["hour"], "share": h["all_share"],
                      "series": "All birds"})])
    long["hours"] = [f"{x:02d}:00–{(x + 1) % 24:02d}:00" for x in long["hour"]]
    base = alt.Chart(long).encode(
        x=alt.X("hour:Q", title="Hour of day", scale=alt.Scale(domain=[0, 23]),
                axis=alt.Axis(values=list(range(0, 24, 3)),
                              labelExpr=_HOUR_LABEL, gridColor="#e2eef0")),
        y=alt.Y("share:Q", title="Share of calls",
                axis=alt.Axis(format="%", gridColor="#e2eef0", tickCount=4)),
        color=_vs_all_color(),
        tooltip=[alt.Tooltip("series:N", title=""),
                 alt.Tooltip("hours:N", title="Hour"),
                 alt.Tooltip("share:Q", title="Share", format=".0%")])
    lines = base.mark_line(strokeWidth=2, interpolate="monotone").encode(
        strokeDash=_vs_all_dash())
    dots = base.transform_filter(alt.datum.series == "This bird").mark_circle(
        size=40, opacity=1)
    st.altair_chart((lines + dots).properties(height=220),
                    width="stretch", key=f"{where}_day")


def _profile_dawn(p):
    d = p["dawn"]
    st.markdown("#### At dawn")
    if d["rank"]:
        st.markdown(f"Usually starts **{_rel_sunrise(d['median_min'])}**, "
                    f"number {d['rank']} of the {d['ranked']} birds heard at "
                    "dawn on 5 or more days.")
    elif d["days"]:
        st.markdown(f"Heard at dawn on {d['days']} day"
                    f"{'' if d['days'] == 1 else 's'}, typically "
                    f"{_rel_sunrise(d['median_min'])}.")
    else:
        st.markdown("Not heard in the dawn window (90 minutes before to "
                    "4 hours after sunrise).")


def _fmt_metric(v, kind):
    if v is None or pd.isna(v):
        return "—"
    if kind == "calls":
        return f"{v:g}"
    if kind == "minutes":
        return f"{v:.1f} min" if v >= 1 else f"{v * 60:.0f} s"
    if kind == "seconds":
        return f"{v:.0f} s"
    return f"{v:.2f}"


def _profile_pattern(p):
    bird, typical = p["pattern"]
    st.markdown("#### How it calls")
    if not bird:
        return
    rows = [("Calls per bout", "calls_per_bout", "calls"),
            ("Bout length", "bout_minutes", "minutes"),
            ("Gap between calls", "gap_s", "seconds"),
            ("Irregularity (lower is steadier)", "gap_cv", "ratio")]
    st.dataframe(pd.DataFrame({
        "": [r[0] for r in rows],
        "This bird": [_fmt_metric(bird.get(k), kind) for _, k, kind in rows],
        "Typical bird": [_fmt_metric(typical.get(k), kind) for _, k, kind in rows],
    }), hide_index=True, width="stretch")
    st.caption(f"From {int(bird['bouts']):,} bouts (calls less than "
               "two minutes apart). The listener logs at most one detection "
               "per species every 15 seconds, so faster calling looks like "
               "15-second gaps.")


def _spectrogram_chart(a, key):
    db = a["spectrogram"]
    n_bands, steps = db.shape
    df = pd.DataFrame({
        "t": np.repeat(np.arange(steps), n_bands) * a["step_s"],
        "f": np.tile(acoustics.band_centres_hz() / 1000, steps),
        "db": db.T.reshape(-1),
    })
    df["t2"] = df["t"] + a["step_s"]
    df["f2"] = df["f"] + acoustics.BAND_HZ / 1000
    df["f"] = df["f"] - acoustics.BAND_HZ / 2000
    df["f2"] = df["f2"] - acoustics.BAND_HZ / 2000
    chart = alt.Chart(df).mark_rect().encode(
        x=alt.X("t:Q", title="Seconds", scale=alt.Scale(domain=[0, steps * a["step_s"]],
                                                        nice=False),
                axis=alt.Axis(tickCount=5, grid=False)),
        x2="t2:Q",
        y=alt.Y("f:Q", title="kHz", scale=alt.Scale(domain=[0, 12], nice=False),
                axis=alt.Axis(values=[0, 2, 4, 6, 8, 10, 12], grid=False)),
        y2="f2:Q",
        color=alt.Color("db:Q", legend=None,
                        # Quiet parts take the palest green, not the page
                        # colour, so the whole clip reads as one plot.
                        scale=alt.Scale(domain=[acoustics.FLOOR_DB, 0],
                                        range=_RAMP)),
        tooltip=[alt.Tooltip("t:Q", title="Seconds", format=".2f"),
                 alt.Tooltip("f:Q", title="kHz", format=".2f"),
                 alt.Tooltip("db:Q", title="dB below peak", format=".0f")],
    ).properties(height=180)
    st.altair_chart(chart, width="stretch", key=key)


def _profile_sound(p, where):
    st.markdown("#### What it sounds like")
    clips = p["clips"]
    if not clips:
        st.info("No clips of this bird are stored.")
        return
    with st.spinner("Analysing its clips…"):
        sounds = load_clip_sounds(tuple(c["clip_url"] for c in clips))
        baseline = (load_baseline_signature(p["baseline_urls"])
                    if p["baseline_urls"] else None)
    ok = [(c, s) for c, s in zip(clips, sounds) if s]
    if not ok:
        st.info("Its clips could not be loaded right now.")
        return
    st.caption("Spectrograms of its three most confident clips: time across, "
               "pitch up, darker is louder. Low rumble under 0.5 kHz is "
               "usually traffic.")
    cols = st.columns(min(3, len(ok)))
    for col, (c, s) in zip(cols, ok[:3]):
        with col:
            _spectrogram_chart(s, key=f"{where}_spec_{c['id']}")
            st.audio(c["clip_url"])
            st.caption(f"{_fmt_day(c['detected_at'])} "
                       f"{c['detected_at']:%H:%M} · {c['confidence']:.0%}")

    sig = acoustics.mean_signature([s["signature"] for _, s in ok])
    peak, lo, hi = acoustics.signature_stats(sig)
    if peak is None:
        st.caption("No call stood out clearly from the background in its "
                   "clips, so there is no pitch signature to show.")
        return
    centres = acoustics.band_centres_hz() / 1000
    parts = [pd.DataFrame({"khz": centres, "share": sig, "series": "This bird"})]
    text = (f"Across {len(ok)} of its clips, its calls are loudest around "
            f"**{_khz(peak)}**, with the middle half of their energy between "
            f"{_khz(lo)} and {_khz(hi)}")
    if baseline is not None:
        parts.append(pd.DataFrame({"khz": centres, "share": baseline,
                                   "series": "All birds"}))
        bpeak, blo, bhi = acoustics.signature_stats(baseline)
        if blo is not None:
            text += (f". Across all birds the middle half sits between "
                     f"{_khz(blo)} and {_khz(bhi)}")
    st.markdown("**Pitch signature.** " + text + ". It counts only sound "
                "that rises well above the background, so steady traffic and "
                "hum drop out.")
    long = pd.concat(parts)
    long = long[long["khz"] >= acoustics.SIGNATURE_MIN_HZ / 1000]
    st.altair_chart(alt.Chart(long).mark_line(strokeWidth=2, interpolate="monotone")
                    .encode(
        x=alt.X("khz:Q", title="Pitch (kHz)", scale=alt.Scale(domain=[0.5, 12]),
                axis=alt.Axis(gridColor="#e2eef0")),
        y=alt.Y("share:Q", title="Share of energy",
                axis=alt.Axis(format="%", gridColor="#e2eef0", tickCount=4)),
        color=_vs_all_color(), strokeDash=_vs_all_dash(),
        tooltip=[alt.Tooltip("series:N", title=""),
                 alt.Tooltip("khz:Q", title="kHz", format=".2f"),
                 alt.Tooltip("share:Q", title="Share", format=".1%")],
    ).properties(height=200), width="stretch", key=f"{where}_pitch")


def _profile_company(p):
    c = p["companions"]
    st.markdown("#### Often heard with")
    if c.empty:
        st.caption("No other bird is heard alongside it noticeably more often "
                   "than chance.")
        return
    st.dataframe(pd.DataFrame({
        "Species": c["common_name"],
        "Shared 15-minute windows": c["shared"],
        "Share of its windows": (c["share"] * 100).round(0).astype(int).astype(str) + "%",
        "Compared with chance": [f"{x:.1f}× as often" for x in c["lift"]],
    }), hide_index=True, width="stretch")


def _profile_reliability(p, where):
    st.markdown("#### How sure the model is")
    conf = p["confidence"]
    long = pd.concat([
        pd.DataFrame({"conf": conf["bin_start"], "share": conf["bird_share"],
                      "series": "This bird"}),
        pd.DataFrame({"conf": conf["bin_start"], "share": conf["all_share"],
                      "series": "All birds"})])
    long["range"] = [f"{c:.0%}–{min(c + 0.05, 1):.0%}" for c in long["conf"]]
    color = _vs_all_color()
    tip = [alt.Tooltip("series:N", title=""),
           alt.Tooltip("range:N", title="Confidence"),
           alt.Tooltip("share:Q", title="Share", format=".0%")]
    x = alt.X("conf:Q", title="Confidence", scale=alt.Scale(domain=[0.25, 1]),
              axis=alt.Axis(format="%", gridColor="#e2eef0"))
    y = alt.Y("share:Q", title="Share of detections",
              axis=alt.Axis(format="%", gridColor="#e2eef0", tickCount=4))
    bird = alt.Chart(long[long["series"] == "This bird"]).mark_bar(
        cornerRadiusTopLeft=3, cornerRadiusTopRight=3, opacity=0.9).encode(
        x=x, x2=alt.X2("x2:Q"), y=y, y2=alt.datum(0), color=color,
        tooltip=tip
    ).transform_calculate(x2="datum.conf + 0.045")
    everyone = alt.Chart(long[long["series"] == "All birds"]).mark_line(
        strokeWidth=2, interpolate="step-after", strokeDash=[5, 4]).encode(
        x=x, y=y, color=color, tooltip=tip)
    st.altair_chart(alt.layer(bird, everyone).properties(height=200),
                    width="stretch", key=f"{where}_conf")
    f = p["filter"]
    if f is not None:
        line = (f"The filters keep {int(f['kept']):,} of its {int(f['raw']):,} "
                f"raw detections ({f['pct_kept']:.0f}%).")
        if isinstance(f.get("top_removed_by"), str):
            line += (" Most removals come from "
                     f"{_test_label(f['top_removed_by']).lower()}.")
        if isinstance(f.get("override"), str):
            line += f" An admin override marks it **{f['override']}**."
        st.caption(line + " The Filter report tab lists every removed detection.")


def render_profile(name, filtered, where):
    """Draw one bird's profile. ``where`` prefixes element keys so the same
    bird can appear in the Birds tab and the pop-up together."""
    p = load_profile(_filter_key(), filtered, name)
    if not p["glance"]:
        st.info(f"No detections of {name} in this view.")
        return
    res = compute_filters(*_filter_key())
    _profile_header(name, p, res, filtered, where)
    _profile_tiles(p["glance"])
    _profile_season(p, where)
    left, right = st.columns(2)
    with left:
        _profile_day(p, where)
    with right:
        _profile_dawn(p)
        _profile_pattern(p)
    _profile_sound(p, where)
    left, right = st.columns(2)
    with left:
        _profile_company(p)
    with right:
        _profile_reliability(p, where)


@st.dialog("Bird profile", width="large")
def _profile_dialog(name):
    view = current_filters()[1]
    if name not in set(view["common_name"]):
        st.info(f"{name} is hidden by the false-positive filters. Choose Raw "
                "in the sidebar to see its profile.")
        return
    render_profile(name, filtered_mode, "dialog")
    st.caption(f"Share this profile: add ?bird={urllib.parse.quote_plus(name)} "
               "to the page address, or pick it in the Birds tab.")


with birds_tab:
    st.subheader("Birds")
    # Drawn only while this tab is open: a profile fetches audio clips for its
    # spectrograms, which the rest of the page shouldn't wait for.
    if birds_tab.open:
        _res, _view = current_filters()
        _options = bird_profile.species_options(_view)
        _asked = st.query_params.get("bird")
        if (filtered_mode and _asked and _asked not in _options
                and _asked in set(_res.detections["common_name"])):
            st.info(f"{_asked} is hidden by the false-positive filters. Choose "
                    "Raw in the sidebar to see its profile, or find it in the "
                    "Filter report tab.")
        if not _options:
            st.info("No birds to show yet.")
        else:
            _latest = bird_profile.latest_species(_view)
            _pick = st.selectbox(
                "Find a bird", _options, index=_options.index(_latest),
                key="bird", bind="query-params",
                placeholder="Type a name",
                help="Starts on the most recently heard bird. The page address "
                     "updates as you choose, so a profile can be bookmarked "
                     "or shared.")
            render_profile(_pick, filtered_mode, "tab")


# --- Filter report -------------------------------------------------------

def _after_admin_write(message):
    """After saving thresholds or an override: drop caches so every view
    picks up the change, and recompute the stored flags (used by the phone
    widget feed and SQL) right away rather than at the listener's next pass."""
    st.cache_data.clear()
    try:
        with st.spinner("Updating stored flags..."):
            result, _secs = filter_refresh.refresh()
        st.session_state.admin_msg = ("success", f"{message}. Stored flags "
                                      f"updated: {filter_refresh.summary(result)}.")
    except Exception as e:  # DB write errors come from psycopg2 too
        st.session_state.admin_msg = ("warning", f"{message}, but updating the "
                                      f"stored flags failed: {e}")
    st.rerun()


def _admin_thresholds():
    stored = load_stored_config()
    with st.expander("Tune thresholds (admin)", expanded=False):
        st.caption("Sliders change what you see straight away, for you only. "
                   "Save to make them the defaults everyone sees and to update "
                   "the stored flags the phone widget uses.")
        groups = list(filters.TESTS) + ["cadence"]
        names = {**filters.TEST_LABELS, "cadence": "Cadence (diagnostic)"}
        for group in groups:
            st.markdown(f"**{names[group]}**")
            for p in filters.PARAMS:
                if p.test != group:
                    continue
                key = f"cfg_{p.name}"
                if key not in st.session_state:
                    st.session_state[key] = stored[p.name]
                if p.kind is int:
                    st.slider(p.label, int(p.min), int(p.max), step=int(p.step),
                              key=key, help=p.description)
                else:
                    st.slider(p.label, float(p.min), float(p.max),
                              step=float(p.step), key=key, help=p.description)
        changed = {name: st.session_state[f"cfg_{name}"]
                   for name, value in stored.items()
                   if abs(st.session_state.get(f"cfg_{name}", value) - value) > 1e-9}
        c1, c2 = st.columns(2)
        if c1.button(f"Save as defaults ({len(changed)} changed)",
                     disabled=not changed, type="primary", key="cfg_save"):
            try:
                db.save_filter_config(changed)
            except SQLAlchemyError as e:
                st.error(f"Could not save thresholds: {e}")
            else:
                _after_admin_write(f"Saved {len(changed)} threshold"
                                   f"{'' if len(changed) == 1 else 's'}")
        if c2.button("Discard changes", disabled=not changed, key="cfg_discard"):
            for name in changed:
                del st.session_state[f"cfg_{name}"]
            st.rerun()


def _admin_overrides():
    with st.expander("Species overrides (admin)", expanded=False):
        st.caption("An override beats every test: confirmed keeps all of a "
                   "species' detections, rejected drops them all. Use it after "
                   "listening to a few clips, for example for a consistent "
                   "misidentification the shape tests cannot catch.")
        rows = load_overrides()
        if rows:
            st.dataframe(pd.DataFrame([
                {"Species": r["common_name"], "Verdict": r["status"],
                 "Note": r["note"] or "",
                 "Set": _fmt_date(r["updated_at"])} for r in rows]),
                hide_index=True, width="stretch")
        names = sorted(load_detections(load_data_version())["common_name"]
                       .dropna().unique())
        with st.form("override_form", clear_on_submit=True):
            name = st.selectbox("Species", names)
            verdict = st.radio("Verdict", ["confirmed", "rejected",
                                           "clear override"], horizontal=True)
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Save override"):
                status = None if verdict == "clear override" else verdict
                try:
                    db.set_species_override(name, status, note)
                except SQLAlchemyError as e:
                    st.error(f"Could not save the override: {e}")
                else:
                    _after_admin_write(f"Override {'cleared' if status is None else 'saved'}"
                                       f" for {name}")


with report_tab:
    st.subheader("Filter report")
    res, view = current_filters()
    det = res.detections
    sp = res.species
    keep = det["keep"].astype(bool)

    msg = st.session_state.pop("admin_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])

    st.caption(
        "BirdNET logs plenty of detections that are not birds: strings of "
        "mediocre guesses from machine noise, a second name on one sound, "
        "one-off misfires. Four tests judge the shape of the data and flag "
        "likely false positives. Nothing is deleted; Raw in the sidebar "
        "shows every detection.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Detections kept", f"{int(keep.sum()):,} of {len(det):,}")
    c2.metric("Species kept", f"{int((sp['kept'] > 0).sum())} of {len(sp)}")
    last = load_last_refresh()
    age = (f"{_humanize_age((config.now_local() - last).total_seconds())} ago"
           if last else "Never")
    c3.metric("Stored flags updated", age,
              help="When the listener (or an admin save) last stored the flags "
                   "that the phone widget uses. This page computes its own, "
                   "so it is always current.")
    if not res.enabled and len(det):
        st.info("Every test is switched off in the sidebar, so nothing is "
                "removed (manual species overrides still apply).")

    with st.expander("How the tests work"):
        cfg = res.config
        st.markdown(
            f"1. **{filters.TEST_LABELS['anchor']}.** A species' detections "
            f"less than {cfg['bout_gap_s']} s apart form a bout. A bout is kept "
            f"only if one detection in it reaches {cfg['anchor_conf']:.0%}.\n"
            f"2. **{filters.TEST_LABELS['shadow']}.** A detection is dropped "
            f"when another species scored higher within "
            f"{cfg['shadow_window_s']:g} s: two names on one sound. A species "
            f"with confident ({cfg['shadow_exempt_conf']:.0%}+) solo calls on "
            f"{cfg['shadow_exempt_days']} or more days is exempt, since its "
            "overlaps are real mixed flocks.\n"
            f"3. **{filters.TEST_LABELS['repeat']}.** A species is kept only if "
            f"it scores {cfg['strong_conf']:.0%}+ on {cfg['repeat_min_days']} "
            f"or more days, or {cfg['repeat_min_bout']} times in one bout (a "
            "flyover). Judged on detections that pass tests 1 and 2.\n"
            f"4. **{filters.TEST_LABELS['daypattern']}.** A species' whole day "
            f"is dropped when it spans {cfg['rtc_min_hours']}+ hours, including "
            f"{cfg['rtc_min_night_hours']}+ hours between "
            f"{cfg['rtc_night_start']}:00 and {cfg['rtc_night_end']}:00, and "
            f"never reaches {cfg['rtc_ceiling']:.0%}: machine noise read as a "
            "bird. Real night migrants can be caught too.\n\n"
            "**Cadence** is a diagnostic, not a filter: a slow, very regular "
            "rhythm within runs suggests something mechanical. The listener "
            "logs at most one detection per species per 15-second segment, so "
            "gaps under about 15 s never appear.\n\n"
            "**Overrides.** The shape tests cannot catch a species the model "
            "gets consistently wrong, so a confirmed or rejected verdict set by "
            "the admin wins over every test.")

    st.markdown("**Per-species summary**")
    if len(sp):
        st.dataframe(
            pd.DataFrame({
                "Species": sp["common_name"],
                "Raw": sp["raw"],
                "Kept": sp["kept"],
                "% kept": sp["pct_kept"],
                "Removed mostly by": [_test_label(t) for t in sp["top_removed_by"]],
                "Median gap (s)": sp["median_gap_s"],
                "Gap CV": sp["gap_cv"],
                "Mechanical rhythm": sp["cadence_flag"].astype(bool),
                "Mixed-flock exempt": sp["shadow_exempt"].astype(bool),
                "Override": sp["override"].fillna(""),
            }),
            hide_index=True, width="stretch",
            column_config={
                "% kept": st.column_config.ProgressColumn(
                    "% kept", min_value=0, max_value=100, format="%.0f%%"),
                "Median gap (s)": st.column_config.NumberColumn(format="%.1f"),
                "Gap CV": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    else:
        st.info("No detections yet.")

    st.markdown("**Removed detections**")
    removed = det[~keep]
    if len(removed):
        f1, f2, f3 = st.columns([2, 2, 1])
        species_opts = ["All species"] + sorted(removed["common_name"].unique())
        pick = f1.selectbox("Species", species_opts, key="rm_species")
        reasons = [r for r in list(filters.TESTS) + ["override"]
                   if (removed["removed_by"] == r).any()]
        why = f2.selectbox("Removed by", ["Any test"] + reasons, key="rm_reason",
                           format_func=lambda r: r if r == "Any test"
                           else _test_label(r))
        limit = int(f3.number_input("Show", min_value=50, max_value=5000,
                                    value=200, step=50, key="rm_limit"))
        sel = removed
        if pick != "All species":
            sel = sel[sel["common_name"] == pick]
        if why != "Any test":
            sel = sel[sel["removed_by"] == why]
        sel = sel.sort_values("detected_at", ascending=False)
        st.caption(f"{len(sel):,} removed detection{'' if len(sel) == 1 else 's'} "
                   f"match; showing the newest {min(limit, len(sel)):,}. Open a "
                   "clip to check the filters' judgement.")
        shown = sel.head(limit)
        st.dataframe(
            pd.DataFrame({
                "When": shown["detected_at"],
                "Species": shown["common_name"],
                "Confidence": (shown["confidence"] * 100).round().astype(int),
                "Removed by": [_test_label(t) for t in shown["removed_by"]],
                "All failed tests": [
                    ", ".join(_test_label(t) for t in str(f).split(","))
                    if pd.notna(f) else "" for f in shown["failed_tests"]],
                # Only where the shadow test actually removed it (a
                # mixed-flock exemption can spare a beaten detection).
                "Beaten by": shown["shadowed_by"].where(
                    ~shown["pass_shadow"].astype(bool)).fillna(""),
                "Clip": shown["clip_url"],
            }),
            hide_index=True, width="stretch",
            column_config={
                "When": st.column_config.DatetimeColumn(
                    format="MMM D, YYYY HH:mm:ss"),
                "Confidence": st.column_config.ProgressColumn(
                    "Confidence", min_value=0, max_value=100, format="%d%%"),
                "Clip": st.column_config.LinkColumn(
                    "Clip", display_text="▶ listen"),
            },
        )
    else:
        st.info("Nothing is removed with the current settings.")

    if _is_admin():
        _admin_thresholds()
        _admin_overrides()
    elif config.ADMIN_PASSWORD:
        st.caption("Thresholds and species overrides can be changed by the "
                   "site admin (sign in from the sidebar).")

# A bird clicked in a table or on a feed card opens in a pop-up (once).
_open_profile = st.session_state.pop("open_profile", None)
if _open_profile:
    _profile_dialog(_open_profile)
