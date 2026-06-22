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

# --- Privacy: human-voice filtering --------------------------------------

# BirdNET's model includes non-bird "noise" classes, among them "Human vocal",
# "Human non-vocal", and "Human whistle". When this is on, every segment is
# analysed with those classes visible; if a human voice is heard above
# HUMAN_VOICE_CONF, the segment's clips are kept on the recording machine only
# and are never uploaded to the public S3 bucket. The bird detection itself is
# still recorded — only the audio is withheld from the cloud.
FILTER_HUMAN_VOICE = os.environ.get("BW_FILTER_HUMAN_VOICE", "1") != "0"
HUMAN_VOICE_CONF = float(os.environ.get("BW_HUMAN_VOICE_CONF", "0.3"))

# The model's non-bird label common-names. HUMAN_LABELS triggers the privacy
# hold; the rest are simply never treated as bird detections.
HUMAN_LABELS = {"Human vocal", "Human non-vocal", "Human whistle"}
NON_BIRD_LABELS = HUMAN_LABELS | {
    "Dog", "Engine", "Environmental", "Fireworks", "Gun", "Noise",
    "Power tools", "Siren",
}

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

# Local cache for clips that can't go to the public bucket: human-voice holds
# and any clip whose S3 upload failed. Bird clips live in S3 only and never land
# here (see clips_s3 / capture.listen).
CLIPS_DIR = os.environ.get("BW_CLIPS_DIR") or os.path.join(BASE_DIR, "clips")

# Seconds of padding kept on each side of a detection when saving its clip.
CLIP_PADDING_SECONDS = float(os.environ.get("BW_CLIP_PADDING", "1.0"))

# Storage cap for the local clips cache, in megabytes. When exceeded, the oldest
# *local* clips are deleted (detection rows are always kept; S3 copies are never
# pruned). 0 disables pruning.
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
# BW_DATABASE_URL, or the individual DB_* parts below.
# Credentials come from the environment / .env only — never commit them.
DATABASE_URL = os.environ.get("BW_DATABASE_URL")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "birdwatcher")
DB_USER = os.environ.get("DB_USER", "")
DB_PASS = os.environ.get("DB_PASS", "")

# Schema that holds the birdwatcher tables, isolated from any other app
# that might share the database (each app keeps to its own schema).
DB_SCHEMA = os.environ.get("BW_DB_SCHEMA", "birdwatcher")


def database_url():
    """SQLAlchemy URL for the Postgres database (psycopg2 driver)."""
    if DATABASE_URL:
        return DATABASE_URL
    auth = DB_USER
    if DB_PASS:
        auth = f"{DB_USER}:{DB_PASS}"
    return f"postgresql+psycopg2://{auth}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# --- iPhone widget feed (optional) ---------------------------------------

# When BW_FEED_S3_BUCKET is set, the listener publishes a small public JSON
# file (latest detection, today's totals, listener health) to S3 so a phone
# widget can read it without ever touching the database. Leave the bucket empty
# to disable the feed entirely. AWS credentials come from the standard
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars (or an AWS profile).
FEED_S3_BUCKET = os.environ.get("BW_FEED_S3_BUCKET", "")
FEED_S3_KEY = os.environ.get("BW_FEED_S3_KEY", "birdwatcher/latest.json")
FEED_S3_REGION = os.environ.get("BW_FEED_S3_REGION", "")
# Only set this if the bucket still uses ACLs (e.g. "public-read"). Buckets with
# "Bucket owner enforced" object ownership reject ACLs — use a bucket policy for
# public read instead and leave this empty.
FEED_S3_ACL = os.environ.get("BW_FEED_S3_ACL", "")
# Minimum seconds between feed publishes (the listener loops faster than this).
FEED_INTERVAL = int(os.environ.get("BW_FEED_INTERVAL", "60"))

# --- Audio clips in the dashboard ----------------------------------------

# When the feed bucket is set, the listener uploads each detection's audio clip
# to S3 (under CLIPS_S3_PREFIX) so the cloud dashboard can play it. Bird clips
# live in S3 only — they are uploaded from memory and never written to local
# disk, and never deleted from the bucket. Reuses the feed's bucket, region,
# ACL, and AWS credentials. Set BW_PUBLISH_CLIPS=0 to keep the JSON feed but
# never upload audio (clips then fall back to local disk, like a human-voice
# hold). Clips flagged as containing a human voice are never uploaded regardless
# (see FILTER_HUMAN_VOICE) — they stay on the recording machine.
PUBLISH_CLIPS = os.environ.get("BW_PUBLISH_CLIPS", "1") != "0"
CLIPS_S3_PREFIX = os.environ.get("BW_CLIPS_S3_PREFIX", "birdwatcher/clips").strip("/")

# --- Clip voting (dashboard) ---------------------------------------------

# Listeners can up/down-vote whether a clip actually matches its species label.
# A detection whose net score (upvotes - downvotes) falls to or below this
# threshold is flagged "Disputed" in the live feed (it is still shown, never
# auto-deleted). Lower = more tolerant; raise toward 0 to flag sooner.
# NOTE: voting WRITES to the database, so the dashboard's DB user needs
# INSERT/UPDATE on the birdwatcher schema (read-only credentials disable voting).
DISPUTED_THRESHOLD = int(os.environ.get("BW_DISPUTED_THRESHOLD", "-3"))
