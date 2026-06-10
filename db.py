"""SQLite storage for bird detections.

One table, `detections`, with a row per (de-duplicated) detection. WAL mode is
enabled so the dashboard can read while the listener writes.
"""
import sqlite3

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT    NOT NULL,   -- ISO 8601 local time
    common_name     TEXT    NOT NULL,
    scientific_name TEXT    NOT NULL,
    confidence      REAL    NOT NULL,
    lat             REAL,
    lon             REAL,
    clip_path       TEXT                -- filename within CLIPS_DIR, or NULL
);
CREATE INDEX IF NOT EXISTS idx_detected_at ON detections(detected_at);
CREATE INDEX IF NOT EXISTS idx_common_name ON detections(common_name);
"""


def get_conn():
    """Open a connection. Cheap for SQLite; callers may open one per use."""
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_detection(conn, detected_at, common_name, scientific_name,
                     confidence, lat, lon, clip_path):
    cur = conn.execute(
        """INSERT INTO detections
           (detected_at, common_name, scientific_name, confidence, lat, lon, clip_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (detected_at.isoformat(timespec="seconds"), common_name, scientific_name,
         float(confidence), lat, lon, clip_path),
    )
    conn.commit()
    return cur.lastrowid


def clear_clip_paths(conn, filenames):
    """Null out clip_path for clips that have been pruned from disk."""
    if not filenames:
        return
    conn.executemany(
        "UPDATE detections SET clip_path = NULL WHERE clip_path = ?",
        [(f,) for f in filenames],
    )
    conn.commit()


# --- Queries used by the dashboard --------------------------------------

def recent_detections(conn, limit=50):
    return conn.execute(
        "SELECT * FROM detections ORDER BY detected_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def counts_for_day(conn, day):
    """Per-species counts for a given YYYY-MM-DD date string."""
    return conn.execute(
        """SELECT common_name, scientific_name,
                  COUNT(*) AS n, MAX(confidence) AS best
           FROM detections
           WHERE substr(detected_at, 1, 10) = ?
           GROUP BY common_name
           ORDER BY n DESC, best DESC""",
        (day,),
    ).fetchall()


def hourly_counts_for_day(conn, day):
    """Detection counts per hour (0-23) for a given date string."""
    rows = conn.execute(
        """SELECT CAST(substr(detected_at, 12, 2) AS INTEGER) AS hour, COUNT(*) AS n
           FROM detections
           WHERE substr(detected_at, 1, 10) = ?
           GROUP BY hour""",
        (day,),
    ).fetchall()
    counts = [0] * 24
    for r in rows:
        counts[r["hour"]] = r["n"]
    return counts


def life_list(conn):
    """Every distinct species ever heard, with first-seen and totals."""
    return conn.execute(
        """SELECT common_name, scientific_name,
                  COUNT(*) AS total,
                  MIN(detected_at) AS first_seen,
                  MAX(detected_at) AS last_seen,
                  MAX(confidence)  AS best
           FROM detections
           GROUP BY common_name
           ORDER BY first_seen ASC""",
    ).fetchall()


def distinct_days(conn):
    return [r["d"] for r in conn.execute(
        "SELECT DISTINCT substr(detected_at,1,10) AS d FROM detections ORDER BY d DESC"
    ).fetchall()]
