"""PostgreSQL storage for bird detections.

The core table, `detections`, lives in its own schema (``config.DB_SCHEMA``,
default ``birdwatcher``) so it can share a database with other apps without
colliding. The home listener writes; the cloud dashboard reads. Alongside it,
the false-positive filter tables hold thresholds, per-detection flags, a
per-species summary and manual species overrides (see filters.py); raw
detections are never modified by filtering.

Timestamps are stored as timezone-naive *local* time (see ``config.now_local``)
so "today" and per-hour buckets are correct regardless of where the dashboard
runs.
"""
import datetime

from sqlalchemy import bindparam, create_engine, text

import config
import filters

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
_VOTES = f'"{_SCHEMA}".clip_votes'
_FCONFIG = f'"{_SCHEMA}".filter_config'
_FLAGS = f'"{_SCHEMA}".detection_flags'
_SPQ = f'"{_SCHEMA}".species_quality'
_OVERRIDE = f'"{_SCHEMA}".species_override'


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.database_url(),
            pool_pre_ping=True,
            future=True,
            # Fail fast when the network is down rather than stalling the
            # listener for the OS's ~20 s connect timeout, and notice a
            # silently dropped connection instead of waiting on it forever.
            connect_args={
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                # Server-side limits for our own sessions (the shared server
                # sets none). On Oct 15 a query blocked on a lock waited for
                # hours, and a killed listener's session kept its locks for
                # the server's 2 h 11 min dead-client timeout, blocking every
                # restart. Now a lock wait gives up after 10 s, a statement
                # after 60 s, and an abandoned open transaction is ended (and
                # its locks freed) after 5 minutes.
                "options": ("-c lock_timeout=10s -c statement_timeout=60s "
                            "-c idle_in_transaction_session_timeout=5min"),
            },
        )
    return _engine


def get_conn():
    """Open a SQLAlchemy connection. The dashboard opens one per request and
    closes it; the connection pool makes that cheap."""
    return get_engine().connect()


def _schema_ready(conn):
    """True when every table, column and index init_db creates already exists.

    Routine starts (every listener restart, every dashboard wake-up) then skip
    the DDL entirely. That matters because ``ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS`` takes an ACCESS EXCLUSIVE lock on detections even when there is
    nothing to add, so it queues behind any open transaction on the table and
    blocks every reader behind it (Oct 15)."""
    names = [_TABLE, _STATUS, _VOTES, _FCONFIG, _FLAGS, _SPQ, _OVERRIDE,
             f'"{_SCHEMA}".idx_bw_detected_at', f'"{_SCHEMA}".idx_bw_common_name']
    checks = [f"to_regclass(:n{i}) IS NOT NULL" for i in range(len(names))]
    checks.append("""EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_schema = :schema AND table_name = 'detections'
                        AND column_name = 'clip_url')""")
    params = {f"n{i}": n for i, n in enumerate(names)}
    params["schema"] = _SCHEMA
    return bool(conn.execute(text("SELECT " + " AND ".join(checks)), params).scalar())


def init_db():
    """Create the schema, table, and indexes if they don't already exist.
    Cheap and lock-free when they do (see _schema_ready)."""
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
    CREATE TABLE IF NOT EXISTS {_VOTES} (
        detection_id BIGINT    NOT NULL,
        voter        TEXT      NOT NULL,
        vote         SMALLINT  NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        PRIMARY KEY (detection_id, voter)
    );
    CREATE TABLE IF NOT EXISTS {_FCONFIG} (
        name        TEXT PRIMARY KEY,
        value       DOUBLE PRECISION NOT NULL,
        description TEXT,
        updated_at  TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS {_FLAGS} (
        detection_id    BIGINT PRIMARY KEY
                        REFERENCES {_TABLE} (id) ON DELETE CASCADE,
        bout_id         INTEGER,
        pass_anchor     BOOLEAN NOT NULL,
        pass_shadow     BOOLEAN NOT NULL,
        pass_repeat     BOOLEAN NOT NULL,
        pass_daypattern BOOLEAN NOT NULL,
        shadowed_by     TEXT,
        keep            BOOLEAN NOT NULL,
        removed_by      TEXT,
        computed_at     TIMESTAMP NOT NULL
    );
    CREATE TABLE IF NOT EXISTS {_SPQ} (
        common_name        TEXT PRIMARY KEY,
        scientific_name    TEXT,
        raw_count          INTEGER NOT NULL,
        kept_count         INTEGER NOT NULL,
        pct_kept           DOUBLE PRECISION,
        top_removed_by     TEXT,
        removed_anchor     INTEGER,
        removed_shadow     INTEGER,
        removed_repeat     INTEGER,
        removed_daypattern INTEGER,
        removed_override   INTEGER,
        shadow_exempt      BOOLEAN,
        median_gap_s       DOUBLE PRECISION,
        gap_cv             DOUBLE PRECISION,
        cadence_flag       BOOLEAN,
        computed_at        TIMESTAMP NOT NULL
    );
    CREATE TABLE IF NOT EXISTS {_OVERRIDE} (
        common_name TEXT PRIMARY KEY,
        status      TEXT NOT NULL CHECK (status IN ('confirmed', 'rejected')),
        note        TEXT,
        updated_at  TIMESTAMP NOT NULL
    )
    """
    with get_engine().begin() as conn:
        if not _schema_ready(conn):
            for stmt in filter(str.strip, ddl.split(";")):
                conn.execute(text(stmt))
        # Seed every threshold with its default so the table documents them
        # all. Existing values are kept; only descriptions are refreshed.
        conn.execute(
            text(f"""INSERT INTO {_FCONFIG} (name, value, description, updated_at)
                     VALUES (:name, :value, :description, :ts)
                     ON CONFLICT (name) DO UPDATE
                         SET description = EXCLUDED.description"""),
            [{"name": p.name, "value": float(p.default),
              "description": p.description,
              "ts": config.now_local().replace(microsecond=0)}
             for p in filters.PARAMS],
        )


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


def recent_detections(conn, limit=50, filtered=False):
    src, keep = _source(filtered)
    return _rows(conn.execute(
        text(f"""SELECT d.* FROM {src} WHERE {keep}
                 ORDER BY d.detected_at DESC, d.id DESC LIMIT :limit"""),
        {"limit": limit},
    ))


def latest_detection_id(conn):
    """The newest detection id (0 when empty): a cheap data-version stamp."""
    return conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {_TABLE}")).scalar()


def _source(filtered):
    """FROM clause (detections aliased ``d``) and a WHERE condition.

    Filtered keeps the rows the stored flags keep and applies manual species
    overrides immediately. Detections newer than the last flag refresh have no
    verdict yet and are left out until the next refresh judges them, so a
    filtered view never shows an unjudged likely false positive (a confirmed
    species shows straight away)."""
    if not filtered:
        return f"{_TABLE} d", "TRUE"
    return (f"""{_TABLE} d
                LEFT JOIN {_FLAGS} f ON f.detection_id = d.id
                LEFT JOIN {_OVERRIDE} o ON o.common_name = d.common_name""",
            """(o.status = 'confirmed'
                OR (o.status IS DISTINCT FROM 'rejected'
                    AND f.keep IS TRUE))""")


def counts_for_day(conn, day, filtered=False):
    """Per-species counts for a given YYYY-MM-DD date string."""
    src, keep = _source(filtered)
    return _rows(conn.execute(
        text(f"""SELECT d.common_name, d.scientific_name,
                        COUNT(*) AS n, MAX(d.confidence) AS best
                 FROM {src}
                 WHERE d.detected_at::date = CAST(:day AS date) AND {keep}
                 GROUP BY d.common_name, d.scientific_name
                 ORDER BY n DESC, best DESC"""),
        {"day": day},
    ))


def life_list(conn, filtered=False):
    """Every distinct species ever heard, with first-seen and totals."""
    src, keep = _source(filtered)
    return _rows(conn.execute(
        text(f"""SELECT d.common_name, d.scientific_name,
                        COUNT(*) AS total,
                        MIN(d.detected_at) AS first_seen,
                        MAX(d.detected_at) AS last_seen,
                        MAX(d.confidence)  AS best
                 FROM {src}
                 WHERE {keep}
                 GROUP BY d.common_name, d.scientific_name
                 ORDER BY first_seen ASC""")
    ))


def overview(conn, filtered=False):
    """All-time totals for the dashboard's summary cards."""
    src, keep = _source(filtered)
    row = conn.execute(text(f"""
        SELECT COUNT(*)                      AS total,
               COUNT(DISTINCT d.common_name) AS species,
               MIN(d.detected_at)            AS first_at,
               MAX(d.detected_at)            AS last_at
        FROM {src}
        WHERE {keep}
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


# --- Clip voting --------------------------------------------------------
# Listeners up/down-vote whether a clip matches its species label. One row per
# (detection, voter); re-voting overwrites. vote is +1 (up), -1 (down), or 0
# (vote withdrawn — kept so a withdrawal still overwrites the prior row).

def record_vote(detection_id, voter, vote):
    """Upsert one voter's verdict on a detection's clip. Opens its own txn.

    ``vote`` is +1, -1, or 0 (withdrawn). Writing requires INSERT/UPDATE on the
    schema, so on a read-only dashboard DB user this raises — the caller treats
    that as 'voting unavailable'."""
    with get_engine().begin() as conn:
        conn.execute(
            text(f"""INSERT INTO {_VOTES} (detection_id, voter, vote, created_at)
                     VALUES (:id, :voter, :vote, :ts)
                     ON CONFLICT (detection_id, voter) DO UPDATE SET
                         vote = EXCLUDED.vote, created_at = EXCLUDED.created_at"""),
            {"id": int(detection_id), "voter": voter, "vote": int(vote),
             "ts": config.now_local().replace(microsecond=0)},
        )


def vote_tallies(conn, detection_ids):
    """Map detection_id -> {'up', 'down', 'net'} for the given ids (only ids with
    at least one vote appear). Returns {} for an empty id list."""
    ids = [int(i) for i in detection_ids if i is not None]
    if not ids:
        return {}
    stmt = text(f"""
        SELECT detection_id,
               SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END) AS up,
               SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END) AS down
        FROM {_VOTES}
        WHERE detection_id IN :ids
        GROUP BY detection_id
    """).bindparams(bindparam("ids", expanding=True))
    out = {}
    for r in conn.execute(stmt, {"ids": ids}).mappings().all():
        up, down = int(r["up"] or 0), int(r["down"] or 0)
        out[r["detection_id"]] = {"up": up, "down": down, "net": up - down}
    return out


def my_votes(conn, voter, detection_ids):
    """This voter's existing votes: detection_id -> vote (+1/-1/0). Lets the
    dashboard show which button a returning voter already picked."""
    ids = [int(i) for i in detection_ids if i is not None]
    if not ids or not voter:
        return {}
    stmt = text(f"""SELECT detection_id, vote FROM {_VOTES}
                    WHERE voter = :voter AND detection_id IN :ids"""
                ).bindparams(bindparam("ids", expanding=True))
    return {r["detection_id"]: int(r["vote"])
            for r in conn.execute(stmt, {"voter": voter, "ids": ids}).mappings().all()}


# --- False-positive filters ---------------------------------------------
# filters.py holds the rules; these functions load their inputs and store their
# outputs. filter_refresh recomputes every flag in one pass (the compute takes a
# fraction of a second), so there is no incremental bookkeeping to get wrong.

def all_detections(conn):
    """Every detection as a pandas DataFrame, oldest first: the filter input
    and the dashboard's data source."""
    import pandas as pd  # both the dashboard and the listener install pandas
    return pd.read_sql(
        text(f"""SELECT id, detected_at, common_name, scientific_name,
                        confidence, clip_path, clip_url
                 FROM {_TABLE} ORDER BY detected_at, id"""),
        conn,
    )


def filter_config(conn):
    """Stored thresholds: name -> value (floats; filters.resolve_config casts)."""
    return {r["name"]: r["value"] for r in conn.execute(
        text(f"SELECT name, value FROM {_FCONFIG}")).mappings().all()}


def save_filter_config(values):
    """Upsert threshold values (name -> number). Opens its own transaction."""
    ts = config.now_local().replace(microsecond=0)
    rows = []
    for name, value in values.items():
        p = filters.PARAMS_BY_NAME.get(name)
        if p is None:
            raise ValueError(f"Unknown filter parameter: {name!r}")
        rows.append({"name": name, "value": float(value),
                     "description": p.description, "ts": ts})
    if not rows:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text(f"""INSERT INTO {_FCONFIG} (name, value, description, updated_at)
                     VALUES (:name, :value, :description, :ts)
                     ON CONFLICT (name) DO UPDATE SET
                         value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"""),
            rows,
        )


def species_overrides(conn):
    """Manual species verdicts: list of {common_name, status, note, updated_at}."""
    return _rows(conn.execute(text(
        f"SELECT common_name, status, note, updated_at FROM {_OVERRIDE} "
        f"ORDER BY common_name")))


def set_species_override(common_name, status, note=None):
    """Mark a species 'confirmed' or 'rejected', or clear it (status None).
    An override wins over every automatic test."""
    with get_engine().begin() as conn:
        if status is None:
            conn.execute(text(f"DELETE FROM {_OVERRIDE} WHERE common_name = :n"),
                         {"n": common_name})
            return
        if status not in filters.OVERRIDE_STATUSES:
            raise ValueError(f"Unknown override status: {status!r}")
        conn.execute(
            text(f"""INSERT INTO {_OVERRIDE} (common_name, status, note, updated_at)
                     VALUES (:n, :s, :note, :ts)
                     ON CONFLICT (common_name) DO UPDATE SET
                         status = EXCLUDED.status, note = EXCLUDED.note,
                         updated_at = EXCLUDED.updated_at"""),
            {"n": common_name, "s": status, "note": note or None,
             "ts": config.now_local().replace(microsecond=0)},
        )


def _py(v):
    """numpy/pandas scalar -> plain Python value (NaN/NA -> None) for psycopg2."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN
        return None
    if hasattr(v, "item"):
        v = v.item()
        if isinstance(v, float) and v != v:
            return None
    return v


def write_filter_results(result, computed_at=None):
    """Replace the stored flags and species summary with ``result`` (a
    filters.Result) in one transaction, so readers never see a half-written
    set. execute_values batches the insert into a few round trips."""
    from psycopg2.extras import execute_values

    ts = (computed_at or config.now_local()).replace(microsecond=0)
    flag_rows = [
        (int(r.id), int(r.bout_id), bool(r.pass_anchor), bool(r.pass_shadow),
         bool(r.pass_repeat), bool(r.pass_daypattern), _py(r.shadowed_by),
         bool(r.keep), _py(r.removed_by), ts)
        for r in result.detections.itertuples(index=False)
    ]
    species_rows = [
        (r.common_name, _py(r.scientific_name), int(r.raw), int(r.kept),
         _py(r.pct_kept), _py(r.top_removed_by), int(r.removed_anchor),
         int(r.removed_shadow), int(r.removed_repeat),
         int(r.removed_daypattern), int(r.removed_override),
         bool(r.shadow_exempt), _py(r.median_gap_s), _py(r.gap_cv),
         bool(r.cadence_flag), ts)
        for r in result.species.itertuples(index=False)
    ]
    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f"DELETE FROM {_FLAGS}")
            if flag_rows:
                execute_values(cur, f"""INSERT INTO {_FLAGS}
                    (detection_id, bout_id, pass_anchor, pass_shadow,
                     pass_repeat, pass_daypattern, shadowed_by, keep,
                     removed_by, computed_at)
                    VALUES %s""", flag_rows, page_size=5000)
            cur.execute(f"DELETE FROM {_SPQ}")
            if species_rows:
                execute_values(cur, f"""INSERT INTO {_SPQ}
                    (common_name, scientific_name, raw_count, kept_count,
                     pct_kept, top_removed_by, removed_anchor, removed_shadow,
                     removed_repeat, removed_daypattern, removed_override,
                     shadow_exempt, median_gap_s, gap_cv, cadence_flag,
                     computed_at)
                    VALUES %s""", species_rows, page_size=1000)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return ts


def last_filter_refresh(conn):
    """When the stored flags were last computed, or None."""
    return conn.execute(text(f"SELECT MAX(computed_at) FROM {_SPQ}")).scalar()
