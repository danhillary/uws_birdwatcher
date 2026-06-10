"""Central configuration. Override any value with an environment variable."""
import datetime
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Where the project lives, so paths work regardless of the current directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load a local .env if present (no-op in the cloud, where vars come from the
# host). Looks for .env next to this file regardless of current directory.
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Location — used by BirdNET to filter to species likely near you.
# Default: Upper West Side, Manhattan.
LATITUDE = float(os.environ.get("BW_LAT", "40.785"))
LONGITUDE = float(os.environ.get("BW_LON", "-73.975"))

# Minimum confidence (0-1) for a detection to be reported.
MIN_CONFIDENCE = float(os.environ.get("BW_MIN_CONF", "0.25"))

# Audio capture settings. BirdNET expects 48 kHz mono.
SAMPLE_RATE = int(os.environ.get("BW_SAMPLE_RATE", "48000"))
CHANNELS = 1

# Length of each recorded segment, in seconds. BirdNET analyses each segment
# internally in 3-second windows, so a longer segment is more efficient and
# avoids clipping calls at boundaries.
SEGMENT_SECONDS = int(os.environ.get("BW_SEGMENT_SECONDS", "15"))

# Input device: either an index (e.g. "2") or part of a device name
# (e.g. "MacBook" or "UAC"). Selecting by name is robust to index shuffling
# when devices connect/disconnect. Empty = system default input.
# See `python -m capture.list_devices`.
INPUT_DEVICE = os.environ.get("BW_INPUT_DEVICE") or "UAC"

# --- Storage (Phase 2) ---------------------------------------------------

# SQLite database file holding every detection.
DB_PATH = os.environ.get("BW_DB_PATH") or os.path.join(BASE_DIR, "birdwatcher.db")

# Directory holding saved audio clips, one short WAV per detection.
CLIPS_DIR = os.environ.get("BW_CLIPS_DIR") or os.path.join(BASE_DIR, "clips")

# Seconds of padding kept on each side of a detection when saving its clip.
CLIP_PADDING_SECONDS = float(os.environ.get("BW_CLIP_PADDING", "1.0"))

# Storage cap for the clips directory, in megabytes. When exceeded, the oldest
# clips are deleted (detection rows are always kept). 0 disables pruning.
MAX_CLIPS_MB = int(os.environ.get("BW_MAX_CLIPS_MB", "2000"))

# --- Timezone (Phase 5) --------------------------------------------------

# Detections are stored as timezone-naive *local* timestamps so that "today"
# and per-hour buckets are correct no matter where the dashboard runs (Posit
# Connect Cloud is likely UTC). Default to NYC; override for another location.
TIMEZONE = os.environ.get("BW_TIMEZONE", "America/New_York")
_TZ = ZoneInfo(TIMEZONE)


def now_local():
    """Current wall-clock time in TIMEZONE, as a *naive* datetime.

    We strip tzinfo so it round-trips cleanly through a Postgres TIMESTAMP
    (without time zone) column and compares correctly against today_local()."""
    return datetime.datetime.now(_TZ).replace(tzinfo=None)


def today_local():
    """Today's date in TIMEZONE (a datetime.date)."""
    return datetime.datetime.now(_TZ).date()


# --- Database (Phase 5) --------------------------------------------------

# Postgres connection. Either provide a full SQLAlchemy URL via
# BW_DATABASE_URL, or the individual DB_* parts (matching the ceqr pattern).
# Credentials come from the environment / .env only — never commit them.
DATABASE_URL = os.environ.get("BW_DATABASE_URL")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "ceqr")
DB_USER = os.environ.get("DB_USER", "")
DB_PASS = os.environ.get("DB_PASS", "")

# Schema that holds the birdwatcher tables, isolated from any other app
# sharing the database (e.g. CEQR lives in its own schema).
DB_SCHEMA = os.environ.get("BW_DB_SCHEMA", "birdwatcher")


def database_url():
    """SQLAlchemy URL for the Postgres database (psycopg2 driver)."""
    if DATABASE_URL:
        return DATABASE_URL
    auth = DB_USER
    if DB_PASS:
        auth = f"{DB_USER}:{DB_PASS}"
    return f"postgresql+psycopg2://{auth}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
