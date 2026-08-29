"""
live_store.py â€” SQLite hand-off buffer for LIVE runner state.

live_runner.py computes bias / regime / skip reasons / zones / levels in memory.
The frontend cannot read another process's memory, so each analysis tick is
written here and api.py reads it back. That hand-off is the ONLY reason this file
exists.

It is therefore a SHORT ROLLING BUFFER, not an archive: purge_old() drops tick
snapshots older than LIVE_RETENTION_HOURS (default 6), and live_runner calls it
after every tick. See purge_old for exactly what is kept and why.

STRICT SEPARATION: this module owns live_state.db and nothing else writes to
it. On-demand backtests live in analysis_store.py / analysis_runs.db so live
and analysis results can never mix. See that module's docstring.

Schema notes
------------
Zones and levels are snapshotted per tick rather than mutated in place, so
"current state" is simply the newest tick's rows (see current_zones /
current_levels).

Timestamps are stored as ISO-8601 UTC text. Every row read back through this
module also carries an `*_ms` epoch-milliseconds field, which is what charting
libraries want on the x-axis.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from tbb import config as paths

LIVE_DB = paths.LIVE_DB

# How long a tick snapshot survives. This is a live hand-off buffer, so hours,
# not days â€” long enough to ride out an api.py restart or a quiet market, short
# enough that the file never becomes an accidental history store.
LIVE_RETENTION_HOURS = float(os.environ.get("LIVE_RETENTION_HOURS", 6))


# ---------- helpers ----------

def _resolve(db_path):
    """
    Resolve the DB path at CALL time, not at def time.

    Every public function defaults db_path to None and funnels through here, so
    reassigning live_store.LIVE_DB (or setting LIVE_DB_PATH) actually takes
    effect. Binding the constant directly as a default argument would freeze it
    at import and silently ignore any override.
    """
    return db_path or LIVE_DB


def _connect(db_path=None):
    conn = sqlite3.connect(_resolve(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the API read while the live runner writes.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _iso(ts) -> str:
    """Normalise anything timestamp-ish to ISO-8601 UTC text."""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def to_ms(iso_str):
    """ISO-8601 text -> epoch milliseconds (chart x-coordinate). None-safe."""
    if not iso_str:
        return None
    ts = pd.Timestamp(iso_str)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def _row_with_ms(row, ts_fields=("ts", "start_ts", "end_ts", "opened_at",
                                 "filled_at", "resolved_at")):
    d = dict(row)
    for f in ts_fields:
        if f in d:
            d[f + "_ms"] = to_ms(d[f])
    return d


# ---------- schema ----------

def init_db(db_path=None):
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS live_ticks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,      -- candle close that triggered the tick
            recorded_at     TEXT NOT NULL,      -- wall clock when we wrote the row
            symbol          TEXT NOT NULL,
            execution_tf    TEXT NOT NULL,      -- TF whose close triggered analysis
            topdown_bias    TEXT,
            aligned_count   INTEGER,
            tradable        INTEGER,
            direction       TEXT,
            skip_reason     TEXT,               -- classify_skip() category, NULL if a signal fired
            skip_reason_raw TEXT,
            signal_json     TEXT                -- full pipeline result when not skipped
        );
        CREATE INDEX IF NOT EXISTS ix_ticks_symbol_ts ON live_ticks(symbol, id DESC);

        -- One row per timeframe per tick: bias + regime.
        CREATE TABLE IF NOT EXISTS live_tf_state (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id          INTEGER NOT NULL REFERENCES live_ticks(id),
            ts               TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            bias             TEXT,              -- bullish / bearish / consolidation
            regime           TEXT,              -- chop / consolidation / trending
            is_consolidation INTEGER            -- regime == 'consolidation'
        );
        CREATE INDEX IF NOT EXISTS ix_tf_state_tick ON live_tf_state(tick_id);

        CREATE TABLE IF NOT EXISTS live_zones (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id    INTEGER NOT NULL REFERENCES live_ticks(id),
            ts         TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            kind       TEXT NOT NULL,           -- 'fvg' | 'consolidation'
            direction  TEXT,                    -- bullish / bearish for fvg, NULL for consolidation
            price_low  REAL NOT NULL,
            price_high REAL NOT NULL,
            start_ts   TEXT,
            end_ts     TEXT,
            tested     INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_zones_lookup ON live_zones(symbol, timeframe, tick_id);

        CREATE TABLE IF NOT EXISTS live_levels (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id    INTEGER NOT NULL REFERENCES live_ticks(id),
            ts         TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            price      REAL NOT NULL,
            touches    INTEGER,
            level_type TEXT                     -- SUPPORT / RESISTANCE / BOTH
        );
        CREATE INDEX IF NOT EXISTS ix_levels_lookup ON live_levels(symbol, timeframe, tick_id);

        -- Resolved-trade ledger, column-compatible with risk_journal.TradeJournal
        -- so the same summary maths can be applied to live and backtest rows alike.
        CREATE TABLE IF NOT EXISTS live_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id       INTEGER REFERENCES live_ticks(id),
            symbol        TEXT NOT NULL,
            timeframe     TEXT,
            direction     TEXT,
            opened_at     TEXT NOT NULL,
            filled_at     TEXT,
            resolved_at   TEXT,
            entry_price   REAL,
            stop_price    REAL,
            take_profit   REAL,
            stop_distance REAL,
            planned_rr    REAL,
            realized_rr   REAL,
            -- pending (waiting on limit fill) / open (filled) / win / loss / no_fill
            outcome       TEXT DEFAULT 'pending',
            pnl           REAL,
            confluence    INTEGER,
            notes         TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_trades_symbol ON live_trades(symbol, outcome);
    """)
    _ensure_trade_columns(conn)
    conn.commit()
    conn.close()


def _ensure_trade_columns(conn):
    """
    Idempotent migration for managed-trade columns (trade_manager.py).
    Older DBs get the columns added; fresh schemas already have them via
    CREATE TABLE above â€” PRAGMA check makes both paths safe.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
    if "tp1_filled" not in cols:
        conn.execute("ALTER TABLE live_trades ADD COLUMN tp1_filled INTEGER DEFAULT 0")
    if "tp1_price" not in cols:
        conn.execute("ALTER TABLE live_trades ADD COLUMN tp1_price REAL")
    if "stop_current" not in cols:
        conn.execute("ALTER TABLE live_trades ADD COLUMN stop_current REAL")
    if "exit_price" not in cols:
        conn.execute("ALTER TABLE live_trades ADD COLUMN exit_price REAL")


# ---------- retention ----------

def purge_old(retention_hours=None, keep_latest_per_symbol=True, db_path=None) -> dict:
    """
    Drop tick snapshots older than `retention_hours`, plus their tf_state / zones
    / levels rows. Returns {table: rows_deleted}.

    Cut on `recorded_at` (wall clock when the row was written), not `ts` (the
    candle it describes) â€” this bounds how long the buffer holds output, which is
    the point.

    Two deliberate exceptions:

    * keep_latest_per_symbol keeps each symbol's newest tick regardless of age.
      The whole contract of this table is "what is the current live state", and
      /live/state, /live/zones and /live/levels all read exactly that one row.
      Purging it would turn a stale-but-real overlay into no overlay at all â€”
      whereas api.py's `overlay_freshness` block already tells the frontend the
      snapshot is old. Cost is one tick per symbol, so growth stays O(symbols).
      Pass False for a strict age-only purge.

    * live_trades is never purged. It is the realized-outcome ledger behind
      /live/stats, not a snapshot, and dropping it would silently reset live
      win-rate and R stats. Its tick_id is nulled when the tick it referenced
      goes, so no row is left pointing at a deleted parent.
    """
    hours = LIVE_RETENTION_HOURS if retention_hours is None else float(retention_hours)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    init_db(db_path)
    conn = _connect(db_path)
    cur = conn.cursor()
    deleted = {}

    keep = []
    if keep_latest_per_symbol:
        keep = [r[0] for r in cur.execute(
            "SELECT MAX(id) FROM live_ticks GROUP BY symbol").fetchall()]

    placeholders = ",".join("?" * len(keep))
    sql = "DELETE FROM live_ticks WHERE recorded_at < ?"
    params = [cutoff]
    if keep:
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(keep)
    cur.execute(sql, params)
    deleted["live_ticks"] = cur.rowcount

    # Child rows follow their tick. Written as an orphan sweep rather than an
    # id list so it also cleans up anything left behind by an earlier crash.
    for table in ("live_tf_state", "live_zones", "live_levels"):
        cur.execute(f"DELETE FROM {table} "
                    f"WHERE tick_id NOT IN (SELECT id FROM live_ticks)")
        deleted[table] = cur.rowcount

    cur.execute("UPDATE live_trades SET tick_id=NULL "
                "WHERE tick_id IS NOT NULL "
                "AND tick_id NOT IN (SELECT id FROM live_ticks)")
    deleted["live_trades_unlinked"] = cur.rowcount

    conn.commit()
    conn.close()
    return deleted


def retention_info(db_path=None) -> dict:
    """What the buffer currently holds â€” surfaced on /health."""
    init_db(db_path)
    conn = _connect(db_path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("live_ticks", "live_tf_state", "live_zones",
                        "live_levels", "live_trades")}
    row = conn.execute("SELECT MIN(recorded_at), MAX(recorded_at) "
                       "FROM live_ticks").fetchone()
    conn.close()
    try:
        size = os.path.getsize(_resolve(db_path))
    except OSError:
        size = None
    return {
        "retention_hours": LIVE_RETENTION_HOURS,
        "purged_on": "recorded_at",
        "keeps_latest_tick_per_symbol": True,
        "trades_retained_indefinitely": True,
        "row_counts": counts,
        "oldest_tick_recorded_at": row[0],
        "newest_tick_recorded_at": row[1],
        "db_bytes": size,
    }


# ---------- writes ----------

def record_tick(snapshot, db_path=None) -> int:
    """
    Persist one analysis tick. `snapshot` is the dict built by
    live_runner.collect_state(). Returns the new tick_id.
    """
    init_db(db_path)
    conn = _connect(db_path)
    cur = conn.cursor()
    ts = _iso(snapshot["ts"])

    cur.execute("""
        INSERT INTO live_ticks
        (ts, recorded_at, symbol, execution_tf, topdown_bias, aligned_count,
         tradable, direction, skip_reason, skip_reason_raw, signal_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts, datetime.now(timezone.utc).isoformat(), snapshot["symbol"],
        snapshot["execution_tf"], snapshot.get("topdown_bias"),
        snapshot.get("aligned_count"),
        None if snapshot.get("tradable") is None else int(bool(snapshot["tradable"])),
        snapshot.get("direction"), snapshot.get("skip_reason"),
        snapshot.get("skip_reason_raw"),
        json.dumps(snapshot["signal"], default=str) if snapshot.get("signal") else None,
    ))
    tick_id = cur.lastrowid

    for tf, st in snapshot.get("tf_state", {}).items():
        cur.execute("""
            INSERT INTO live_tf_state
            (tick_id, ts, symbol, timeframe, bias, regime, is_consolidation)
            VALUES (?,?,?,?,?,?,?)
        """, (tick_id, ts, snapshot["symbol"], tf, st.get("bias"), st.get("regime"),
              None if st.get("regime") is None else int(st["regime"] == "consolidation")))

    for z in snapshot.get("zones", []):
        cur.execute("""
            INSERT INTO live_zones
            (tick_id, ts, symbol, timeframe, kind, direction, price_low,
             price_high, start_ts, end_ts, tested)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (tick_id, ts, snapshot["symbol"], z["timeframe"], z["kind"],
              z.get("direction"), z["price_low"], z["price_high"],
              _iso(z.get("start_ts")), _iso(z.get("end_ts")),
              None if z.get("tested") is None else int(bool(z["tested"]))))

    for lv in snapshot.get("levels", []):
        cur.execute("""
            INSERT INTO live_levels
            (tick_id, ts, symbol, timeframe, price, touches, level_type)
            VALUES (?,?,?,?,?,?,?)
        """, (tick_id, ts, snapshot["symbol"], lv["timeframe"], lv["price"],
              lv.get("touches"), lv.get("level_type")))

    conn.commit()
    conn.close()
    return tick_id


def open_trade(tick_id, symbol, timeframe, direction, opened_at, entry_price,
               stop_price, take_profit, planned_rr, confluence=None, notes=None,
               tp1_price=None, db_path=None) -> int:
    """Record a fired live signal as a pending trade."""
    init_db(db_path)
    conn = _connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO live_trades
        (tick_id, symbol, timeframe, direction, opened_at, entry_price, stop_price,
         take_profit, stop_distance, planned_rr, outcome, confluence, notes,
         tp1_price, stop_current)
        VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)
    """, (tick_id, symbol, timeframe, direction, _iso(opened_at), entry_price,
          stop_price, take_profit, abs(entry_price - stop_price), planned_rr,
          confluence, notes, tp1_price, stop_price))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def pending_trades(symbol=None, db_path=None):
    """
    Trades still in flight: 'pending' (limit not yet filled) and 'open' (filled,
    waiting on SL/TP). These are what the runner re-checks on each new candle.
    """
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT * FROM live_trades WHERE outcome IN ('pending','open')"
    params = ()
    if symbol:
        sql += " AND symbol=?"
        params = (symbol,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_filled(trade_id, filled_at, db_path=None):
    """Limit entry has been touched â€” the trade is now live."""
    conn = _connect(db_path)
    conn.execute("UPDATE live_trades SET outcome='open', filled_at=? WHERE id=?",
                 (_iso(filled_at), trade_id))
    conn.commit()
    conn.close()


def mark_no_fill(trade_id, db_path=None):
    """Limit never reached before expiry â€” never became a trade, excluded from stats."""
    conn = _connect(db_path)
    conn.execute("UPDATE live_trades SET outcome='no_fill' WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()

def resolve_trade(trade_id, outcome, resolved_at, exit_price, db_path=None):
    """
    Close a pending live trade. realized_rr is derived from the recorded entry
    and stop so it is directly comparable with the backtest's realized_rr.
    """
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM live_trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        conn.close()
        raise KeyError(f"no live trade with id={trade_id}")

    pnl = ((exit_price - row["entry_price"]) if row["direction"] == "LONG"
           else (row["entry_price"] - exit_price))
    dist = abs(row["entry_price"] - row["stop_price"])
    realized_rr = pnl / dist if dist > 0 else 0.0

    conn.execute("""
        UPDATE live_trades SET outcome=?, resolved_at=?, realized_rr=?, pnl=?
        WHERE id=?
    """, (outcome, _iso(resolved_at), realized_rr, pnl, trade_id))
    conn.commit()
    conn.close()
    return {"trade_id": trade_id, "outcome": outcome,
            "realized_rr": realized_rr, "pnl": pnl}


def update_trade_management(trade_id, tp1_filled=None, stop_current=None,
                            tp1_price=None, db_path=None):
    """Persist the manager's ratcheting state between ticks."""
    sets, params = [], []
    if tp1_filled is not None:
        sets.append("tp1_filled=?")
        params.append(int(bool(tp1_filled)))
    if stop_current is not None:
        sets.append("stop_current=?")
        params.append(float(stop_current))
    if tp1_price is not None:
        sets.append("tp1_price=?")
        params.append(float(tp1_price))
    if not sets:
        return
    params.append(trade_id)
    conn = _connect(db_path)
    conn.execute(f"UPDATE live_trades SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()


def resolve_trade_managed(trade_id, outcome, resolved_at, exit_price,
                          realized_rr, pnl, db_path=None):
    """
    Close a managed trade whose realized_rr combines the TP1 half and the
    trailed remainder â€” the manager computes both, so we store them as given
    instead of re-deriving a flat SL/TP result.
    """
    conn = _connect(db_path)
    conn.execute("""
        UPDATE live_trades SET outcome=?, resolved_at=?, exit_price=?, realized_rr=?, pnl=?
        WHERE id=?
    """, (outcome, _iso(resolved_at), float(exit_price), float(realized_rr),
          float(pnl), trade_id))
    conn.commit()
    conn.close()
    return {"trade_id": trade_id, "outcome": outcome,
            "realized_rr": realized_rr, "pnl": pnl}


# ---------- reads (API surface) ----------

def latest_tick(symbol, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM live_ticks WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)
    ).fetchone()
    conn.close()
    return _row_with_ms(row) if row else None


def tf_state_for_tick(tick_id, db_path=None):
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM live_tf_state WHERE tick_id=?", (tick_id,)
    ).fetchall()
    conn.close()
    return {r["timeframe"]: {"bias": r["bias"], "regime": r["regime"],
                             "is_consolidation": bool(r["is_consolidation"])
                             if r["is_consolidation"] is not None else None}
            for r in rows}


def recent_skips(symbol, limit=20, db_path=None):
    """Last N ticks that were skipped â€” the live funnel feed."""
    init_db(db_path)
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT ts, execution_tf, skip_reason, skip_reason_raw
        FROM live_ticks
        WHERE symbol=? AND skip_reason IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (symbol, limit)).fetchall()
    conn.close()
    return [_row_with_ms(r) for r in rows]


def skip_reason_counts(symbol, db_path=None):
    """Aggregate funnel: how often each gate has rejected, all time."""
    init_db(db_path)
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT skip_reason, COUNT(*) AS n FROM live_ticks
        WHERE symbol=? AND skip_reason IS NOT NULL
        GROUP BY skip_reason ORDER BY n DESC
    """, (symbol,)).fetchall()
    conn.close()
    return {r["skip_reason"]: r["n"] for r in rows}


def current_zones(symbol, timeframe=None, db_path=None):
    """
    Zones from the most recent tick that actually recorded zones for this
    symbol (+ timeframe). Falling back to the newest tick outright would return
    empty whenever the latest tick short-circuited before zone collection.
    """
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT MAX(tick_id) FROM live_zones WHERE symbol=?"
    params = [symbol]
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    tick_id = conn.execute(sql, params).fetchone()[0]
    if tick_id is None:
        conn.close()
        return []

    sql = "SELECT * FROM live_zones WHERE tick_id=? AND symbol=?"
    params = [tick_id, symbol]
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    rows = conn.execute(sql + " ORDER BY price_low", params).fetchall()
    conn.close()
    return [_row_with_ms(r) for r in rows]


def current_levels(symbol, timeframe=None, db_path=None):
    """S/R levels from the most recent tick that recorded any, strongest first."""
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT MAX(tick_id) FROM live_levels WHERE symbol=?"
    params = [symbol]
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    tick_id = conn.execute(sql, params).fetchone()[0]
    if tick_id is None:
        conn.close()
        return []

    sql = "SELECT * FROM live_levels WHERE tick_id=? AND symbol=?"
    params = [tick_id, symbol]
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    rows = conn.execute(sql + " ORDER BY touches DESC", params).fetchall()
    conn.close()
    return [_row_with_ms(r) for r in rows]


def live_stats(symbol=None, db_path=None):
    """
    Running win_rate / avg_rr / profit_factor_R over RESOLVED LIVE trades only.

    pf_R is R-multiple based, matching backtest.rr_profit_factor â€” raw price
    deltas are not comparable across symbols.
    """
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT * FROM live_trades WHERE outcome IN ('win','loss')"
    params = ()
    if symbol:
        sql += " AND symbol=?"
        params = (symbol,)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    in_flight = conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE outcome IN ('pending','open')"
        + (" AND symbol=?" if symbol else ""), params
    ).fetchone()[0]

    # "Today" is UTC and counts RESOLUTIONS (resolved_at), because that is when
    # the R was actually banked â€” an open trade from this morning has no
    # outcome yet. pnl stays in price-delta units (rr * stop distance); it is
    # not account USD, since position size is not modelled.
    today = datetime.now(timezone.utc).date().isoformat()
    tsql = ("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM live_trades "
            "WHERE outcome IN ('win','loss') AND substr(resolved_at, 1, 10)=?")
    tparams = [today]
    if symbol:
        tsql += " AND symbol=?"
        tparams.append(symbol)
    trades_today, today_pnl = conn.execute(tsql, tparams).fetchone()
    conn.close()

    if not rows:
        return {"symbol": symbol, "total_trades": 0, "in_flight_trades": in_flight,
                "win_rate": None, "avg_rr": None, "profit_factor_R": None,
                "wins": 0, "losses": 0,
                "trades_today": trades_today, "today_pnl": float(today_pnl)}

    rr = [r["realized_rr"] for r in rows if r["realized_rr"] is not None]
    wins = [r for r in rows if r["outcome"] == "win"]
    gross_win = sum(x for x in rr if x > 0)
    gross_loss = abs(sum(x for x in rr if x < 0))

    return {
        "symbol": symbol,
        "total_trades": len(rows),
        "in_flight_trades": in_flight,
        "wins": len(wins),
        "losses": len(rows) - len(wins),
        "win_rate": len(wins) / len(rows),
        "avg_rr": (sum(rr) / len(rr)) if rr else None,
        "profit_factor_R": (gross_win / gross_loss) if gross_loss > 0
                           else (float("inf") if gross_win > 0 else None),
        "trades_today": trades_today,
        "today_pnl": float(today_pnl),
    }


# How long after the newest tick we still call the runner alive. The runner
# ticks on every confirmed 1H/15M close, so an hour of quiet market is normal;
# two hours without any tick means the process is almost certainly down.
RUNNER_STALE_AFTER = float(os.environ.get("LIVE_RUNNER_STALE_AFTER", 7200))


def runner_heartbeat(db_path=None, stale_after=None) -> dict:
    """
    Is the live_runner process alive? Inferred from the age of the newest tick:
    the runner writes one per confirmed trigger-TF close, so a fresh timestamp
    is proof of life and a very old one means it is not running (or cannot
    reach Bybit). 'never_run' distinguishes a clean first boot from a dead one.
    """
    hours = RUNNER_STALE_AFTER if stale_after is None else float(stale_after)
    init_db(db_path)
    conn = _connect(db_path)
    row = conn.execute("SELECT MAX(recorded_at) FROM live_ticks").fetchone()
    conn.close()

    newest = row[0] if row else None
    status = "never_run"
    age = None
    if newest:
        ts = pd.Timestamp(newest)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        age = max(0.0, (datetime.now(timezone.utc) - ts.to_pydatetime())
                  .total_seconds())
        status = "running" if age <= hours else "stale"
    return {
        "status": status,                       # running | stale | never_run
        "last_tick_age_seconds": round(age, 1) if age is not None else None,
        "stale_after_seconds": hours,
        "newest_tick_recorded_at": newest,
    }


def resolved_trades(symbol=None, limit=200, db_path=None):
    init_db(db_path)
    conn = _connect(db_path)
    sql = "SELECT * FROM live_trades WHERE outcome IN ('win','loss')"
    params = []
    if symbol:
        sql += " AND symbol=?"
        params.append(symbol)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_row_with_ms(r) for r in rows]
