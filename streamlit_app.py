"""Birdwatcher dashboard (Streamlit) — live feed, daily stats, life list, and
the false-positive filter report.

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
import filter_refresh
import filters

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


@st.cache_data(ttl=60, show_spinner=False)
def load_detections():
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


@st.cache_data(ttl=60, show_spinner=False)
def compute_filters(cfg_items, enabled, override_items):
    """filters.compute on the cached detections, keyed by the filter setting."""
    return filters.compute(load_detections(), dict(cfg_items), enabled,
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

_last_at = load_detections()["detected_at"].max()
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


def current_filters():
    """(filters.Result for the current settings, the rows to display).

    The rows are the kept detections in Filtered mode and all of them in Raw.
    """
    overrides = {r["common_name"]: r["status"] for r in load_overrides()}
    res = compute_filters(tuple(sorted(effective_config().items())),
                          enabled_tests(), tuple(sorted(overrides.items())))
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

feed_tab, stats_tab, life_tab, report_tab = st.tabs(
    ["Live feed", "Daily stats", "Life list", "Filter report"])

with feed_tab:
    st.subheader("Live feed")

    st.caption("Back-to-back repeats of the same species are grouped into one "
               "card (best clip shown). Tap \U0001F44D / \U0001F44E to tell us "
               "whether a clip matches the bird. The feed shows every "
               "detection as it arrives; in Filtered mode, cards the "
               "false-positive filters drop are marked.")

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
        removed = {}
        if filtered_mode:
            det = current_filters()[0].detections
            hit = det[det["id"].isin(ids) & ~det["keep"].astype(bool)]
            removed = dict(zip(hit["id"], hit["removed_by"]))
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
        names = sorted(load_detections()["common_name"].dropna().unique())
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
