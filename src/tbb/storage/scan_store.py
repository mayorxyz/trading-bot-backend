"""
scan_store.py - SQLite persistence for API-TRIGGERED MULTI-SYMBOL PREDICT scans.

STRICT SEPARATION from live state, analysis runs, and the signal ledger. This
module owns scan_runs.db and never reads or writes any other database; no other
store touches this file. It is a separate database FILE, not merely a separate
table, so a query against one can never pick up the other's rows.

A POST /scan run walks every requested symbol through the pipeline one at a
time (a full quote-book pass can take tens of minutes), so scans are modelled
as jobs: the endpoint returns a job_id immediately and a background thread
updates the job row and appends one scan_results row per symbol as it goes.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pandas as pd

from tbb import config as paths

SCAN_DB = paths.SCAN_DB

# Job lifecycle. RUNNING covers "loop in progress"; STOPPED means the operator
# (POST /scan/stop) asked the loop to finish early - completed symbols stay,
# unprocessed ones are simply gone.
QUEUED, RUNNING, PAUSED, DONE, ERROR, STOPPED = (
    "queued", "running", "paused", "done", "error", "stopped")



def _resolve(db_path):
    """Resolve at call time so SCAN_DB_PATH overrides actually take effect."""
    return db_path or SCAN_DB


def _connect(db_path=None):
    conn = sqlite3.connect(_resolve(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _to_ms(iso_str):
    if not iso_str:
        return None
    ts = pd.Timestamp(iso_str)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def _ensure_scan_result_columns(conn):
    """Additive upgrade for pre-existing scan_runs.db files: add nullable
    logging columns without touching existing data. CREATE TABLE IF NOT
    EXISTS above covers fresh DBs; ALTER covers old ones."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)").fetchall()}
    if "confluence_breakdown_json" not in cols:
        conn.execute("ALTER TABLE scan_results ADD COLUMN confluence_breakdown_json TEXT")
    if "skip_score" not in cols:
        conn.execute("ALTER TABLE scan_results ADD COLUMN skip_score REAL")


def init_db(db_path=None):
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_jobs (
            job_id         TEXT PRIMARY KEY,
            status         TEXT NOT NULL,
            quote          TEXT,
            timeframe      TEXT,
            symbols_total  INTEGER,
            symbols_done   INTEGER NOT NULL DEFAULT 0,
            signals_fired  INTEGER NOT NULL DEFAULT 0,
            skipped        INTEGER NOT NULL DEFAULT 0,
            failed         INTEGER NOT NULL DEFAULT 0,
            current_symbol TEXT,
            params_json    TEXT,
            created_at     TEXT NOT NULL,
            started_at     TEXT,
            finished_at    TEXT,
            error          TEXT
        );

        CREATE TABLE IF NOT EXISTS scan_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id           TEXT NOT NULL REFERENCES scan_jobs(job_id),
            symbol           TEXT NOT NULL,
            timeframe        TEXT,
            predicted_at     TEXT,
            bar_ts           TEXT,
            signal_fired     INTEGER NOT NULL,
            skip_reason      TEXT,
            skip_reason_raw  TEXT,
            direction        TEXT,
            entry            REAL,
            sl               REAL,
            tp               REAL,
            rr               REAL,
            confluence_score REAL,
            confidence       TEXT,
            signal_json      TEXT,
            error            TEXT,
            confluence_breakdown_json TEXT,
            skip_score       REAL,
            UNIQUE(job_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS ix_scan_results_job
            ON scan_results(job_id, signal_fired);
    """)
    _ensure_scan_result_columns(conn)
    conn.commit()
    conn.close()


def create_job(quote=None, timeframe=None, symbols_total=None, params=None,
               db_path=None) -> str:
    init_db(db_path)
    job_id = uuid.uuid4().hex
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO scan_jobs
        (job_id, status, quote, timeframe, symbols_total, params_json, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (job_id, QUEUED, quote, timeframe, symbols_total,
          json.dumps(params, default=str) if params else None, _now()))
    conn.commit()
    conn.close()
    return job_id


def mark_running(job_id, symbols_total=None, db_path=None):
    conn = _connect(db_path)
    if symbols_total is not None:
        conn.execute("""
            UPDATE scan_jobs SET status=?, started_at=?, symbols_total=?
            WHERE job_id=?
        """, (RUNNING, _now(), symbols_total, job_id))
    else:
        conn.execute("UPDATE scan_jobs SET status=?, started_at=? WHERE job_id=?",
                     (RUNNING, _now(), job_id))
    conn.commit()
    conn.close()


def mark_paused(job_id, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE scan_jobs SET status=? WHERE job_id=?",
                 (PAUSED, job_id))
    conn.commit()
    conn.close()


def mark_resumed(job_id, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE scan_jobs SET status=? WHERE job_id=?",
                 (RUNNING, job_id))
    conn.commit()
    conn.close()


def update_progress(job_id, symbols_done=None, signals_fired=None, skipped=None,
                    failed=None, current_symbol=None, db_path=None):
    """Partial progress update - only the fields the caller passes change."""
    fields, values = [], []
    for column, value in (("symbols_done", symbols_done),
                          ("signals_fired", signals_fired),
                          ("skipped", skipped),
                          ("failed", failed),
                          ("current_symbol", current_symbol)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    if not fields:
        return
    values.append(job_id)
    conn = _connect(db_path)
    conn.execute(f"UPDATE scan_jobs SET {', '.join(fields)} WHERE job_id=?", values)
    conn.commit()
    conn.close()


def mark_done(job_id, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE scan_jobs SET status=?, finished_at=? WHERE job_id=?",
                 (DONE, _now(), job_id))
    conn.commit()
    conn.close()


def mark_stopped(job_id, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE scan_jobs SET status=?, finished_at=? WHERE job_id=?",
                 (STOPPED, _now(), job_id))
    conn.commit()
    conn.close()


def mark_error(job_id, message, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE scan_jobs SET status=?, finished_at=?, error=? WHERE job_id=?",
                 (ERROR, _now(), str(message)[:4000], job_id))
    conn.commit()
    conn.close()


def add_result(job_id, row, db_path=None):
    """
    Persist one per-symbol predict result. `row` keys: symbol, timeframe,
    predicted_at, bar_ts, signal_fired, skip_reason, skip_reason_raw, direction,
    entry, sl, tp, rr, confluence_score, confidence, signal, error,
    confluence_breakdown_json, skip_score (last two: skip-only logging, None otherwise).
    REPLACE keeps re-running the same job_id idempotent (UNIQUE job_id+symbol).
    """
    init_db(db_path)
    conn = _connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO scan_results
        (job_id, symbol, timeframe, predicted_at, bar_ts, signal_fired,
         skip_reason, skip_reason_raw, direction, entry, sl, tp, rr,
         confluence_score, confidence, signal_json, error,
         confluence_breakdown_json, skip_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (job_id, row.get("symbol"), row.get("timeframe"), row.get("predicted_at"),
          row.get("bar_ts"), 1 if row.get("signal_fired") else 0,
          row.get("skip_reason"), row.get("skip_reason_raw"), row.get("direction"),
          row.get("entry"), row.get("sl"), row.get("tp"), row.get("rr"),
          row.get("confluence_score"), row.get("confidence"),
          json.dumps(row["signal"], default=str) if row.get("signal") else None,
          row.get("error"), row.get("confluence_breakdown_json"),
          row.get("skip_score")))
    conn.commit()
    conn.close()


def _result_row(r):
    d = dict(r)
    d["signal_fired"] = bool(d.get("signal_fired"))
    d["predicted_at_ms"] = _to_ms(d.get("predicted_at"))
    d["bar_ts_ms"] = _to_ms(d.get("bar_ts"))
    signal_json = d.pop("signal_json", None)
    d["signal"] = json.loads(signal_json) if signal_json else None
    return d


def get_results(job_id, fired=None, limit=500, db_path=None):
    """
    Per-symbol rows for one scan, in scan order. fired=True keeps only signals,
    fired=False only skips/failures. ms fields mirror every *_at timestamp.
    """
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT * FROM scan_results WHERE job_id=?"
    params = [job_id]
    if fired is not None:
        sql += " AND signal_fired=?"
        params.append(1 if fired else 0)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_result_row(r) for r in rows]


def get_job(job_id, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM scan_jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json")) if d.get("params_json") else None
    d["created_at_ms"] = _to_ms(d.get("created_at"))
    d["started_at_ms"] = _to_ms(d.get("started_at"))
    d["finished_at_ms"] = _to_ms(d.get("finished_at"))
    return d


def latest_job_id(db_path=None):
    """Most recently created scan job, queued or finished - GET /scan/results default."""
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT job_id FROM scan_jobs ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return None if row is None else row["job_id"]


def active_job(db_path=None):
    """The queued/running scan, or None - /health and the Results page header."""
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute("""
        SELECT * FROM scan_jobs WHERE status IN (?, ?)
        ORDER BY created_at DESC, rowid DESC LIMIT 1
    """, (QUEUED, RUNNING)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d.pop("params_json", None)
    d["created_at_ms"] = _to_ms(d.get("created_at"))
    d["started_at_ms"] = _to_ms(d.get("started_at"))
    return d


def list_jobs(limit=50, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT job_id, status, quote, timeframe, symbols_total, symbols_done,
               signals_fired, skipped, failed, current_symbol, created_at,
               started_at, finished_at, error
        FROM scan_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]