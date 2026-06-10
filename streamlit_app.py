"""Birdwatcher dashboard (Streamlit) — live feed, daily stats, and life list.

Reads the shared PostgreSQL database written by the microphone listener, so it
works both locally and when hosted on Posit Connect Cloud (Streamlit framework,
primary file = this file).

Run locally with:
    streamlit run streamlit_app.py
"""
import datetime
import os

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import config
import db

st.set_page_config(page_title="uws_birdwatcher", page_icon="\U0001F426", layout="wide")

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


def _local_clip(name):
    """Absolute path to a clip if it exists on *this* machine, else None.
    Clips live only where they were recorded, so a cloud host has none."""
    if not name:
        return None
    path = os.path.join(config.CLIPS_DIR, os.path.basename(name))
    return path if os.path.isfile(path) else None


# --- Sidebar -------------------------------------------------------------

st.sidebar.title("\U0001F426 Birdwatcher")
st.sidebar.caption(
    f"Upper West Side, NYC · {config.LATITUDE}, {config.LONGITUDE}"
)
auto = st.sidebar.checkbox("Auto-refresh (10s)", value=True)
if auto:
    # Lightweight client-side rerun; no extra dependency needed.
    st.markdown(
        '<meta http-equiv="refresh" content="10">', unsafe_allow_html=True
    )
if st.sidebar.button("Refresh now"):
    st.rerun()


# --- Tabs ----------------------------------------------------------------

feed_tab, stats_tab, life_tab = st.tabs(
    ["Live feed", "Daily stats", "Life list"]
)

with feed_tab:
    st.subheader("Live feed")
    rows = _read(db.recent_detections, 100, default=[]) or []
    if not rows:
        st.info(
            "No detections yet. Start the listener "
            "(`python -m capture.listen`) and play some birdsong near the mic."
        )
    else:
        table = pd.DataFrame(
            [
                {
                    "Time": _fmt_time(r["detected_at"]),
                    "Species": r["common_name"],
                    "Scientific name": r["scientific_name"],
                    "Confidence": _pct(r["confidence"]),
                }
                for r in rows
            ]
        )
        st.dataframe(table, hide_index=True, width="stretch")

        clips = [(r, _local_clip(r["clip_path"])) for r in rows]
        clips = [(r, p) for r, p in clips if p]
        if clips:
            st.caption("Recent clips (available on the recording machine)")
            for r, path in clips[:10]:
                st.write(f"{_fmt_time(r['detected_at'])} — **{r['common_name']}**")
                st.audio(path)

with stats_tab:
    st.subheader("Daily stats")
    days = _read(db.distinct_days, default=[]) or []
    today = config.today_local().isoformat()
    options = [today] + [d for d in days if d != today]
    day = st.selectbox("Day", options, index=0)

    counts = _read(db.counts_for_day, day, default=[]) or []
    hourly = _read(db.hourly_counts_for_day, day, default=[0] * 24) or [0] * 24
    total = sum(r["n"] for r in counts)

    st.write(
        f"**{total}** detection{'' if total == 1 else 's'} on {day} "
        f"across **{len(counts)}** species."
    )

    st.markdown("**Activity by hour**")
    hourly_df = pd.DataFrame(
        {"Detections": hourly},
        index=[f"{h:02d}" for h in range(24)],
    )
    hourly_df.index.name = "Hour"
    st.bar_chart(hourly_df, color="#4c9a6a")

    st.markdown("**Species this day**")
    if counts:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Species": r["common_name"],
                        "Scientific name": r["scientific_name"],
                        "Count": r["n"],
                        "Best confidence": _pct(r["best"]),
                    }
                    for r in counts
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No detections on this day.")

with life_tab:
    st.subheader("Life list")
    rows = _read(db.life_list, default=[]) or []
    st.caption(f"{len(rows)} species heard")
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Species": r["common_name"],
                        "Scientific name": r["scientific_name"],
                        "First heard": _fmt_date(r["first_seen"]),
                        "Last heard": _fmt_date(r["last_seen"]),
                        "Times": r["total"],
                        "Best": _pct(r["best"]),
                    }
                    for r in rows
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No species heard yet.")
