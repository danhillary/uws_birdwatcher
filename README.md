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

Below the summary cards a **listener health** pill shows whether the recording
machine is alive: 🟢 listening (with the live mic level), 🟡 running but the mic
is silent (likely unplugged/muted), or 🔴 offline (no heartbeat — crashed or
asleep). The listener writes a heartbeat every segment to a small
`listener_status` table, so a quiet-but-healthy setup is easy to tell apart from
a dead one.

The listener and dashboard share the same database, so the dashboard updates as
new birds are detected. **Clip playback in the cloud** requires the clips to be
reachable from the dashboard host: when the S3 feed bucket is configured (see
below), the listener uploads each bird clip there — straight from memory, so it
lives in S3 only and never on local disk — and the dashboard plays it from a
public URL. Without a bucket (or with `BW_PUBLISH_CLIPS=0`), clips fall back to
local files on the recording machine, where playback still works.

### Privacy: human voices are kept out of the cloud

BirdNET's model can recognise a human voice (its non-bird "Human vocal" classes).
By default (`BW_FILTER_HUMAN_VOICE=1`), if a voice is heard in a segment, that
segment's clips are **kept on the recording machine only** and never uploaded to
the public bucket — the bird detection is still recorded, just without cloud
audio. Tune `BW_HUMAN_VOICE_CONF` (default `0.3`) lower to be stricter.

### Clip voting (crowd checking)

The live feed shows each recent detection as a card with its clip and 👍 / 👎
buttons, so visitors can flag clips that don't match the species (e.g. a
jackhammer logged as a woodpecker). Votes are stored per browser session (one
vote each, changeable); a detection whose net score drops to
`BW_DISPUTED_THRESHOLD` (default `-3`) is labelled **Disputed ⚠** in the feed but
never auto-deleted.

Voting **writes** to the database, so the dashboard's DB user needs
`INSERT`/`UPDATE` on the `birdwatcher` schema (the `clip_votes` table is created
by `init_db`). With a read-only user, the feed still works and just shows a
"voting unavailable" note.

## iPhone widget (optional)

A Scriptable widget can show the **latest bird** — its photo, when it was heard,
today's tally, and a listener-health dot — right on your home or lock screen.

It reads a small public JSON file the listener publishes to S3, so the phone
never touches the database. The feature is **off unless `BW_FEED_S3_BUCKET` is
set** (see Configuration).

**1. Make an S3 home for the feed.** Create a bucket (or reuse one) and decide a
key like `birdwatcher/latest.json`. The object must be **publicly readable**.
Modern buckets disable ACLs, so add a bucket policy granting public read to that
prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadBirdwatcherFeed",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::YOUR-BUCKET/birdwatcher/*"
  }]
}
```

**2. Give the listener AWS credentials** with `s3:PutObject` on that prefix.
(Clips are never deleted from the bucket, so no `s3:DeleteObject` is needed.)
Add to the listener's `.env`:

```
BW_FEED_S3_BUCKET=YOUR-BUCKET
BW_FEED_S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Install boto3 (it's in `requirements-listen.txt`). Test it once:

```bash
# Windows:  .venv\Scripts\python -m feed
# macOS:    .venv/bin/python -m feed
```

That prints the JSON and uploads it. Confirm the public URL loads in a browser:
`https://YOUR-BUCKET.s3.amazonaws.com/birdwatcher/latest.json`. From then on the
running listener refreshes it about once a minute (`BW_FEED_INTERVAL`).

**3. Add the widget.** Install **[Scriptable](https://scriptable.app)** (free) on
the iPhone, create a new script, and paste in `widget/RambleRegister.js`.
`FEED_URL` and `DASHBOARD_URL` at the top are pre-filled with this project's
public feed and dashboard (tapping the widget opens the dashboard) — change them
only if you run your own instance. Run it once to grant network access, then
long-press the home screen → ＋ → Scriptable → add a **Medium** widget and point
it at the script. (Full setup notes are in the file's header.)

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
the schema, table, and indexes automatically. With an S3 bucket configured, bird
clips are stored in the cloud (and play in the hosted dashboard); without one,
clips stay on the machine that recorded them.

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
Bird-clip playback works in the cloud once an S3 bucket is configured (clips are
uploaded there and never written to local disk); otherwise playback is
local-only. See the iPhone-widget section for the bucket setup.

## Configuration

All settings live in `config.py` and can be overridden with environment
variables:

| Variable            | Default            | Meaning                                        |
|---------------------|--------------------|------------------------------------------------|
| `BW_INPUT_DEVICE`   | `UAC`              | Mic name substring (e.g. `MacBook`) or index   |
| `BW_LAT`            | `40.785`           | Latitude (default: Upper West Side, NYC)       |
| `BW_LON`            | `-73.975`          | Longitude                                      |
| `BW_MIN_CONF`       | `0.5`              | Global minimum confidence (0–1) to report a detection |
| `BW_SPECIES_MIN_CONF`| *(see below)*     | JSON per-species confidence overrides          |
| `BW_SEGMENT_SECONDS`| `15`               | Length of each recorded segment                |
| `BW_SAMPLE_RATE`    | `48000`            | Sample rate (BirdNET expects 48 kHz)           |
| `DB_HOST`           | `localhost`        | PostgreSQL host                                |
| `DB_PORT`           | `5432`             | PostgreSQL port                                |
| `DB_NAME`           | `birdwatcher`      | PostgreSQL database name                       |
| `DB_USER`           | *(empty)*          | PostgreSQL user                                |
| `DB_PASS`           | *(empty)*          | PostgreSQL password                            |
| `BW_DATABASE_URL`   | *(empty)*          | Full SQLAlchemy URL (overrides the `DB_*` set) |
| `BW_DB_SCHEMA`      | `birdwatcher`      | Schema holding the detections table            |
| `BW_TIMEZONE`       | `America/New_York` | Local TZ for "today"/hourly buckets            |
| `BW_CLIPS_DIR`      | `clips/`           | Local cache for clips not on S3 (voice holds, upload fallbacks) |
| `BW_MAX_CLIPS_MB`   | `2000`             | Local clip-cache cap; oldest local clips pruned over it (S3 never pruned) |
| `BW_FEED_S3_BUCKET` | *(empty)*          | S3 bucket for the iPhone-widget feed; empty = off |
| `BW_FEED_S3_KEY`    | `birdwatcher/latest.json` | S3 key for the published JSON feed      |
| `BW_FEED_S3_REGION` | *(empty)*          | AWS region for the feed bucket                 |
| `BW_FEED_S3_ACL`    | *(empty)*          | S3 ACL (e.g. `public-read`); omit if ACLs disabled |
| `BW_FEED_INTERVAL`  | `60`               | Min seconds between feed publishes             |
| `BW_PUBLISH_CLIPS`  | `1`                | Upload clips to S3 for cloud playback (`0` = off) |
| `BW_CLIPS_S3_PREFIX`| `birdwatcher/clips`| S3 key prefix for uploaded clips               |
| `BW_FILTER_HUMAN_VOICE` | `1`            | Keep clips with a human voice off the public bucket |
| `BW_HUMAN_VOICE_CONF` | `0.3`            | Confidence at which a human voice triggers the hold |
| `BW_DISPUTED_THRESHOLD` | `-3`           | Net clip-vote score at/below which a detection is flagged "Disputed" |

Example (PowerShell): `$env:BW_INPUT_DEVICE="UAC"; .venv\Scripts\python -m capture.listen`

> **Update `BW_LAT`/`BW_LON` if the Windows machine is somewhere else** — BirdNET
> uses location to decide which species are plausible.

### Cutting down false positives

A single mic in a noisy city throws a lot of bad guesses. Woodpecker "drumming"
is the worst offender — it's acoustically almost identical to a jackhammer or
hammering on a construction site, so BirdNET routinely reports a woodpecker when
it's really roadwork.

Two knobs handle this:

- **`BW_MIN_CONF`** raises the global bar (default `0.5`). Higher = fewer false
  positives but you may miss faint, genuine birds.
- **`BW_SPECIES_MIN_CONF`** sets a *stricter* bar for noise-prone species. Keys
  match case-insensitively as a substring of the common name, so `"woodpecker"`
  covers Downy/Hairy/Red-bellied/etc. in one entry. Your settings merge over the
  built-in defaults (`woodpecker`, `flicker`, `sapsucker` all at `0.7`); set a
  species to `0` to opt it back out.

  ```
  BW_SPECIES_MIN_CONF={"woodpecker": 0.8, "mourning dove": 0.6}
  ```

  A "Downy Woodpecker" at 0.55 confidence is now dropped (almost certainly a
  jackhammer); a clear one at 0.72 still comes through.

## Project layout

```
config.py              # central config (location, mic, thresholds, DB, timezone)
analysis.py            # reusable BirdNET wrapper
db.py                  # PostgreSQL storage + queries (SQLAlchemy)
storage.py             # clip saving + storage-cap pruning
feed.py                # publishes the iPhone-widget JSON feed to S3 (optional)
clips_s3.py            # uploads detection clips to S3 for cloud playback (optional)
streamlit_app.py       # Streamlit dashboard (also the Connect Cloud primary file)
capture/
  list_devices.py      # list microphones
  listen.py            # record -> identify -> save to DB + clips
widget/
  RambleRegister.js    # Scriptable iPhone widget (reads the S3 feed)
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
- **Phase 6 (done)** — cloud audio storage on S3 so clip playback works from the
  hosted dashboard, not just the recording machine ✅. Bird clips are uploaded
  straight from memory and live in **S3 only** (never written to local disk) and
  are never pruned from the bucket; only human-voice holds and failed-upload
  fallbacks are kept locally ✅.
