"""PostgreSQL storage for bird detections.

One table, `detections`, lives in its own schema (``config.DB_SCHEMA``, default
``birdwatcher``) so it can share a database with other apps without
colliding. The home listener writes; the cloud dashboard reads.

Timestamps are stored as timezone-naive *local* time (see ``config.now_local``)
so "today" and per-hour buckets are correct regardless of where the dashboard
runs.
"""
import datetime

from sqlalchemy import create_engine, text

import config

# Lazily-built singleton engine. pool_pre_ping recycles connections dropped by
# the server/firewall, which matters for a long-running listener.
_engine = None


def _ident(name):
    """Validate a SQL identifier (schema name) before interpolating it.

    Identifiers can't be passed as bind parameters, so we whitelist a safe
    character set rather than trusting the value blindly."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


_SCHEMA = _ident(config.DB_SCHEMA)
_TABLE = f'"{_SCHEMA}".detections'
_STATUS = f'"{_SCHEMA}".listener_status'


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.database_url(),
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_conn():
    """Open a SQLAlchemy connection. The dashboard opens one per request and
    closes it; the connection pool makes that cheap."""
    return get_engine().connect()


def init_db():
    """Create the schema, table, and indexes if they don't already exist."""
    ddl = f"""
    CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}";
    CREATE TABLE IF NOT EXISTS {_TABLE} (
        id              BIGSERIAL PRIMARY KEY,
        detected_at     TIMESTAMP   NOT NULL,
        common_name     TEXT        NOT NULL,
        scientific_name TEXT        NOT NULL,
        confidence      DOUBLE PRECISION NOT NULL,
        lat             DOUBLE PRECISION,
        lon             DOUBLE PRECISION,
        clip_path       TEXT,
        clip_url        TEXT
    );
    ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS clip_url TEXT;
    CREATE INDEX IF NOT EXISTS idx_bw_detected_at ON {_TABLE} (detected_at);
    CREATE INDEX IF NOT EXISTS idx_bw_common_name ON {_TABLE} (common_name);
    CREATE TABLE IF NOT EXISTS {_STATUS} (
        host              TEXT PRIMARY KEY,
        updated_at        TIMESTAMP NOT NULL,
        last_peak         DOUBLE PRECISION,
        last_detection_at TIMESTAMP
    );
    """
    with get_engine().begin() as conn:
        for stmt in filter(str.strip, ddl.split(";")):
            conn.execute(text(stmt))


def insert_detection(detected_at, common_name, scientific_name,
                     confidence, lat, lon, clip_path, clip_url=None):
    """Insert one detection. Opens and commits its own transaction.

    ``clip_path`` is the local filename on the recording machine; ``clip_url``
    is the public S3 URL when the clip was uploaded for the cloud dashboard
    (None when uploads are off, failed, or the clip was withheld for privacy)."""
    if isinstance(detected_at, datetime.datetime):
        detected_at = detected_at.replace(microsecond=0, tzinfo=None)
    with get_engine().begin() as conn:
        row = conn.execute(
            text(f"""INSERT INTO {_TABLE}
                (detected_at, common_name, scientific_name, confidence, lat, lon, clip_path, clip_url)
                VALUES (:detected_at, :common_name, :scientific_name, :confidence, :lat, :lon, :clip_path, :clip_url)
                RETURNING id"""),
            {
                "detected_at": detected_at, "common_name": common_name,
                "scientific_name": scientific_name, "confidence": float(confidence),
                "lat": lat, "lon": lon, "clip_path": clip_path, "clip_url": clip_url,
            },
        ).first()
    return row[0] if row else None


def record_heartbeat(host, last_peak, last_detection_at=None):
    """Upsert a 'still alive' pulse for one listener host.

    Called every capture loop (~every segment) so the dashboard can tell a
    quiet-but-running listener from a crashed one. ``last_peak`` is the segment's
    audio level (near zero => the mic is muted/unplugged)."""
    if isinstance(last_detection_at, datetime.datetime):
        last_detection_at = last_detection_at.replace(microsecond=0, tzinfo=None)
    with get_engine().begin() as conn:
        conn.execute(
            text(f"""INSERT INTO {_STATUS}
                    (host, updated_at, last_peak, last_detection_at)
                    VALUES (:host, :updated_at, :last_peak, :last_detection_at)
                    ON CONFLICT (host) DO UPDATE SET
                        updated_at        = EXCLUDED.updated_at,
                        last_peak         = EXCLUDED.last_peak,
                        last_detection_at = COALESCE(EXCLUDED.last_detection_at,
                                                     {_STATUS}.last_detection_at)"""),
            {
                "host": host,
                "updated_at": config.now_local().replace(microsecond=0),
                "last_peak": float(last_peak),
                "last_detection_at": last_detection_at,
            },
        )


def detections_needing_clip_url(conn):
    """Rows that have a local clip on disk but no S3 URL yet — the backfill
    candidates. Returns id, timestamp, names, and clip_path so the backfill can
    locate the file and (optionally) voice-check it before uploading."""
    return _rows(conn.execute(
        text(f"""SELECT id, detected_at, common_name, scientific_name, clip_path
                 FROM {_TABLE}
                 WHERE clip_path IS NOT NULL AND clip_url IS NULL
                 ORDER BY detected_at""")
    ))


def set_clip_url(detection_id, url):
    """Record the public S3 URL for one detection after a backfill upload."""
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {_TABLE} SET clip_url = :u WHERE id = :id"),
            {"u": url, "id": detection_id},
        )


def clear_clip_paths(filenames):
    """Null out clip_path for local clips pruned from disk, so the dashboard's
    local fallback never points at a missing file. clip_url is left untouched —
    the S3 copy is permanent (bird clips are never deleted from the bucket)."""
    if not filenames:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {_TABLE} SET clip_path = NULL "
                 f"WHERE clip_path = :f"),
            [{"f": f} for f in filenames],
        )


# --- Queries used by the dashboard --------------------------------------
# Each takes an open connection and returns plain list[dict] rows.

def _rows(result):
    return [dict(r) for r in result.mappings().all()]


def recent_detections(conn, limit=50):
    return _rows(conn.execute(
        text(f"SELECT * FROM {_TABLE} ORDER BY detected_at DESC, id DESC LIMIT :limit"),
        {"limit": limit},
    ))


def counts_for_day(conn, day):
    """Per-species counts for a given YYYY-MM-DD date string."""
    return _rows(conn.execute(
        text(f"""SELECT common_name, scientific_name,
                        COUNT(*) AS n, MAX(confidence) AS best
                 FROM {_TABLE}
                 WHERE detected_at::date = CAST(:day AS date)
                 GROUP BY common_name, scientific_name
                 ORDER BY n DESC, best DESC"""),
        {"day": day},
    ))


def hourly_counts_for_day(conn, day):
    """Detection counts per hour (0-23) for a given date string."""
    rows = conn.execute(
        text(f"""SELECT EXTRACT(HOUR FROM detected_at)::int AS hour, COUNT(*) AS n
                 FROM {_TABLE}
                 WHERE detected_at::date = CAST(:day AS date)
                 GROUP BY hour"""),
        {"day": day},
    ).mappings().all()
    counts = [0] * 24
    for r in rows:
        counts[r["hour"]] = r["n"]
    return counts


def life_list(conn):
    """Every distinct species ever heard, with first-seen and totals."""
    return _rows(conn.execute(
        text(f"""SELECT common_name, scientific_name,
                        COUNT(*) AS total,
                        MIN(detected_at) AS first_seen,
                        MAX(detected_at) AS last_seen,
                        MAX(confidence)  AS best
                 FROM {_TABLE}
                 GROUP BY common_name, scientific_name
                 ORDER BY first_seen ASC""")
    ))


def distinct_days(conn):
    rows = conn.execute(
        text(f"SELECT DISTINCT detected_at::date AS d FROM {_TABLE} ORDER BY d DESC")
    ).mappings().all()
    return [r["d"].isoformat() for r in rows]


def overview(conn):
    """All-time totals for the dashboard's summary cards."""
    row = conn.execute(text(f"""
        SELECT COUNT(*)                    AS total,
               COUNT(DISTINCT common_name) AS species,
               MIN(detected_at)            AS first_at,
               MAX(detected_at)            AS last_at
        FROM {_TABLE}
    """)).mappings().first()
    return dict(row) if row else {
        "total": 0, "species": 0, "first_at": None, "last_at": None,
    }


def listener_status(conn):
    """The freshest listener heartbeat, or None if none recorded yet.

    Returns a dict with host, updated_at, last_peak, last_detection_at; the
    dashboard turns updated_at's age and last_peak into a health indicator."""
    row = conn.execute(text(f"""
        SELECT host, updated_at, last_peak, last_detection_at
        FROM {_STATUS}
        ORDER BY updated_at DESC
        LIMIT 1
    """)).mappings().first()
    return dict(row) if row else None


def detections_per_day(conn, days=14):
    """Daily detection counts for the last `days` days (for a trend chart)."""
    start = (config.today_local() - datetime.timedelta(days=days - 1)).isoformat()
    rows = conn.execute(text(f"""
        SELECT detected_at::date AS day, COUNT(*) AS n
        FROM {_TABLE}
        WHERE detected_at::date >= CAST(:start AS date)
        GROUP BY day
        ORDER BY day
    """), {"start": start}).mappings().all()
    return [dict(r) for r in rows]
