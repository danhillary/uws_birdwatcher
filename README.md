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
.venv\Scripts\python -m pip install -r requirements.txt
```

### macOS / Linux

```bash
# macOS: install Python 3.11 if needed -> brew install python@3.11
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

> First run downloads/initialises the BirdNET model and takes ~10–20s.

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
[14:32:07] 🐦 American Robin (Turdus migratorius) — 78%
```

Stop with Ctrl-C. To test without live birds, play a birdsong clip near the mic.

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

## Roadmap

- **Phase 2** — save detections to SQLite + store an audio clip per bird
- **Phase 3** — FastAPI dashboard: live feed, daily stats, life list, clip playback
- **Phase 4** — Docker packaging (native capture client + dockerized analyzer/dashboard)
