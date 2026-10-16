# uws_birdwatcher

![Listener status](https://healthchecks.io/badge/ecc7da1d-2e3a-4faa-ad73-ab5ccc/4fWMRb2c-2.svg)

Point a microphone out the window, identify the birds you hear with
[BirdNET](https://github.com/kahst/BirdNET-Analyzer) (Cornell Lab), and show them
on a live dashboard — **The Ramble Register**.

The full pipeline is running: a microphone next to the window feeds BirdNET,
which writes each identified bird to a shared PostgreSQL database; a Streamlit
dashboard hosted on Posit Connect Cloud reads that database and shows the live
feed, daily stats, and a life list. Detections are filtered to species likely at
your location and time of year, and a second layer of shape tests flags likely
false positives (machine noise, double labels, one-off misfires) without ever
deleting them.

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

`run_listener.vbs` (in the repo root) starts the listener hidden, supervised by
a watchdog (see *Self-recovery and alerts* below). The listener's output goes to
`listener.log` and the watchdog's restart history to `watchdog.log`.
Double-click it to start now, or autostart it on logon:

1. Open **Task Scheduler** → **Create Task…** (not "Basic Task").
2. **General:** name it "Birdwatcher listener"; select **Run only when user is
   logged on** (this is what gives it microphone access).
3. **Triggers:** New → **At log on** → (optionally your user). Tick **Enabled**.
4. **Actions:** New → Program/script: `wscript.exe`; Add arguments:
   `"C:\path\to\uws_birdwatcher\run_listener.vbs"`.
5. **Settings:** untick **Stop the task if it runs longer than…**. (Task
   Scheduler's "restart if the task fails" does nothing here: the .vbs returns
   at once, so the task always looks successful. The watchdog does the
   restarting.) On **Conditions**, untick **Start the task only if the computer
   is on AC power**, or a laptop on battery at logon won't start it.
6. Optional extra safety: add a second trigger, **Daily**, repeating every
   **10 minutes** indefinitely. If the watchdog itself ever dies, this brings
   it back; while it's running, extra launches exit at once.
7. Save. It launches at every logon; check `listener.log` to confirm detections.

To check it's running: `watchdog.log` should end with "listener started", and
Task Manager → Details shows `python.exe` processes for `capture.watchdog` and
`capture.listen`. To stop it:

```bash
.venv\Scripts\python -m capture.watchdog --stop
```

(Ending only the listener's `python.exe` just makes the watchdog restart it.)
Also set **Power & sleep** so the desktop doesn't sleep, or it stops recording.

> Want it running *before* you log in (e.g. headless reboots)? Either enable
> Windows auto-login for that account, or use a tool like
> [NSSM](https://nssm.cc/) to run it as a service — but verify the service can
> actually access the mic, since Session 0 often can't. This matters: on Oct
> 14, 2026 a Windows Update restart at 01:29 left the machine at the sign-in
> screen, and nothing recorded until someone signed in at 12:36.

### Self-recovery and alerts

The listener used to stop for good on the first unexpected error. On Oct 13,
2026 the network dropped at 23:51, a detection arrived while the database was
unreachable, and the unguarded insert killed it. Now recovery has three layers,
and you hear about an outage only when all of them fail:

1. **Inside the listener.** Recording is local-first (see *Local-first
   recording* below), so network and database trouble can't stop it. A mic
   that stays silent or errors for four segments (about a minute) is reopened: PortAudio is restarted and the device found again by
   name, which is what a USB dropout or driver reset needs. A recording that
   never finishes is abandoned after 35 s rather than blocking forever. If the
   mic is still dead after 10 minutes of reopening, the listener exits with
   code 3 so a fresh process can try.
2. **The watchdog** (`capture/watchdog.py`, started by `run_listener.vbs`)
   restarts the listener whenever it exits, waiting 5 s and doubling up to 5
   minutes if it keeps failing. It also kills and restarts a listener whose
   heartbeat file (`listener.heartbeat`, touched every segment) has gone stale
   for 3 minutes. A lock stops a second listener or watchdog from starting.
3. **Alerts (dead man's switch).** Set `BW_HEALTHCHECK_URL` to a
   [healthchecks.io](https://healthchecks.io) check (free; or a self-hosted
   instance). The listener pings it every 5 minutes, but only while the mic is
   hearing sound. If the pings stop for longer than the check's grace period
   (the machine is off, asleep or at the sign-in screen, the network is down,
   or the mic stays dead), healthchecks.io notifies you by email, push or SMS,
   and again when pings resume. The watchdog also reports a failure straight
   away, with the end of the log, after 3 restarts within an hour. Outages that
   fix themselves within the grace period never notify you.

To set up alerts: create a check at healthchecks.io with **period 5 minutes**
and **grace 30 minutes**, add the notification channels you want, then put
its ping URL in `.env` and send a test ping:

```bash
# .env:  BW_HEALTHCHECK_URL=https://hc-ping.com/your-check-uuid
.venv\Scripts\python -m healthcheck
```

Restart the listener to pick it up: `python -m capture.watchdog --stop`,
then start the scheduled task (Task Scheduler → *birdwatcher* → **Run**, or
`Start-ScheduledTask -TaskName birdwatcher` in PowerShell). Start it that way
rather than from a terminal or an app, so it doesn't stop when that app
closes or updates. The status badge at the top of this README
is that check's healthchecks.io badge: green while pings arrive, red once the
check is down.

## Dashboard

The dashboard — **The Ramble Register**, styled in a light "Central Park
Morning" theme — is a [Streamlit](https://streamlit.io/) app. In a **second
terminal** (leave the listener running in the first), start it:

```bash
# Windows:  .venv\Scripts\streamlit run streamlit_app.py
# macOS:    .venv/bin/streamlit run streamlit_app.py
```

Streamlit opens **http://localhost:8501** automatically. Six tabs:

- **Live feed** — most recent detections, auto-refreshing, with playable clips
- **Daily stats** — per-species counts and an activity-by-hour chart (pick a day)
- **Life list** — every species ever heard, first/last heard, totals
- **Birds** — a profile for any bird (type to search; the page address updates,
  e.g. `?bird=Blue+Jay`, so a profile can be bookmarked or shared, and the phone
  widget opens on the latest bird's). It shows a Wikipedia summary, a status
  (resident, arrived, departed, passing through, rare visitor), what sets it
  apart from the other birds, its season, its day against the average bird,
  when it starts at dawn, how it calls (bouts and gaps), spectrograms of its
  best clips with a pitch signature against all birds, the birds it is heard
  with more often than chance, and how reliably the model recognises it.
  Clicking a bird in the Life list or Daily stats tables, or **Profile** on a
  live-feed card, opens the same profile in a pop-up.
- **Insights** — seasonal and daily patterns: which species are here in which
  weeks (fall migrants arriving), species per week, how the first call of the
  day tracks sunrise, which species sing first at dawn, and each common
  species' daily rhythm. Every chart has hover details and a data table, and
  follows the Filtered/Raw switch. Sunrise is computed locally (NOAA's
  algorithm), so no extra service or package is needed.
- **Filter report** — what the false-positive filters keep and remove, per
  species, with every removed detection listed for checking

A **Filtered / Raw** switch in the sidebar applies to every view, the live feed
and the phone widget included (Filtered is the default). In Filtered mode likely
false positives are hidden everywhere; they are listed only in the Filter report
tab and in Raw mode. See *False-positive filters* below.

Below the summary cards a **listener health** pill shows whether the recording
machine is alive: 🟢 listening (with the live mic level), 🟡 running but the mic
is silent (likely unplugged/muted), or 🔴 offline (no heartbeat — crashed or
asleep). The listener writes a heartbeat every segment to a small
`listener_status` table, so a quiet-but-healthy setup is easy to tell apart from
a dead one.

The listener and dashboard share the same database, so the dashboard updates as
new birds are detected. **Clip playback in the cloud** requires the clips to be
reachable from the dashboard host: when the S3 feed bucket is configured (see
below), the listener uploads each bird clip there, deletes its local copy (see
*Local-first recording*), and the dashboard plays it from a public URL. Without a bucket (or with `BW_PUBLISH_CLIPS=0`), clips fall back to
local files on the recording machine, where playback still works.

### Local-first recording

The capture loop never touches the network. For every detection it saves the
clip to `clips/` and appends the detection, with the time it was heard, to a
local queue (`outbox.jsonl`). A background sync thread (`uploader.py`) does
everything that needs a connection: it uploads each bird clip to S3, writes the
detection to the database, deletes the local clip, and keeps the dashboard
heartbeat and phone-widget feed up to date. When the network or database is
down, only the sync thread waits; recording carries on and the backlog drains,
oldest first, when the connection returns. During an outage the dashboard shows
the listener as offline; afterwards the missed detections appear with their
real times.

This came out of Oct 15, 2026, when the listener sat behind a stuck database
lock from 17:25 to 19:49 and recorded nothing. The database connections now
also carry their own limits: a lock wait gives up after 10 s, a statement after
60 s, and an abandoned transaction is ended after 5 minutes (it used to hold
its locks for the server's 2-hour dead-client timeout). Startup no longer runs
schema changes when nothing is missing, so it takes no heavy locks.

If a clip's upload keeps failing while the database works, its detection waits
up to 30 minutes (`BW_CLIP_UPLOAD_GIVE_UP`), then is written with the clip
kept locally for `python -m backfill_clips`. If the sync thread can't reach the
database for 15 minutes (`BW_SYNC_STALE`), health pings stop, so a sync that
stays broken alerts you even though recording continues.

**Local disk use is capped,** so a long outage can't fill the disk:

| What | Cap | When it's full |
|------|-----|----------------|
| `clips/` | `BW_MAX_CLIPS_MB` (2,000 MB) | Oldest local clips are deleted, including any still waiting to upload; their detections are still saved, without audio |
| `outbox.jsonl` | `BW_OUTBOX_MAX_MB` (50 MB, over 100,000 detections) | Oldest queued detections are dropped |
| `listener.log` | 50 MB, plus one 50 MB `listener.log.1` | Rolled over when the listener (re)starts |

Uploaded bird clips are deleted locally as soon as they're in S3, so normally
`clips/` holds only human-voice holds and failed-upload fallbacks (2,000 MB is
roughly 4,000 clips).

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

### False-positive filters

BirdNET at a window logs plenty of detections that are not birds: long strings
of mediocre guesses from machine noise, a second species name on one sound,
one-off misfires. Four tests judge the **shape** of the data, not whether a
species is likely in New York, and **flag** detections rather than delete them.
The raw `detections` table is never modified.

| # | Test | Rule (defaults) |
|---|------|-----------------|
| 1 | Anchored bouts | Group a species' detections into bouts (gaps ≤ 120 s); keep a bout only if one detection reaches 0.6. |
| 2 | Shadow labels | Drop a detection if another species scored higher within 1.5 s. Species with strong (0.7+) solo calls on 3+ days are exempt: their overlaps are real mixed flocks. |
| 3 | Repeatability | Keep a species only if it scores 0.7+ on 2+ days, or 3+ times in one bout (a flyover). Judged on rows passing tests 1–2. |
| 4 | Round-the-clock days | Drop a species' day if it spans 8+ hours, including 2+ between 22:00 and 04:00, and never reaches 0.8. |

**Cadence** (median gap and its coefficient of variation within runs) is shown
as a diagnostic only. **Manual species overrides** (confirmed / rejected) win
over every test, for the cases shape tests can't catch, like a species the model
consistently misidentifies.

How it fits together:

- `filters.py` holds the rules as pure pandas code (no database), so they are
  unit-tested (`python -m pytest tests`) and fast (well under a second).
- The dashboard runs them in-process, so the Filtered/Raw switch, the per-test
  switches and the admin threshold sliders respond instantly. Its caches key on
  the newest detection id, so a new detection is judged within seconds. In
  Filtered mode the live feed hides what the filters drop; Raw shows every card
  and marks the ones the filters would hide.
- The listener recomputes and stores every flag on a background thread every
  `BW_FILTER_REFRESH` seconds (default 120), in `detection_flags` and
  `species_quality`. The phone widget reads these: its latest bird, totals and
  new species are all filtered, and a new detection appears there once it has
  been judged.
- Thresholds live in the `filter_config` table (seeded with the defaults) and
  overrides in `species_override`.
- Setting `BW_ADMIN_PASSWORD` unlocks admin controls on the Filter report tab:
  threshold sliders (instant, for the admin only), **Save as defaults**, and
  species overrides. Saving writes to the database, so on the hosted dashboard
  it needs a DB user with write access, as voting does.

Refresh or check the filters from the command line:

```bash
python -m filter_refresh              # recompute and store the flags now
python -m filter_refresh --dry-run    # compute and print, write nothing
python -m filter_refresh --validate   # replay the Oct 12, 2026 trial run
```

`--validate` reruns the rules on the detections the original trial saw and
checks them against its figures (8,988 of 17,619 detections and 46 of 128
species kept, plus per-species counts after each test). The trial predated the
mixed-flock exemption, so validation turns it off. With the exemption on, the
same window keeps 9,116 detections and the same 46 species.

Known limits: the listener stamps every detection in a 15-second segment with
the same time, so "within 1.5 s" effectively means "in the same segment", and
cadence can't see gaps under about 15 s. Test 4 can also catch genuine night
migration (a few Swamp Sparrow and Blue Grosbeak nights); use an override where
that matters.

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
   the shared database. Optionally set `BW_ADMIN_PASSWORD` to enable the
   filter admin controls.

**The data bridge.** The microphone listener runs on your home machine and
writes to the shared PostgreSQL database; the cloud-hosted dashboard reads from
that same database, so detections recorded at home appear online. A freshly
deployed dashboard renders but stays empty until the listener records something.
Bird-clip playback works in the cloud once an S3 bucket is configured (clips are
uploaded there and their local copies deleted); otherwise playback is
local-only. See the iPhone-widget section for the bucket setup.

## Configuration

All settings live in `config.py` and can be overridden with environment
variables:

| Variable            | Default            | Meaning                                        |
|---------------------|--------------------|------------------------------------------------|
| `BW_INPUT_DEVICE`   | `UAC`              | Mic name substring (e.g. `MacBook`) or index   |
| `BW_LAT`            | `40.7760`          | Latitude (default: Upper West Side, NYC; approximate on purpose, as the dashboard shows it publicly) |
| `BW_LON`            | `-73.9770`         | Longitude                                      |
| `BW_MAP_RADIUS`     | `450`              | Radius in metres of the dashboard map's shaded "approximately here" area |
| `BW_MIN_CONF`       | `0.25`             | Minimum confidence (0–1) to report a detection |
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
| `BW_MAX_CLIPS_MB`   | `2000`             | Disk cap for local clips (upload backlog, voice holds, fallbacks); oldest pruned over it (S3 never pruned) |
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
| `BW_FILTER_REFRESH` | `120`              | Seconds between the listener's recomputes of the stored false-positive flags (`0` = off) |
| `BW_ADMIN_PASSWORD` | *(empty)*          | Passphrase for the dashboard's filter admin controls; empty = no admin controls |
| `BW_HEALTHCHECK_URL` | *(empty)*         | healthchecks.io ping URL for outage alerts; empty = off |
| `BW_HEALTHCHECK_INTERVAL` | `300`        | Seconds between "alive" pings (sent only while the mic hears sound) |
| `BW_MIC_REOPEN_AFTER` | `4`              | Consecutive silent/failed segments before the mic is reopened |
| `BW_MIC_GIVE_UP`    | `600`              | Seconds of a dead mic, despite reopening, before the listener restarts itself |
| `BW_HANG_SECONDS`   | `180`              | Seconds without a heartbeat before the watchdog kills and restarts the listener |
| `BW_ALERT_AFTER_RESTARTS` | `3`          | Restarts within `BW_ALERT_WINDOW` that trigger the watchdog's failure alert |
| `BW_ALERT_WINDOW`   | `3600`             | Window, in seconds, for counting restarts |
| `BW_DB_RETRY`       | `60`               | Seconds the sync thread skips the database after an error |
| `BW_OUTBOX_PATH`    | `outbox.jsonl`     | Local queue of detections waiting to sync |
| `BW_OUTBOX_MAX_MB`  | `50`               | Disk cap for the queue; oldest rows dropped over it |
| `BW_CLIP_UPLOAD_GIVE_UP` | `1800`        | Seconds a detection waits for its failing clip upload before it's written with the clip kept locally |
| `BW_SYNC_STALE`     | `900`              | Seconds without reaching the database before health pings stop |

Example (PowerShell): `$env:BW_INPUT_DEVICE="UAC"; .venv\Scripts\python -m capture.listen`

> **Update `BW_LAT`/`BW_LON` if the Windows machine is somewhere else** — BirdNET
> uses location to decide which species are plausible.

## Project layout

```
config.py              # central config (location, mic, thresholds, DB, timezone)
analysis.py            # reusable BirdNET wrapper
db.py                  # PostgreSQL storage + queries (SQLAlchemy)
filters.py             # false-positive filter rules (pure pandas, no DB)
filter_refresh.py      # recompute + store filter flags; --validate replays the trial
insights.py            # Insights-tab analytics + sunrise/sunset (pure pandas, no DB)
bird_profile.py        # per-bird profile analytics (pure pandas, no DB)
acoustics.py           # clip spectrograms + pitch signatures (numpy only)
storage.py             # clip saving + storage-cap pruning
feed.py                # publishes the iPhone-widget JSON feed to S3 (optional)
outbox.py              # local queue every detection goes through before the database
uploader.py            # sync thread: clips to S3, rows to the DB, local cleanup
healthcheck.py         # dead man's switch pings + failure alerts (optional)
clips_s3.py            # uploads detection clips to S3 for cloud playback (optional)
streamlit_app.py       # Streamlit dashboard (also the Connect Cloud primary file)
capture/
  list_devices.py      # list microphones
  listen.py            # record -> identify -> save to DB + clips
  watchdog.py          # restarts the listener on crash/hang; alerts if that fails
  recovery.py          # mic-reopen and restart/alert decisions (unit-tested)
widget/
  RambleRegister.js    # Scriptable iPhone widget (reads the S3 feed)
requirements.txt       # slim dashboard deps (Connect Cloud installs these)
requirements-listen.txt# adds the BirdNET + microphone stack (home machine only)
requirements-dev.txt   # test tools (pytest)
tests/
  test_filters.py      # unit tests for each filter rule
  test_insights.py     # unit tests for the analytics and sun times
  test_bird_profile.py # unit tests for bird profiles and clip acoustics
  test_recovery.py     # unit tests for the outbox, mic guard and restart policy
  test_uploader.py     # unit tests for the sync step (upload, insert, cleanup)
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
  hosted dashboard, not just the recording machine ✅. Bird clips end up in **S3
  only** (deleted locally once uploaded) and are never pruned from the bucket;
  only human-voice holds and failed-upload fallbacks are kept locally ✅.
- **Phase 7 (done)** — false-positive filters: four shape tests flag likely
  false positives without deleting them; Filtered/Raw and per-test switches on
  every view; a Filter report tab with per-species summary and removed
  detections; admin threshold sliders and species overrides; stored flags for
  the phone widget; validated against the Oct 2026 trial run ✅.
- **Phase 8 (done)** — Insights tab: seasonal presence grid, species per week,
  dawn chorus vs sunrise, dawn singing order, and daily rhythm ✅.
- **Phase 9 (done)** — bird profiles: searchable, shareable per-bird pages and
  pop-ups with seasonal, daily, dawn and calling-pattern stats against the
  average bird, plus spectrograms and pitch signatures from the stored clips ✅.
