"""
backtest.py — walks historical data bar-by-bar, no lookahead, runs pipeline
at each step, tracks SL/TP outcomes via TradeJournal.

Usage: python backtest.py
"""

import os
import math

import pandas as pd
import numpy as np
from pipeline import analyze_pair_with_bias
from phase4_risk_journal import TradeJournal

SYMBOL = "BTCUSDT"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT"]
EXECUTION_TF = "1H"
LOOKBACK_BARS = 200      # min bars needed before we start testing
CHECK_FORWARD_BARS = 100  # max bars to look ahead for SL/TP hit
MAX_FILL_WAIT_BARS = 24   # limit entry expires unfilled after this many bars
LEVEL_COOLDOWN_BARS = 24  # a level is barred for this long after its trade closes
LEVEL_BAND_PCT = 0.0025   # entries within 0.25% are treated as the same S/R zone
MAX_CONCURRENT_POSITIONS = 3  # per symbol, and only ever on distinct S/R zones

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Keep the DB beside this file — "/tmp" is not a valid path on Windows.
BACKTEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_signals.db")


def load_data(symbol=SYMBOL):
    def read(tf):
        path = os.path.join(DATA_DIR, f"{symbol.lower()}_{tf}.csv")
        return pd.read_csv(path, index_col=0, parse_dates=True)
    return read("1d"), read("4h"), read("1h")


def slice_up_to(df, ts):
    """No-lookahead: only bars up to and including ts."""
    return df[df.index <= ts]


def check_outcome(df_1h, signal_idx_pos, direction, entry, sl, tp,
                  max_fill_wait=MAX_FILL_WAIT_BARS, max_forward=CHECK_FORWARD_BARS):
    """
    Two-phase forward walk, no lookahead.

    Phase 1 - fill: `entry` is a LIMIT level from find_best_entry, not the
    signal bar's close, so the order only fills if price actually trades to it.
    LONG entries sit at/below price (fill when low <= entry); SHORT entries sit
    at/above it (fill when high >= entry). Never filled within max_fill_wait
    bars -> 'no_fill', and the setup is not counted as a trade at all.

    Phase 2 - outcome: from the fill bar onward, whichever of SL/TP is touched
    first. When a single bar spans both, SL is assumed to hit first: OHLC data
    cannot resolve intrabar order, so we take the unfavourable case rather than
    silently crediting a win.

    Returns (outcome, exit_price, bars_held, bars_to_fill) with outcome one of
    'win' | 'loss' | 'timeout' | 'no_fill'.
    """
    highs = df_1h["high"].values
    lows = df_1h["low"].values
    n = len(df_1h)

    fill_i = None
    fill_end = min(signal_idx_pos + 1 + max_fill_wait, n)
    for i in range(signal_idx_pos + 1, fill_end):
        if direction == "LONG" and lows[i] <= entry:
            fill_i = i
            break
        if direction == "SHORT" and highs[i] >= entry:
            fill_i = i
            break

    if fill_i is None:
        return "no_fill", None, 0, None

    bars_to_fill = fill_i - signal_idx_pos
    end = min(fill_i + max_forward, n)

    # Starts at fill_i, not fill_i + 1: the fill bar itself can carry price on
    # to SL or TP after the entry is touched.
    for i in range(fill_i, end):
        h, l = highs[i], lows[i]
        if direction == "LONG":
            if l <= sl:
                return "loss", sl, i - fill_i, bars_to_fill
            if h >= tp:
                return "win", tp, i - fill_i, bars_to_fill
        else:  # SHORT
            if h >= sl:
                return "loss", sl, i - fill_i, bars_to_fill
            if l <= tp:
                return "win", tp, i - fill_i, bars_to_fill

    return "timeout", None, end - fill_i, bars_to_fill


def level_key(price):
    """
    Bucket a price into a ~LEVEL_BAND_PCT-wide zone, so near-identical S/R
    levels (e.g. 64143.42 and 64175.94, 0.05% apart) share one cooldown slot
    instead of each being treated as a fresh setup. Log-space keeps the band
    proportional at any price level.
    """
    return round(math.log(price) / math.log(1 + LEVEL_BAND_PCT))


def classify_skip(reason):
    """
    Bucket a pipeline 'skipped' string into an actionable gate name.

    Previously every non-consolidation skip was counted as "topdown_blocked",
    which lumped bias rejections together with entry/SL/validity rejections and
    made it impossible to tell which gate was actually rejecting setups.
    Numbers are stripped from validity reasons so they group instead of each
    forming its own unique key.
    """
    if reason.startswith("not tradable"):
        return ("bias: regime=consolidation" if "regime=consolidation" in reason
                else "bias: not aligned")
    if reason.startswith("low conviction"):
        return "confluence: low conviction"
    if reason.startswith("no qualifying entry"):
        return "entry: no qualifying S/R level"
    if reason.startswith("sl/tp"):
        return "sl_tp: calculation failed"
    if reason.startswith("invalid trade"):
        if "inverted" in reason:
            return "validity: inverted SL/TP"
        if "below minimum" in reason:
            return "validity: R:R below minimum"
        if "exceeds" in reason:
            return "validity: entry too far from price"
        if "too wide" in reason:
            return "validity: SL too wide"
        if "too tight" in reason:
            return "validity: SL too tight"
        return "validity: other"
    return f"other: {reason[:50]}"


def _blank_counters():
    return {"insufficient_htf": 0, "position_open": 0, "cooldown": 0, "no_fill": 0,
            "timeouts": 0, "trades": 0, "test_points": 0, "zone_occupied": 0,
            "opposing_concurrent": 0, "skips": {}, "errors": {}}


def _blank_dirs():
    return {"LONG": {"win": 0, "loss": 0, "rr": 0.0},
            "SHORT": {"win": 0, "loss": 0, "rr": 0.0}}


def _align_tz(ts, index):
    """Coerce a user-supplied date to the CSV index's tz so comparisons work."""
    ts = pd.Timestamp(ts)
    tz = getattr(index, "tz", None)
    if tz is None:
        return ts.tz_localize(None) if ts.tzinfo is not None else ts
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert(tz)


def run_symbol(symbol, journal, step=4, trade_id_start=0, verbose=True,
               start_date=None, end_date=None):
    """
    Backtest one symbol into a shared journal.

    Position state (open_positions, level_blocked_until) is local to this call,
    so concurrency limits apply PER SYMBOL — symbols are treated as independent
    accounts. That is deliberate for measuring whether an edge exists at all.
    It is NOT a realistic portfolio equity curve, which would need a shared
    capital pool and a cap on concurrent positions across all symbols
    (see RiskManager in phase4_risk_journal.py).

    Up to MAX_CONCURRENT_POSITIONS may be open at once, but never two on the
    same S/R zone: open_positions is keyed by level_key(), so a zone already
    held is refused rather than stacked.

    start_date/end_date (optional) restrict which test points are evaluated, for
    the on-demand /analyze endpoint. Left as None the bounds are exactly the
    original LOOKBACK_BARS .. n - CHECK_FORWARD_BARS range. Note that forward
    outcome resolution still reads past end_date — a trade opened near the end
    is allowed to resolve rather than being cut off mid-flight.

    Returns (counters, by_dir, next_trade_id).
    """
    df_1d, df_4h, df_1h = load_data(symbol)

    c = _blank_counters()
    by_dir = _blank_dirs()
    trade_id = trade_id_start
    n = len(df_1h)

    # level_key -> (exit_bar, direction) for every position or working order
    # currently occupying a slot.
    open_positions = {}
    level_blocked_until = {}

    lo, hi = LOOKBACK_BARS, n - CHECK_FORWARD_BARS
    if start_date is not None:
        lo = max(lo, int(df_1h.index.searchsorted(_align_tz(start_date, df_1h.index), side="left")))
    if end_date is not None:
        hi = min(hi, int(df_1h.index.searchsorted(_align_tz(end_date, df_1h.index), side="right")))

    points = range(lo, hi, step) if hi > lo else range(0)
    c["test_points"] = len(points)

    for pos in points:
        ts = df_1h.index[pos]

        hist_1d = slice_up_to(df_1d, ts)
        hist_4h = slice_up_to(df_4h, ts)
        hist_1h = df_1h.iloc[:pos + 1]  # up to and including current bar, no future

        # Counted, not silent: if the HTF CSVs start later than the 1H CSV, every
        # early 1H bar is unusable and would otherwise vanish without a trace.
        if len(hist_1d) < 10 or len(hist_4h) < 10 or len(hist_1h) < LOOKBACK_BARS:
            c["insufficient_htf"] += 1
            continue

        # Retire anything that has closed by this bar, then refuse only if every
        # slot is still occupied. Checked before analysis to skip pipeline cost.
        open_positions = {k: v for k, v in open_positions.items() if v[0] > pos}
        if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
            c["position_open"] += 1
            continue

        recent = hist_1h.tail(LOOKBACK_BARS)

        try:
            result = analyze_pair_with_bias(
                pair=symbol, timeframe=EXECUTION_TF,
                df_by_tf={"1D": hist_1d, "4H": hist_4h, "1H": hist_1h},
                opens=recent["open"].to_numpy(dtype=float),
                highs=recent["high"].to_numpy(dtype=float),
                lows=recent["low"].to_numpy(dtype=float),
                closes=recent["close"].to_numpy(dtype=float),
                volumes=recent["volume"].to_numpy(dtype=float),
                db_path=BACKTEST_DB,
            )
        except Exception as e:
            # Never swallow silently — a masked exception is indistinguishable
            # from "no signal found", which is what hid the real bug before.
            key = type(e).__name__
            c["errors"][key] = c["errors"].get(key, 0) + 1
            if c["errors"][key] <= 3:
                print(f"[{symbol} {ts}] ERROR {key}: {e}")
            continue

        if "skipped" in result:
            key = classify_skip(result["skipped"])
            c["skips"][key] = c["skips"].get(key, 0) + 1
            continue

        lk = level_key(result["entry"])
        # Distinct zones only: never stack a second position on a level already held.
        if lk in open_positions:
            c["zone_occupied"] += 1
            continue
        if pos < level_blocked_until.get(lk, -1):
            c["cooldown"] += 1
            continue

        # Diagnostic only, not a gate: how often we end up holding offsetting
        # exposure on one symbol (a LONG at support while a SHORT at resistance
        # is open). Legitimate range-trading, but worth knowing the frequency.
        if any(d != result["direction"] for _, d in open_positions.values()):
            c["opposing_concurrent"] += 1

        outcome, exit_price, bars_held, bars_to_fill = check_outcome(
            df_1h, pos, result["direction"], result["entry"], result["sl"], result["tp"]
        )
        if outcome == "no_fill":
            c["no_fill"] += 1
            # A working order still occupies its slot until it expires, and the
            # zone is cooled down so an unreachable level is not retried on
            # every subsequent test point.
            expiry = pos + MAX_FILL_WAIT_BARS
            open_positions[lk] = (expiry, result["direction"])
            level_blocked_until[lk] = expiry + LEVEL_COOLDOWN_BARS
            continue

        # Position is held from fill until SL/TP; the slot frees at exit.
        exit_pos = pos + bars_to_fill + bars_held
        open_positions[lk] = (exit_pos, result["direction"])
        level_blocked_until[lk] = exit_pos + LEVEL_COOLDOWN_BARS

        if outcome == "timeout":
            c["timeouts"] += 1
            continue

        pnl = (exit_price - result["entry"]) if result["direction"] == "LONG" else (result["entry"] - exit_price)
        realized_rr = pnl / abs(result["entry"] - result["sl"]) if result["entry"] != result["sl"] else 0

        trade_id += 1
        c["trades"] += 1
        d = by_dir[result["direction"]]
        d[outcome] += 1
        d["rr"] += realized_rr
        journal.log_trade(
            trade_id=trade_id, timestamp=ts, pair=symbol, direction=result["direction"],
            entry_model="pipeline", htf_poi_tf="1D", poi_id=f"bt_{trade_id}",
            entry_price=result["entry"], stop_price=result["sl"], take_profit=result["tp"],
            stop_distance=abs(result["entry"] - result["sl"]), planned_rr=result["rr"],
            realized_rr=realized_rr, risk_pct_used=0.01, position_size=None,
            outcome=outcome, pnl=pnl, balance_after=None, notes=f"confidence={result['confidence']}",
        )
        if verbose:
            print(f"[{symbol} {ts}] {result['direction']} entry={result['entry']:.4f} -> {outcome} "
                  f"(RR={realized_rr:.2f}, filled after {bars_to_fill}, held {bars_held} bars)")

    return c, by_dir, trade_id


def _merge(into, c):
    for k in ("insufficient_htf", "position_open", "cooldown", "no_fill",
              "timeouts", "trades", "test_points", "zone_occupied",
              "opposing_concurrent"):
        into[k] += c[k]
    for bucket in ("skips", "errors"):
        for k, v in c[bucket].items():
            into[bucket][k] = into[bucket].get(k, 0) + v


def rr_profit_factor(closed):
    """
    Profit factor computed from R-multiples, not price deltas.

    summary_stats()'s profit_factor and net_pnl come from raw price differences,
    which are NOT comparable across symbols: a BTC move is ~1e4 and a DOGE move
    is ~1e-1, so summing them lets BTC drown out everything else. R-multiples
    are already normalised by stop distance, so they aggregate correctly.
    """
    rr = closed["realized_rr"]
    gross_win = rr[rr > 0].sum()
    gross_loss = abs(rr[rr < 0].sum())
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def print_report(label, c, by_dir, stats, rr_pf=None):
    print(f"\n=== {label} ===")
    print(stats)
    if rr_pf is not None:
        print(f"profit_factor_R (scale-free): {rr_pf:.3f}")

    print(f"\n--- funnel ({c['test_points']} test points) ---")
    print(f"{c['insufficient_htf']:5d}  insufficient HTF history (HTF CSV starts after 1H CSV)")
    print(f"{c['position_open']:5d}  all {MAX_CONCURRENT_POSITIONS} position slots busy")
    print(f"{c['zone_occupied']:5d}  zone already held (distinct-zone rule)")
    print(f"{c['cooldown']:5d}  level in cooldown ({LEVEL_COOLDOWN_BARS} bars, {LEVEL_BAND_PCT:.2%} zone)")
    for key, count in sorted(c["skips"].items(), key=lambda kv: -kv[1]):
        print(f"{count:5d}  {key}")
    print(f"{c['no_fill']:5d}  no-fill (limit never reached in {MAX_FILL_WAIT_BARS} bars)")
    print(f"{c['timeouts']:5d}  timeout (filled, no SL/TP in {CHECK_FORWARD_BARS} bars)")
    print(f"{c['trades']:5d}  TRADES RESOLVED")

    accounted = (c["insufficient_htf"] + c["position_open"] + c["zone_occupied"]
                 + c["cooldown"] + sum(c["skips"].values()) + c["no_fill"]
                 + c["timeouts"] + c["trades"])
    print(f"{accounted:5d}  accounted of {c['test_points']}"
          f"{'' if accounted == c['test_points'] else '  <-- MISMATCH'}")
    print(f"exceptions: {c['errors'] if c['errors'] else 'none'}")
    print(f"opened while holding opposite direction: {c['opposing_concurrent']} "
          f"(diagnostic, not a gate)")

    print("\n--- by direction ---")
    for name, s in by_dir.items():
        t = s["win"] + s["loss"]
        if t:
            print(f"{name:5s} trades={t:4d}  wins={s['win']:4d}  "
                  f"win_rate={s['win'] / t:.3f}  avg_rr={s['rr'] / t:+.3f}")


def run_multi(symbols=SYMBOLS, step=4, verbose=False):
    """Backtest every symbol into one shared journal, then report combined."""
    journal = TradeJournal()
    total = _blank_counters()
    all_dir = _blank_dirs()
    trade_id = 0
    done = []

    for sym in symbols:
        try:
            c, by_dir, trade_id = run_symbol(sym, journal, step, trade_id, verbose)
        except FileNotFoundError as e:
            print(f"{sym:10s} SKIPPED - missing data: {e}")
            continue
        _merge(total, c)
        for side in all_dir:
            for f in ("win", "loss", "rr"):
                all_dir[side][f] += by_dir[side][f]
        done.append(sym)
        print(f"{sym:10s} points={c['test_points']:5d}  trades={c['trades']:4d}  "
              f"no_fill={c['no_fill']:4d}  slots_busy={c['position_open']:5d}  "
              f"zone_held={c['zone_occupied']:4d}")

    df = journal.to_dataframe()
    print("\n--- per symbol ---")
    for sym in done:
        closed = df[(df["pair"] == sym) & (df["outcome"].isin(["win", "loss"]))]
        if closed.empty:
            print(f"{sym:10s} no resolved trades")
            continue
        print(f"{sym:10s} trades={len(closed):4d}  "
              f"win_rate={(closed['outcome'] == 'win').mean():.3f}  "
              f"avg_rr={closed['realized_rr'].mean():+.3f}  "
              f"pf_R={rr_profit_factor(closed):.3f}")

    closed_all = df[df["outcome"].isin(["win", "loss"])]
    print_report(f"COMBINED - {len(done)} symbols", total, all_dir,
                 journal.summary_stats(),
                 rr_pf=rr_profit_factor(closed_all) if not closed_all.empty else None)
    print("\nNOTE: summary_stats() net_pnl/profit_factor are price-delta based and are")
    print("      NOT cross-symbol comparable. Use win_rate, avg_rr and profit_factor_R.")
    return journal, journal.summary_stats()


def run_backtest(step=4, symbol=SYMBOL):
    """Single-symbol backtest (original behaviour)."""
    journal = TradeJournal()
    c, by_dir, _ = run_symbol(symbol, journal, step, 0, verbose=True)
    stats = journal.summary_stats()
    df = journal.to_dataframe()
    closed = df[df["outcome"].isin(["win", "loss"])]
    print_report(f"BACKTEST SUMMARY - {symbol}", c, by_dir, stats,
                 rr_pf=rr_profit_factor(closed) if not closed.empty else None)
    return journal, stats


if __name__ == "__main__":
    run_multi(step=4)