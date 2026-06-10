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
        clip_path       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bw_detected_at ON {_TABLE} (detected_at);
    CREATE INDEX IF NOT EXISTS idx_bw_common_name ON {_TABLE} (common_name);
    """
    with get_engine().begin() as conn:
        for stmt in filter(str.strip, ddl.split(";")):
            conn.execute(text(stmt))


def insert_detection(detected_at, common_name, scientific_name,
                     confidence, lat, lon, clip_path):
    """Insert one detection. Opens and commits its own transaction."""
    if isinstance(detected_at, datetime.datetime):
        detected_at = detected_at.replace(microsecond=0, tzinfo=None)
    with get_engine().begin() as conn:
        row = conn.execute(
            text(f"""INSERT INTO {_TABLE}
                (detected_at, common_name, scientific_name, confidence, lat, lon, clip_path)
                VALUES (:detected_at, :common_name, :scientific_name, :confidence, :lat, :lon, :clip_path)
                RETURNING id"""),
            {
                "detected_at": detected_at, "common_name": common_name,
                "scientific_name": scientific_name, "confidence": float(confidence),
                "lat": lat, "lon": lon, "clip_path": clip_path,
            },
        ).first()
    return row[0] if row else None


def clear_clip_paths(filenames):
    """Null out clip_path for clips that have been pruned from disk."""
    if not filenames:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {_TABLE} SET clip_path = NULL WHERE clip_path = :f"),
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
