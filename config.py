"""Central configuration. Override any value with an environment variable."""
import os

# Where the project lives, so paths work regardless of the current directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
