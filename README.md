# uws_birdwatcher

Point a microphone out the window, identify the birds you hear with
[BirdNET](https://github.com/kahst/BirdNET-Analyzer) (Cornell Lab), and
(eventually) show them on a dashboard.

**Phase 1 (done):** microphone → BirdNET → live detections printed to the
terminal, filtered to species likely at your location and time of year.

## Requirements

- **Python 3.11** specifically. TensorFlow / birdnetlib do not yet have wheels
  for 3.12+. (Newer or older Python will fail to install the dependencies.)
- A microphone. A USB mic works best for pointing out a window.
- No other system packages are needed on Windows or macOS — the audio and model
  libraries bundle their own native code.

## Setup

### Windows

Install Python 3.11 from [python.org](https://www.python.org/downloads/release/python-3119/)
(tick "Add python.exe to PATH"), then in PowerShell from the project folder:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-listen.txt
```

### macOS / Linux

```bash
# macOS: install Python 3.11 if needed -> brew install python@3.11
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -r requirements-listen.txt
```

> First run downloads/initialises the BirdNET model and takes ~10–20s.

> **Two requirements files:** `requirements.txt` holds the lightweight dashboard
> deps; `requirements-listen.txt` adds the microphone + BirdNET stack. The
> machine next to the mic needs both (as above). A host that only runs the
> dashboard (e.g. Posit Connect Cloud) installs just `requirements.txt`.

## Pick your microphone

List the available input devices and their indexes:

```bash
# Windows:  .venv\Scripts\python -m capture.list_devices
# macOS:    .venv/bin/python -m capture.list_devices
```

You can select a device by **name** (robust — survives index changes when
devices connect/disconnect) or by index. See Configuration below.

## Run

```bash
# Windows:  .venv\Scripts\python -m capture.listen
# macOS:    .venv/bin/python -m capture.listen
```

It records ~15-second segments and prints any detections, e.g.:

```
[14:32:07] 🐦 American Robin (Turdus migratorius) — 78%  -> 20260609T143207_American_Robin.wav
```

Each detection is also saved to a SQLite database (`birdwatcher.db`) with a short
audio clip in `clips/`. Stop with Ctrl-C. To test without live birds, play a
birdsong clip near the mic.

## Dashboard

In a **second terminal** (leave the listener running in the first), start the
web dashboard:

```bash
# Windows:  .venv\Scripts\python -m uvicorn web.app:app --port 8000
# macOS:    .venv/bin/python  -m uvicorn web.app:app --port 8000
```

Open **http://localhost:8000**. Three views:

- **Live feed** — most recent detections, auto-refreshing, with playable clips
- **Daily stats** — per-species counts and an activity-by-hour chart (pick a day)
- **Life list** — every species ever heard, first/last heard, totals

The listener and dashboard share the same database, so the dashboard updates as
new birds are detected. (The dashboard loads htmx and Chart.js from a CDN, so it
needs internet access in the browser; everything else runs locally.)

## Host the dashboard online (Posit Connect Cloud)

The dashboard can be published to [Posit Connect Cloud](https://connect.posit.cloud/),
which builds straight from this GitHub repo:

1. At connect.posit.cloud, click **Publish** and choose the **FastAPI** framework.
2. Pick this repository and branch.
3. Set the **primary file** to `app.py` (it exposes the FastAPI `app`).
4. Publish. Connect Cloud installs `requirements.txt` (the slim dashboard deps —
   *not* the BirdNET/microphone stack) and serves the app. It auto-redeploys on
   every push.

**Important — the data lives where the mic is.** The microphone listener runs on
your home machine and writes to a *local* database; a cloud-hosted dashboard
can't see that. So a freshly deployed dashboard renders but shows no detections
until a **data bridge** sends them up (e.g. the listener writing to a hosted
database that the dashboard reads). Audio-clip playback is a local-only feature
unless clips are also uploaded to cloud storage. See the roadmap.

## Configuration

All settings live in `config.py` and can be overridden with environment
variables:

| Variable            | Default            | Meaning                                        |
|---------------------|--------------------|------------------------------------------------|
| `BW_INPUT_DEVICE`   | `UAC`              | Mic name substring (e.g. `MacBook`) or index   |
| `BW_LAT`            | `40.785`           | Latitude (default: Upper West Side, NYC)       |
| `BW_LON`            | `-73.975`          | Longitude                                      |
| `BW_MIN_CONF`       | `0.25`             | Minimum confidence (0–1) to report a detection |
| `BW_SEGMENT_SECONDS`| `15`               | Length of each recorded segment                |
| `BW_SAMPLE_RATE`    | `48000`            | Sample rate (BirdNET expects 48 kHz)           |
| `BW_DB_PATH`        | `birdwatcher.db`   | SQLite database file                           |
| `BW_CLIPS_DIR`      | `clips/`           | Folder for saved audio clips                   |
| `BW_MAX_CLIPS_MB`   | `2000`             | Clip storage cap; oldest clips pruned over it  |

Example (PowerShell): `$env:BW_INPUT_DEVICE="UAC"; .venv\Scripts\python -m capture.listen`

> **Update `BW_LAT`/`BW_LON` if the Windows machine is somewhere else** — BirdNET
> uses location to decide which species are plausible.

## Project layout

```
config.py              # central config (location, mic, thresholds)
capture/
  list_devices.py      # list available microphones
  listen.py            # Phase 1: mic -> BirdNET -> terminal
requirements.txt
```

## Project layout (updated)

```
config.py              # central config
analysis.py            # reusable BirdNET wrapper
db.py                  # SQLite storage + queries
storage.py             # clip saving + storage-cap pruning
capture/
  list_devices.py      # list microphones
  listen.py            # record -> identify -> save to DB + clips
web/
  app.py               # FastAPI dashboard
  templates/  static/  # pages and styles
```

## Roadmap

- **Phase 1** — microphone → BirdNET → terminal ✅
- **Phase 2** — SQLite storage + audio clips + storage-cap pruning ✅
- **Phase 3** — dashboard: live feed, daily stats, life list, clip playback ✅
- **Phase 4 (maybe later)** — Docker packaging. Deferred: the native install
  works fine on Windows, so it isn't needed yet.
- **Phase 5 (in progress)** — host the dashboard on Posit Connect Cloud. App is
  deploy-ready; next is the data bridge (home listener → hosted database →
  cloud dashboard), then optional cloud audio storage for clip playback.
