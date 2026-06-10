# uws_birdwatcher

Point a microphone out the window, identify the birds you hear with
[BirdNET](https://github.com/kahst/BirdNET-Analyzer) (Cornell Lab), and show them
on a live dashboard — **The Ramble Register**.

The full pipeline is running: a microphone next to the window feeds BirdNET,
which writes each identified bird to a shared PostgreSQL database; a Streamlit
dashboard hosted on Posit Connect Cloud reads that database and shows the live
feed, daily stats, and a life list. Detections are filtered to species likely at
your location and time of year.

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

Each detection is also saved to a PostgreSQL database (in the `birdwatcher`
schema — see Database below) with a short audio clip in `clips/`. Stop with
Ctrl-C. To test without live birds, play a birdsong clip near the mic.

### Run it in the background on Windows (autostart on logon)

The listener needs the microphone, which is tied to your interactive Windows
session — so run it as a **scheduled task in your user session**, not as a
Windows Service (Session 0 services can't reach the mic).

`run_listener.vbs` (in the repo root) starts the listener hidden and appends its
output to `listener.log`. Double-click it to start now, or autostart it on logon:

1. Open **Task Scheduler** → **Create Task…** (not "Basic Task").
2. **General:** name it "Birdwatcher listener"; select **Run only when user is
   logged on** (this is what gives it microphone access).
3. **Triggers:** New → **At log on** → (optionally your user). Tick **Enabled**.
4. **Actions:** New → Program/script: `wscript.exe`; Add arguments:
   `"C:\path\to\uws_birdwatcher\run_listener.vbs"`.
5. **Settings:** tick **If the task fails, restart every 1 minute** and **If the
   running task does not end when requested, force it to stop**; untick **Stop
   the task if it runs longer than…** so it runs indefinitely.
6. Save. It launches at every logon; check `listener.log` to confirm detections.

To check it's running: Task Manager → Details → look for `python.exe`. To stop
it: end that `python.exe` (or disable the task). Also set **Power & sleep** so
the desktop doesn't sleep, or it stops recording.

> Want it running *before* you log in (e.g. headless reboots)? Either enable
> Windows auto-login for that account, or use a tool like
> [NSSM](https://nssm.cc/) to run it as a service — but verify the service can
> actually access the mic, since Session 0 often can't.

## Dashboard

The dashboard — **The Ramble Register**, styled in a light "Central Park
Morning" theme — is a [Streamlit](https://streamlit.io/) app. In a **second
terminal** (leave the listener running in the first), start it:

```bash
# Windows:  .venv\Scripts\streamlit run streamlit_app.py
# macOS:    .venv/bin/streamlit run streamlit_app.py
```

Streamlit opens **http://localhost:8501** automatically. Three tabs:

- **Live feed** — most recent detections, auto-refreshing, with playable clips
- **Daily stats** — per-species counts and an activity-by-hour chart (pick a day)
- **Life list** — every species ever heard, first/last heard, totals

The listener and dashboard share the same database, so the dashboard updates as
new birds are detected. Audio-clip playback works on the machine that recorded
the clips (the files stay local).

## Database

Detections are stored in **PostgreSQL**, in a dedicated `birdwatcher` schema so
the database can be shared with other apps without colliding. Both the listener
and the dashboard connect using the `DB_*` environment variables (see
Configuration). This is what lets a cloud-hosted dashboard show detections
recorded on your home machine: the listener writes to the shared database and
the dashboard reads from it — the **data bridge**.

Copy `.env.example` to `.env` and fill in your connection details:

```bash
cp .env.example .env   # then edit .env with your DB host/name/user/password
```

`.env` is gitignored — credentials are never committed. The first run creates
the schema, table, and indexes automatically. Audio-clip files stay on the
machine that recorded them (clip playback is a local-only feature for now).

## Host the dashboard online (Posit Connect Cloud)

The dashboard can be published to [Posit Connect Cloud](https://connect.posit.cloud/),
which builds straight from this GitHub repo:

1. At connect.posit.cloud, click **Publish** → **From GitHub** and choose the
   **Streamlit** framework.
2. Pick this repository and branch.
3. Set the **primary file** to `streamlit_app.py`.
4. Publish. Connect Cloud installs `requirements.txt` (the slim dashboard deps —
   *not* the BirdNET/microphone stack) and serves the app. It auto-redeploys on
   every push.
5. In Connect Cloud's **Variables**, set `DB_HOST`, `DB_PORT`, `DB_NAME`,
   `DB_USER`, `DB_PASS`, and `BW_DB_SCHEMA=birdwatcher` so the dashboard reads
   the shared database.

**The data bridge.** The microphone listener runs on your home machine and
writes to the shared PostgreSQL database; the cloud-hosted dashboard reads from
that same database, so detections recorded at home appear online. A freshly
deployed dashboard renders but stays empty until the listener records something.
Audio-clip playback remains a local-only feature unless clips are also uploaded
to cloud storage. See the roadmap.

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
| `DB_HOST`           | `localhost`        | PostgreSQL host                                |
| `DB_PORT`           | `5432`             | PostgreSQL port                                |
| `DB_NAME`           | `ceqr`             | PostgreSQL database name                       |
| `DB_USER`           | *(empty)*          | PostgreSQL user                                |
| `DB_PASS`           | *(empty)*          | PostgreSQL password                            |
| `BW_DATABASE_URL`   | *(empty)*          | Full SQLAlchemy URL (overrides the `DB_*` set) |
| `BW_DB_SCHEMA`      | `birdwatcher`      | Schema holding the detections table            |
| `BW_TIMEZONE`       | `America/New_York` | Local TZ for "today"/hourly buckets            |
| `BW_CLIPS_DIR`      | `clips/`           | Folder for saved audio clips                   |
| `BW_MAX_CLIPS_MB`   | `2000`             | Clip storage cap; oldest clips pruned over it  |

Example (PowerShell): `$env:BW_INPUT_DEVICE="UAC"; .venv\Scripts\python -m capture.listen`

> **Update `BW_LAT`/`BW_LON` if the Windows machine is somewhere else** — BirdNET
> uses location to decide which species are plausible.

## Project layout

```
config.py              # central config (location, mic, thresholds, DB, timezone)
analysis.py            # reusable BirdNET wrapper
db.py                  # PostgreSQL storage + queries (SQLAlchemy)
storage.py             # clip saving + storage-cap pruning
streamlit_app.py       # Streamlit dashboard (also the Connect Cloud primary file)
capture/
  list_devices.py      # list microphones
  listen.py            # record -> identify -> save to DB + clips
requirements.txt       # slim dashboard deps (Connect Cloud installs these)
requirements-listen.txt# adds the BirdNET + microphone stack (home machine only)
.env.example           # template for DB credentials (copy to .env)
```

## Roadmap

- **Phase 1** — microphone → BirdNET → terminal ✅
- **Phase 2** — SQLite storage + audio clips + storage-cap pruning ✅
- **Phase 3** — dashboard: live feed, daily stats, life list, clip playback ✅
- **Phase 4 (maybe later)** — Docker packaging. Deferred: the native install
  works fine on Windows, so it isn't needed yet.
- **Phase 5 (done)** — host the dashboard on Posit Connect Cloud. Storage moved
  to PostgreSQL (`birdwatcher` schema) so the home listener and cloud dashboard
  share one database ✅; the dashboard was rebuilt in Streamlit (Connect Cloud
  doesn't host raw FastAPI) and branded as **The Ramble Register** ✅; deployed to
  Connect Cloud with the `DB_*`/`BW_DB_SCHEMA` env vars set ✅; the Windows
  listener runs in the background and writes to the shared database, so home
  detections appear online — the **data bridge** is live end-to-end ✅.
- **Phase 6 (maybe later)** — optional cloud audio storage (e.g. S3) so clip
  playback works from the hosted dashboard, not just the recording machine.
