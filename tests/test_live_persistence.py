"""
test_live_persistence.py â€” proves live_runner PERSISTS state to live_state.db,
and that it persists NOTHING ELSE.

Replays historical candles as if they were arriving live: each iteration hands
live_runner.run_analysis a df_by_tf sliced up to that bar, exactly the shape the
websocket loop builds. Then asserts rows actually landed in live_state.db.

Two guards on top of that:

* data/ must be untouched. The live path is in-memory only; data/*.csv belongs to
  the explicit historical path (ingestion_bybit's CLI, POST /analyze). Reading
  the CSVs to feed the replay is fine â€” creating or modifying one is a
  regression, so every mtime is snapshotted and compared.
* live_state.db must stay a short rolling buffer, so purge_old is exercised
  against a backdated tick.
"""

import glob
import os
import sqlite3
import sys

import pandas as pd

# (sys.path hack removed by restructure; package is pip-installed)

from tbb.live import runner as live_runner
from tbb.storage import live_store
from tbb.backtesting.backtest import load_data, slice_up_to
from tbb import config as paths

# Use a scratch DB so a real live_state.db is never clobbered by the test.
TEST_DB = os.path.join(paths.DATA_DIR, "live_state_test.db")
DATA_DIR = paths.DATA_DIR

START_POS = 3600     # late enough that there is plenty of history
N_TICKS = 14
BAR_STRIDE = 6       # advance this many 1H bars per simulated tick
H1_WINDOW = 700      # rows of 1H kept, mirroring live_runner.MAX_ROWS behaviour


def _data_fingerprint():
    """{path: (mtime_ns, size)} for everything in data/ except this test's own
    scratch DB (the live path legitimately writes live_state.db, which TEST_DB
    stands in for)."""
    skip = {TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"}
    return {p: (os.stat(p).st_mtime_ns, os.stat(p).st_size)
            for p in glob.glob(os.path.join(DATA_DIR, "*"))
            if p not in skip}



def main():
    live_store.LIVE_DB = TEST_DB
    for path in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
        if os.path.exists(path):
            os.remove(path)
    live_store.init_db(TEST_DB)

    df_1d, df_4h, df_1h = load_data("BTCUSDT")
    print(f"loaded BTCUSDT: 1D={len(df_1d)} 4H={len(df_4h)} 1H={len(df_1h)}")

    # Snapshot data/ AFTER the harness has finished reading it, so only writes
    # from the live path itself can show up in the comparison below.
    data_before = _data_fingerprint()

    signals = 0
    for k in range(N_TICKS):
        pos = START_POS + k * BAR_STRIDE
        if pos >= len(df_1h):
            break
        ts = df_1h.index[pos]
        df_by_tf = {
            "1D": slice_up_to(df_1d, ts),
            "4H": slice_up_to(df_4h, ts),
            "1H": df_1h.iloc[max(0, pos + 1 - H1_WINDOW):pos + 1],
        }
        result = live_runner.run_analysis(df_by_tf, execution_tf="1H")
        if "skipped" not in result:
            signals += 1

    print(f"\n--- replayed {k + 1} ticks, {signals} produced a signal ---")

    # ---- assert the live path wrote nothing to disk but live_state.db ----
    print("\n--- data/ must be untouched by the live path ---")
    data_after = _data_fingerprint()
    created = sorted(set(data_after) - set(data_before))
    modified = sorted(p for p in set(data_before) & set(data_after)
                      if data_before[p] != data_after[p])
    print(f"  files before={len(data_before)} after={len(data_after)}")
    print(f"  created:  {[os.path.basename(p) for p in created] or 'NONE'}")
    print(f"  modified: {[os.path.basename(p) for p in modified] or 'NONE'}")

    failures = []
    if created:
        failures.append(f"live path CREATED files in data/: {created}")
    if modified:
        failures.append(f"live path MODIFIED files in data/: {modified}")

    # ---- assert persistence ----
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    counts = {}
    for table in ("live_ticks", "live_tf_state", "live_zones", "live_levels", "live_trades"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print("\nrow counts:", counts)

    if counts["live_ticks"] == 0:
        failures.append("live_ticks is empty â€” nothing was persisted")
    if counts["live_tf_state"] == 0:
        failures.append("live_tf_state is empty â€” bias/regime not persisted")
    if counts["live_levels"] == 0:
        failures.append("live_levels is empty â€” S/R levels not persisted")

    print("\n--- sample tick ---")
    row = conn.execute("SELECT * FROM live_ticks ORDER BY id DESC LIMIT 1").fetchone()
    for key in row.keys():
        val = row[key]
        if key == "signal_json" and val:
            val = val[:80] + "..."
        print(f"  {key:16s} = {val}")

    print("\n--- bias/regime per TF (latest tick) ---")
    for r in conn.execute(
        "SELECT * FROM live_tf_state WHERE tick_id=? ORDER BY timeframe", (row["id"],)
    ):
        print(f"  {r['timeframe']:4s} bias={r['bias']:14s} regime={str(r['regime']):14s} "
              f"is_consolidation={r['is_consolidation']}")

    print("\n--- zones (latest tick that recorded any) ---")
    zt = conn.execute("SELECT MAX(tick_id) FROM live_zones").fetchone()[0]
    for r in conn.execute(
        "SELECT * FROM live_zones WHERE tick_id=? ORDER BY kind, price_low LIMIT 8", (zt,)
    ):
        print(f"  {r['timeframe']:4s} {r['kind']:14s} {str(r['direction']):8s} "
              f"[{r['price_low']:.2f} .. {r['price_high']:.2f}] "
              f"{str(r['start_ts'])[:16]} -> {str(r['end_ts'])[:16]} tested={r['tested']}")

    print("\n--- S/R levels (latest tick, strongest first) ---")
    lt = conn.execute("SELECT MAX(tick_id) FROM live_levels").fetchone()[0]
    for r in conn.execute(
        "SELECT * FROM live_levels WHERE tick_id=? ORDER BY touches DESC LIMIT 6", (lt,)
    ):
        print(f"  {r['timeframe']:4s} {r['level_type']:11s} price={r['price']:.2f} "
              f"touches={r['touches']}")

    print("\n--- live trades ---")
    for r in conn.execute("SELECT * FROM live_trades ORDER BY id"):
        print(f"  id={r['id']} {r['direction']:5s} entry={r['entry_price']:.2f} "
              f"sl={r['stop_price']:.2f} tp={r['take_profit']:.2f} "
              f"outcome={r['outcome']:8s} rr={r['realized_rr']} "
              f"opened={str(r['opened_at'])[:16]} resolved={str(r['resolved_at'])[:16]}")

    print("\n--- distinct skip reasons seen ---")
    for r in conn.execute("""
        SELECT skip_reason, COUNT(*) n FROM live_ticks
        WHERE skip_reason IS NOT NULL GROUP BY skip_reason ORDER BY n DESC
    """):
        print(f"  {r['n']:3d}  {r['skip_reason']}")
    conn.close()

    print("\n--- live_store read API ---")
    print("  live_stats:", live_store.live_stats("BTCUSDT", db_path=TEST_DB))
    print("  current_levels(1H):",
          len(live_store.current_levels("BTCUSDT", "1H", db_path=TEST_DB)), "rows")
    print("  current_zones(1H):",
          len(live_store.current_zones("BTCUSDT", "1H", db_path=TEST_DB)), "rows")
    print("  recent_skips:",
          len(live_store.recent_skips("BTCUSDT", db_path=TEST_DB)), "rows")

    print("\n--- retention: live_state.db is a rolling buffer, not an archive ---")
    info = live_store.retention_info(db_path=TEST_DB)
    print(f"  retention_hours={info['retention_hours']} "
          f"keeps_latest_per_symbol={info['keeps_latest_tick_per_symbol']} "
          f"db_bytes={info['db_bytes']}")

    # Backdate every tick past the window, then purge. The newest tick per symbol
    # must survive (it is the live hand-off) and trades must survive (they are the
    # realized-outcome ledger, not a snapshot).
    conn = sqlite3.connect(TEST_DB)
    conn.execute("UPDATE live_ticks SET recorded_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()

    trades_before = live_store.retention_info(db_path=TEST_DB)["row_counts"]["live_trades"]
    purged = live_store.purge_old(db_path=TEST_DB)
    after = live_store.retention_info(db_path=TEST_DB)["row_counts"]
    print(f"  purged: {purged}")
    print(f"  after:  {after}")

    if purged["live_ticks"] == 0:
        failures.append("purge_old removed no backdated ticks â€” buffer would grow forever")
    if after["live_ticks"] != 1:
        failures.append(f"expected exactly 1 surviving tick (newest for BTCUSDT), "
                        f"got {after['live_ticks']}")
    if after["live_trades"] != trades_before:
        failures.append(f"purge_old destroyed trade ledger rows: "
                        f"{trades_before} -> {after['live_trades']}")
    if live_store.latest_tick("BTCUSDT", db_path=TEST_DB) is None:
        failures.append("latest_tick is None after purge â€” live hand-off broken")
    orphans = 0
    conn = sqlite3.connect(TEST_DB)
    for t in ("live_tf_state", "live_zones", "live_levels"):
        orphans += conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE tick_id NOT IN (SELECT id FROM live_ticks)"
        ).fetchone()[0]
    dangling = conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE tick_id IS NOT NULL "
        "AND tick_id NOT IN (SELECT id FROM live_ticks)").fetchone()[0]
    conn.close()
    print(f"  orphaned child rows={orphans}  dangling trade.tick_id={dangling}")
    if orphans or dangling:
        failures.append(f"purge left {orphans} orphan rows / {dangling} dangling tick_ids")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nPASS â€” live state persists to live_state.db only, and rolls over.")


if __name__ == "__main__":
    main()
