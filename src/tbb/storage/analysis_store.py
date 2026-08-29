"""
analysis_store.py â€” SQLite persistence for ON-DEMAND ANALYSIS (backtest) runs.

STRICT SEPARATION from live state. This module owns analysis_runs.db and never
reads or writes live_state.db; live_store.py never touches this file. They are
separate database files, not merely separate tables, so a query against one can
never pick up the other's rows.

A POST /analyze run is slow (it replays the pipeline bar by bar), so runs are
modelled as jobs: the endpoint returns a job_id immediately and the work happens
in a background thread that updates the job row as it progresses.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pandas as pd

from tbb import config as paths

ANALYSIS_DB = paths.ANALYSIS_DB

# Job lifecycle
QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"


def _resolve(db_path):
    """Resolve at call time so ANALYSIS_DB overrides actually take effect."""
    return db_path or ANALYSIS_DB


def _connect(db_path=None):
    conn = sqlite3.connect(_resolve(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _iso(ts):
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def _to_ms(iso_str):
    if not iso_str:
        return None
    ts = pd.Timestamp(iso_str)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path=None):
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_jobs (
            job_id      TEXT PRIMARY KEY,
            status      TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            timeframe   TEXT,
            start_date  TEXT,
            end_date    TEXT,
            step        INTEGER,
            created_at  TEXT NOT NULL,
            started_at  TEXT,
            finished_at TEXT,
            error       TEXT,
            summary_json TEXT,
            funnel_json  TEXT
        );

        CREATE TABLE IF NOT EXISTS analysis_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT NOT NULL REFERENCES analysis_jobs(job_id),
            trade_id      INTEGER,
            symbol        TEXT NOT NULL,
            timeframe     TEXT,
            direction     TEXT,
            signal_ts     TEXT,
            entry_price   REAL,
            stop_price    REAL,
            take_profit   REAL,
            stop_distance REAL,
            planned_rr    REAL,
            realized_rr   REAL,
            outcome       TEXT,
            pnl           REAL,
            notes         TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_analysis_trades_job ON analysis_trades(job_id);
    """)
    conn.commit()
    conn.close()


def create_job(symbol, timeframe=None, start_date=None, end_date=None, step=4,
               db_path=None) -> str:
    init_db(db_path)
    job_id = uuid.uuid4().hex
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO analysis_jobs
        (job_id, status, symbol, timeframe, start_date, end_date, step, created_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (job_id, QUEUED, symbol, timeframe, start_date, end_date, step, _now()))
    conn.commit()
    conn.close()
    return job_id


def mark_running(job_id, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE analysis_jobs SET status=?, started_at=? WHERE job_id=?",
                 (RUNNING, _now(), job_id))
    conn.commit()
    conn.close()


def mark_error(job_id, message, db_path=None):
    conn = _connect(db_path)
    conn.execute("UPDATE analysis_jobs SET status=?, finished_at=?, error=? WHERE job_id=?",
                 (ERROR, _now(), str(message)[:4000], job_id))
    conn.commit()
    conn.close()


def save_result(job_id, trades, summary, funnel, db_path=None):
    """
    trades: list of dicts (one per resolved trade)
    summary: dict of win_rate / avg_rr / pf_R / ...
    funnel: dict of gate-name -> count
    """
    conn = _connect(db_path)
    cur = conn.cursor()
    for t in trades:
        cur.execute("""
            INSERT INTO analysis_trades
            (job_id, trade_id, symbol, timeframe, direction, signal_ts, entry_price,
             stop_price, take_profit, stop_distance, planned_rr, realized_rr,
             outcome, pnl, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (job_id, t.get("trade_id"), t.get("symbol"), t.get("timeframe"),
              t.get("direction"), _iso(t.get("signal_ts")), t.get("entry_price"),
              t.get("stop_price"), t.get("take_profit"), t.get("stop_distance"),
              t.get("planned_rr"), t.get("realized_rr"), t.get("outcome"),
              t.get("pnl"), t.get("notes")))

    cur.execute("""
        UPDATE analysis_jobs
        SET status=?, finished_at=?, summary_json=?, funnel_json=?
        WHERE job_id=?
    """, (DONE, _now(), json.dumps(summary, default=str),
          json.dumps(funnel, default=str), job_id))
    conn.commit()
    conn.close()


def get_job(job_id, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
    d["funnel"] = json.loads(d.pop("funnel_json")) if d.get("funnel_json") else None
    return d


def get_job_trades(job_id, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM analysis_trades WHERE job_id=? ORDER BY signal_ts", (job_id,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["signal_ts_ms"] = _to_ms(d.get("signal_ts"))
        out.append(d)
    return out


def list_jobs(limit=50, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT job_id, status, symbol, start_date, end_date, step,
               created_at, finished_at, error
        FROM analysis_jobs ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
