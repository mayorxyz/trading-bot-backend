"""
signal_store.py — SQLite log of every signal + outcome tracking.
"""

import sqlite3
from datetime import datetime, timezone

import paths

# Absolute (data/signals.db) — the old cwd-relative default silently wrote
# wherever the process happened to be started from.
DB_PATH = paths.SIGNALS_DB


def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT,
            pattern TEXT,
            shape TEXT,
            entry_price REAL,
            sl_price REAL,
            tp_price REAL,
            rr REAL,
            confluence_score INTEGER,
            valid INTEGER,
            outcome TEXT DEFAULT 'PENDING',
            closed_price REAL,
            closed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_signal(pair, timeframe, direction, entry_price, sl_price, tp_price, rr,
                confluence_score, valid, pattern=None, shape=None, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO signals
        (timestamp, pair, timeframe, direction, pattern, shape, entry_price,
         sl_price, tp_price, rr, confluence_score, valid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), pair, timeframe, direction,
          pattern, shape, entry_price, sl_price, tp_price, rr,
          confluence_score, int(valid)))
    conn.commit()
    conn.close()


def close_signal(signal_id, outcome, closed_price, db_path=DB_PATH):
    """outcome: 'WIN' or 'LOSS'"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        UPDATE signals SET outcome=?, closed_price=?, closed_at=?
        WHERE id=?
    """, (outcome, closed_price, datetime.now(timezone.utc).isoformat(), signal_id))
    conn.commit()
    conn.close()


def get_pending_signals(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM signals WHERE outcome='PENDING' AND valid=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def check_and_close_pending(current_prices: dict, db_path=DB_PATH):
    """
    current_prices: {pair: current_price}
    Checks each pending signal's SL/TP against current price, closes if hit.
    NOTE: simplistic — assumes current price crossing SL/TP = hit. For real
    accuracy, check high/low of each candle since entry, not just latest price.
    """
    pending = get_pending_signals(db_path)
    for sig in pending:
        price = current_prices.get(sig["pair"])
        if price is None:
            continue
        if sig["direction"] == "LONG":
            if price <= sig["sl_price"]:
                close_signal(sig["id"], "LOSS", price, db_path)
            elif price >= sig["tp_price"]:
                close_signal(sig["id"], "WIN", price, db_path)
        elif sig["direction"] == "SHORT":
            if price >= sig["sl_price"]:
                close_signal(sig["id"], "LOSS", price, db_path)
            elif price <= sig["tp_price"]:
                close_signal(sig["id"], "WIN", price, db_path)
