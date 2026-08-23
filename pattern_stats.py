"""
pattern_stats.py — win-rate stats per pattern/pair/timeframe from signal_store.
"""

import sqlite3
from signal_store import DB_PATH


def get_stats(db_path=None, min_occurrences=5):
    """Resolved at CALL time so reassigning pattern_stats.DB_PATH takes effect."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT pattern, pair, timeframe,
               COUNT(*) as occurrences,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               ROUND(100.0 * SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
               ROUND(AVG(rr), 2) as avg_rr
        FROM signals
        WHERE outcome != 'PENDING' AND pattern IS NOT NULL
        GROUP BY pattern, pair, timeframe
        HAVING occurrences >= ?
        ORDER BY occurrences DESC
    """, (min_occurrences,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pattern_winrate(pattern, pair, timeframe, db_path=None):
    """Quick lookup for live use: what's this pattern's track record here?"""
    conn = sqlite3.connect(db_path or DB_PATH)
    row = conn.execute("""
        SELECT COUNT(*) as n,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
        FROM signals
        WHERE outcome != 'PENDING' AND pattern=? AND pair=? AND timeframe=?
    """, (pattern, pair, timeframe)).fetchone()
    conn.close()
    if not row or row[0] == 0:
        return None
    n, wins = row
    return {"occurrences": n, "win_rate": round(100 * wins / n, 1)}
