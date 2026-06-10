"""Central configuration. Override any value with an environment variable."""
import os

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
