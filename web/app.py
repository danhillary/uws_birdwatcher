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
from sqlalchemy.exc import SQLAlchemyError

import config
import db

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

app = FastAPI(title="uws_birdwatcher")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")

# Best-effort table creation at import. On an unreachable/read-only host (or
# before any detections exist) this may fail; reads below tolerate that.
try:
    db.init_db()
except SQLAlchemyError:
    pass


def _read(fn, *args, default=None):
    """Run a db read function, returning `default` if the DB/table isn't ready.
    Keeps the dashboard rendering an empty state instead of erroring."""
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


@app.on_event("startup")
def _startup():
    try:
        db.init_db()
    except SQLAlchemyError:
        pass


# --- Template helpers ----------------------------------------------------

def _as_dt(value):
    """Coerce a Postgres datetime/date or an ISO string to a datetime."""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    return datetime.datetime.fromisoformat(value)


def _fmt_time(value):
    try:
        return _as_dt(value).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return value


def _fmt_date(value):
    try:
        return _as_dt(value).strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return value


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_date"] = _fmt_date


def _today():
    return datetime.date.today().isoformat()


# --- Pages ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def feed(request: Request):
    rows = _read(db.recent_detections, 100, default=[])
    return templates.TemplateResponse(
        request, "feed.html", {"rows": rows, "active": "feed"}
    )


@app.get("/partials/feed", response_class=HTMLResponse)
def feed_partial(request: Request):
    """Just the table rows, for HTMX auto-refresh."""
    rows = _read(db.recent_detections, 100, default=[])
    return templates.TemplateResponse(
        request, "_feed_rows.html", {"rows": rows}
    )


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, day: str | None = None):
    day = day or _today()
    counts = _read(db.counts_for_day, day, default=[])
    hourly = _read(db.hourly_counts_for_day, day, default=[0] * 24)
    days = _read(db.distinct_days, default=[])
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
    rows = _read(db.life_list, default=[])
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
