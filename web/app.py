"""Birdwatcher dashboard: live feed, daily stats, life list, and clip playback.

Run with:
    python -m uvicorn web.app:app --reload      (dev)
    python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
Then open http://localhost:8000
"""
import datetime
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import db

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

app = FastAPI(title="uws_birdwatcher")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")


@app.on_event("startup")
def _startup():
    db.init_db()


# --- Template helpers ----------------------------------------------------

def _fmt_time(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso


def _fmt_date(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return iso


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_date"] = _fmt_date


def _today():
    return datetime.date.today().isoformat()


# --- Pages ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def feed(request: Request):
    conn = db.get_conn()
    try:
        rows = db.recent_detections(conn, limit=100)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "feed.html", {"rows": rows, "active": "feed"}
    )


@app.get("/partials/feed", response_class=HTMLResponse)
def feed_partial(request: Request):
    """Just the table rows, for HTMX auto-refresh."""
    conn = db.get_conn()
    try:
        rows = db.recent_detections(conn, limit=100)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "_feed_rows.html", {"rows": rows}
    )


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, day: str | None = None):
    day = day or _today()
    conn = db.get_conn()
    try:
        counts = db.counts_for_day(conn, day)
        hourly = db.hourly_counts_for_day(conn, day)
        days = db.distinct_days(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "stats.html",
        {
            "active": "stats", "day": day, "days": days,
            "counts": counts, "hourly": hourly,
            "total": sum(r["n"] for r in counts),
        },
    )


@app.get("/species", response_class=HTMLResponse)
def species(request: Request):
    conn = db.get_conn()
    try:
        rows = db.life_list(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "species.html", {"active": "species", "rows": rows}
    )


@app.get("/clips/{filename}")
def clip(filename: str):
    """Serve a saved audio clip (basename only, must exist in CLIPS_DIR)."""
    safe = os.path.basename(filename)
    path = os.path.join(config.CLIPS_DIR, safe)
    if not os.path.isfile(path):
        return PlainTextResponse("Clip not found", status_code=404)
    return FileResponse(path, media_type="audio/wav")
