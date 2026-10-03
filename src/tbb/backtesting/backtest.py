"""
backtest.py â€” walks historical data bar-by-bar, no lookahead, runs pipeline
at each step, tracks SL/TP outcomes via TradeJournal.

Usage: python backtest.py
"""

import os
import math
import sys
import sqlite3

import pandas as pd
import numpy as np
from tbb.pipeline import analyze_pair_with_bias
from tbb.analysis.risk_journal import TradeJournal

from tbb import config as paths
from tbb.live import trade_manager
from tbb.backtesting.rejection_diagnostics import (RejectionDiagnostics,
                                                   diagnostics_enabled)

SYMBOL = "BTCUSDT"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT"]
EXECUTION_TF = "1H"
LOOKBACK_BARS = 200      # min bars needed before we start testing
CHECK_FORWARD_BARS = 100  # max bars to look ahead for SL/TP hit
MAX_FILL_WAIT_BARS = 24   # limit entry expires unfilled after this many bars
LEVEL_COOLDOWN_BARS = 24  # a level is barred for this long after its trade closes
LEVEL_BAND_PCT = 0.0025   # entries within 0.25% are treated as the same S/R zone
MAX_CONCURRENT_POSITIONS = 3  # per symbol, and only ever on distinct S/R zones

# Trading frictions (read at import; override via env for A/B).
# Applied ONLY to accounting (realized_rr / pnl), never to signal, fill or
# SL/TP logic. Both legs pay COST_BPS_PER_SIDE + SLIPPAGE_BPS on notional.
COST_BPS_PER_SIDE = float(os.environ.get("COST_BPS_PER_SIDE", "10"))
SLIPPAGE_BPS = float(os.environ.get("SLIPPAGE_BPS", "5"))


def cost_frac_per_side():
    """Combined fee+slippage fraction of notional paid on each leg."""
    return (COST_BPS_PER_SIDE + SLIPPAGE_BPS) / 10000.0


def apply_trading_costs(entry, exit_price, sl, realized_rr_gross, pnl_gross):
    """Convert gross realized_R/pnl to net of entry+exit costs.

    cost_price = entry * frac + exit * frac; pnl_net = pnl_gross - cost_price;
    rr_net = pnl_net / |entry - sl|. Pure accounting helper — no strategy
    input is modified.
    """
    try:
        risk = abs(float(entry) - float(sl))
    except (TypeError, ValueError):
        risk = 0.0
    if risk <= 0 or exit_price is None or realized_rr_gross is None or pnl_gross is None:
        return realized_rr_gross, pnl_gross, 0.0
    frac = cost_frac_per_side()
    cost_price = abs(float(entry)) * frac + abs(float(exit_price)) * frac
    pnl_net = float(pnl_gross) - cost_price
    return pnl_net / risk, pnl_net, cost_price / risk

DATA_DIR = paths.DATA_DIR

# Pipeline logging lands beside the other databases in data/ (see paths.py) â€”
# "/tmp" is not a valid path on Windows.
BACKTEST_DB = paths.BACKTEST_LOG_DB


def has_execution_csv(symbol, execution_tf="1H"):
    """Is there local candle history for this symbol + execution timeframe?"""
    return os.path.exists(os.path.join(
        DATA_DIR, f"{symbol.lower()}_{execution_tf.lower()}.csv"))


def available_execution_tfs(symbol):
    """Execution timeframes with local CSVs, fastest first ('if present')."""
    return [tf for tf in ("15M", "30M", "1H", "4H", "1D")
            if has_execution_csv(symbol, tf)]


# Bars-per-hour equivalents, so wall-clock horizons survive a timeframe change.
_TF_MINUTES = {"15M": 15, "30M": 30, "1H": 60, "4H": 240, "1D": 1440}


def load_data(symbol=SYMBOL, execution_tf="1H"):
    """
    Load 1D + 4H (bias frames) plus the execution-timeframe candles.

    execution_tf may be '1H' (classic behaviour) or '15M'/'30M' when that CSV
    is present. Bar-count constants are rescaled by run_symbol so fill/timeout
    horizons keep their wall-clock meaning across timeframes.
    """
    def read(tf):
        path = os.path.join(DATA_DIR, f"{symbol.lower()}_{tf}.csv")
        return pd.read_csv(path, index_col=0, parse_dates=True)

    df_1d = read("1d")
    df_4h = read("4h")
    if execution_tf.upper() != "1H":
        return df_1d, df_4h, read(execution_tf.lower())
    return df_1d, df_4h, read("1h")


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


# Build #5: when True (default), resolutions run through
# trade_manager.advance â€” the SAME two-step management live uses (half off at
# TP1, breakeven ratchet, chandelier trail), so /analyze numbers and
# /live/stats are finally the same currency. Set BACKTEST_MANAGED_EXITS=0 for
# the legacy flat SL/TP walk (kept for A/B comparison).
MANAGED_EXITS = os.environ.get("BACKTEST_MANAGED_EXITS", "1") != "0"


def check_outcome_managed(df_1h, signal_idx_pos, direction, entry, sl, tp,
                          max_fill_wait=MAX_FILL_WAIT_BARS,
                          max_forward=CHECK_FORWARD_BARS):
    """
    check_outcome's contract, resolved by trade_manager.advance().

    Horizon parity with the flat walk: the manager only ever sees bars up to
    signal + max_fill_wait + max_forward, so 'open' at the cut maps to the
    same 'timeout' bucket. Fill semantics are identical (limit must be
    touched within max_fill_wait).

    Returns a dict:
        outcome   'win' | 'loss' | 'timeout' | 'no_fill'
        exit_price, bars_held, bars_to_fill   (None/0 where not applicable)
        realized_rr, pnl                     (managed combined-R, on win/loss)
        events                               (manager trail, e.g. TP1 fill)
    """
    n = len(df_1h)
    end_excl = min(n, signal_idx_pos + 1 + max_fill_wait - 1 + max_forward + 1)
    df_view = df_1h.iloc[:end_excl]

    trade = {
        "opened_at": df_1h.index[signal_idx_pos],
        "entry_price": entry,
        "stop_price": sl,
        "take_profit": tp,
        "direction": direction,
        "tp1_filled": False,
        "stop_current": None,
    }
    m = trade_manager.advance(trade, df_view, max_fill_wait=max_fill_wait)

    def _abs(ts):
        return int(df_view.index.get_loc(ts)) if ts is not None else None

    fill_abs = _abs(m.get("fill_ts"))
    bars_to_fill = (fill_abs - signal_idx_pos) if fill_abs is not None else None

    # Parity with the flat walk: an entry that never got touched is
    # 'no_fill' â€” including when the data ends before the full wait window
    # elapses. 'timeout' only ever applies AFTER a fill.
    if fill_abs is None:
        return {"outcome": "no_fill", "exit_price": None, "bars_held": 0,
                "bars_to_fill": None, "realized_rr": None, "pnl": None,
                "events": []}
    if m["outcome"] in ("pending", "open"):
        held = (_abs(m.get("resolved_ts")) or end_excl - 1) - fill_abs
        return {"outcome": "timeout", "exit_price": None, "bars_held": held,
                "bars_to_fill": bars_to_fill, "realized_rr": None,
                "pnl": None, "events": m["events"]}

    res_abs = _abs(m.get("resolved_ts"))
    return {
        "outcome": m["outcome"],
        "exit_price": m["exit_price"],
        "bars_held": (res_abs - fill_abs) if res_abs is not None and fill_abs is not None else 0,
        "bars_to_fill": bars_to_fill,
        "realized_rr": m["realized_rr"],
        "pnl": m["pnl"],
        "events": m["events"],
    }


def evaluate_outcome(df_exec, signal_idx_pos, direction, entry, sl, tp,
                     max_fill_wait=MAX_FILL_WAIT_BARS,
                     max_forward=CHECK_FORWARD_BARS):
    """Dispatch to managed or flat resolution; always returns one dict shape."""
    if MANAGED_EXITS:
        return check_outcome_managed(df_exec, signal_idx_pos, direction,
                                     entry, sl, tp,
                                     max_fill_wait=max_fill_wait,
                                     max_forward=max_forward)
    outcome, exit_price, bars_held, bars_to_fill = check_outcome(
        df_exec, signal_idx_pos, direction, entry, sl, tp,
        max_fill_wait=max_fill_wait, max_forward=max_forward)
    return {"outcome": outcome, "exit_price": exit_price,
            "bars_held": bars_held, "bars_to_fill": bars_to_fill,
            "realized_rr": None, "pnl": None, "events": []}


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
            "opposing_concurrent": 0, "rr_gross": 0.0, "rr_net": 0.0,
            "cost_r": 0.0, "skips": {}, "errors": {}}


def _blank_dirs():
    return {"LONG": {"win": 0, "loss": 0, "rr": 0.0, "rr_gross": 0.0},
            "SHORT": {"win": 0, "loss": 0, "rr": 0.0, "rr_gross": 0.0}}


def _align_tz(ts, index):
    """Coerce a user-supplied date to the CSV index's tz so comparisons work."""
    ts = pd.Timestamp(ts)
    tz = getattr(index, "tz", None)
    if tz is None:
        return ts.tz_localize(None) if ts.tzinfo is not None else ts
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert(tz)


def _get_candidate_outcome_metrics(df_exec, pos, direction, entry, sl, tp, fill_wait, horizon):
    """
    Calculate MFE_R, MAE_R and first-touch R-attainment flags for a window.
    Pessimistic: if SL (-1R) and a target are hit in same bar, it's a loss.
    Outcomes only count if the entry is touched within fill_wait bars after pos.
    MFE/MAE measured from the fill bar onward; accumulation stops at SL.
    """
    if not entry or not sl or entry == sl:
        return {"filled": 0, "mfe_r": None, "mae_r": None, "r1": None, "r15": None, "r2": None}
    
    highs = df_exec["high"].values
    lows = df_exec["low"].values
    n = len(df_exec)
    
    fill_i = None
    # Entry can happen from pos+1 onwards
    fill_end = min(pos + 1 + fill_wait, n)
    for i in range(pos + 1, fill_end):
        if direction == "LONG" and lows[i] <= entry:
            fill_i = i
            break
        if direction == "SHORT" and highs[i] >= entry:
            fill_i = i
            break
            
    if fill_i is None:
        return {"filled": 0, "mfe_r": None, "mae_r": None, "r1": None, "r15": None, "r2": None}

    # Outcome from fill_i
    r_val = abs(entry - sl)
    end = min(fill_i + horizon, n)
    mfe = 0.0
    mae = 0.0
    r1 = r15 = r2 = 0
    hit_neg1 = False
    
    for i in range(fill_i, end):
        h, l = highs[i], lows[i]
        
        if direction == "LONG":
            # Pessimistic: check SL first
            if l <= sl:
                mae = max(mae, entry - sl)
                hit_neg1 = True
            else:
                mfe = max(mfe, h - entry)
                mae = max(mae, entry - l)
                if h >= entry + 2 * r_val: r1 = r15 = r2 = 1
                elif h >= entry + 1.5 * r_val: r1 = r15 = 1
                elif h >= entry + r_val: r1 = 1
        else: # SHORT
            if h >= sl:
                mae = max(mae, sl - entry)
                hit_neg1 = True
            else:
                mfe = max(mfe, entry - l)
                mae = max(mae, h - entry)
                if l <= entry - 2 * r_val: r1 = r15 = r2 = 1
                elif l <= entry - 1.5 * r_val: r1 = r15 = 1
                elif l <= entry - r_val: r1 = 1
        
        if hit_neg1:
            break
            
    return {
        "filled": 1, "mfe_r": mfe / r_val, "mae_r": mae / r_val,
        "r1": r1, "r15": r15, "r2": r2
    }


def _log_candidate_outcome(symbol, ts, result, df_1d, df_4h, df_exec, pos, execution_tf):
    """
    Additive logger for every point that reached calculate_confluence.
    Gated by CANDIDATE_LOG=1.
    """
    if "confluence_breakdown" not in result:
        return

    # Common historical slicing up to decision bar ts / pos
    hist_1d = df_1d[df_1d.index <= ts]
    hist_4h = df_4h[df_4h.index <= ts]
    hist_exec = df_exec.iloc[:pos + 1]

    from tbb.bias_bridge import resolve_bias
    bias = resolve_bias({"1D": hist_1d, "4H": hist_4h, "1H": hist_exec})
    htf_aligned = bias.get("topdown", {}).get("aligned_count")

    timestamp_ts = pd.Timestamp(ts)
    hour_utc = int(timestamp_ts.hour)
    dow = int(timestamp_ts.dayofweek)

    close_close = float(hist_exec["close"].iloc[-1])
    import talib
    atr_arr = talib.ATR(
        hist_exec["high"].to_numpy(dtype=float),
        hist_exec["low"].to_numpy(dtype=float),
        hist_exec["close"].to_numpy(dtype=float),
        timeperiod=14
    )
    atr_pct = float(atr_arr[-1] / close_close * 100) if len(atr_arr) and not np.isnan(atr_arr[-1]) else None

    # 1. Recover entry/sl/tp/direction/touches if it was a skip (low conviction)
    if "skipped" not in result:
        direction = result["direction"]
        entry = result["entry"]
        sl = result["sl"]
        tp = result["tp"]
        score = result["confluence_score"]
        skip_reason = None
        touches = result.get("entry_level_touches")
    else:
        from tbb.pipeline import _prepare_candidate
        direction = bias.get("direction")
        if not direction:
            return
        d = str(direction).upper()
        direction = {"BULLISH": "LONG", "LONG": "LONG", "BUY": "LONG",
                     "BEARISH": "SHORT", "SHORT": "SHORT", "SELL": "SHORT"}.get(d)
        if direction is None:
            return
        recent = hist_exec.tail(LOOKBACK_BARS)
        prep = _prepare_candidate(
            recent["open"].to_numpy(dtype=float),
            recent["high"].to_numpy(dtype=float),
            recent["low"].to_numpy(dtype=float),
            recent["close"].to_numpy(dtype=float),
            recent["volume"].to_numpy(dtype=float),
            direction
        )
        if "trade" not in prep:
            return
        entry = prep["trade"]["entry"]
        sl = prep["trade"]["sl"]["sl_price"]
        tp = prep["trade"]["tp"]["tp_price"]
        score = round(sum(result["confluence_breakdown"].values()))
        skip_reason = result["skipped"]
        touches = prep.get("level_touches")

    entry_dist_pct = float(abs(entry - close_close) / close_close * 100) if close_close else None
    sl_dist_pct = float(abs(entry - sl) / entry * 100) if entry else None

    # 2. Outcomes for 24h and 100h horizons
    tf_min = _TF_MINUTES.get(execution_tf.upper(), 60)
    scale = 60.0 / tf_min
    fill_wait = int(24 * scale)
    res_24 = _get_candidate_outcome_metrics(df_exec, pos, direction, entry, sl, tp, fill_wait, int(24 * scale))
    res_100 = _get_candidate_outcome_metrics(df_exec, pos, direction, entry, sl, tp, fill_wait, int(100 * scale))
    
    filled = res_24["filled"]

    # 3. SQLite write
    db_path = os.path.join(DATA_DIR, "candidate_outcomes.db")
    try:
        conn = sqlite3.connect(db_path)
        comp_keys = [
            "pattern_match", "trend_align", "mtf_alignment", "sr_level_strength",
            "wick_rejection", "fib_score", "volume_confirm", "structure_bos_align",
            "liquidity_target", "no_mss_conflict", "chart_pattern_align",
            "retracement_confirm", "breakout_score", "elliott_score",
            "structure_retest_confirm", "session_timing"
        ]

        # Ensure table exists with all columns in exact order
        cols = ["symbol TEXT", "timestamp TEXT", "direction TEXT", "entry REAL",
                "stop REAL", "tp REAL", "confluence_score REAL"]
        cols += [f"{k} REAL" for k in comp_keys]
        cols += [
            "skip_reason TEXT", "filled INTEGER",
            "entry_dist_pct REAL", "sl_dist_pct REAL", "atr_pct REAL",
            "touches INTEGER", "htf_aligned INTEGER", "hour_utc INTEGER", "dow INTEGER",
            "mfe_24 REAL", "mae_24 REAL", "r1_24 INTEGER", "r15_24 INTEGER", "r2_24 INTEGER",
            "mfe_100 REAL", "mae_100 REAL", "r1_100 INTEGER", "r15_100 INTEGER", "r2_100 INTEGER"
        ]
        conn.execute(f"CREATE TABLE IF NOT EXISTS candidate_outcomes ({', '.join(cols)})")

        # Prepare values matching cols order
        row = [symbol, str(ts), direction, entry, sl, tp, score]
        bd = result["confluence_breakdown"]
        row += [bd.get(k, 0.0) for k in comp_keys]
        row.append(skip_reason)
        row.append(filled)
        row += [entry_dist_pct, sl_dist_pct, atr_pct, touches, htf_aligned, hour_utc, dow]
        
        for res in [res_24, res_100]:
            if filled:
                row += [res["mfe_r"], res["mae_r"], res["r1"], res["r15"], res["r2"]]
            else:
                row += [None] * 5

        placeholders = ",".join(["?"] * len(row))
        conn.execute(f"INSERT INTO candidate_outcomes VALUES ({placeholders})", row)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Candidate logging error: {e}", file=sys.stderr)


def run_symbol(symbol, journal, step=4, trade_id_start=0, verbose=True,
               start_date=None, end_date=None, execution_tf="1H",
               min_confluence_score=50, diag=None):
    """
    Backtest one symbol into a shared journal.

    execution_tf: '1H' (default), '30M' or '15M' â€” whichever local CSV exists
    (see has_execution_csv / available_execution_tfs). Wall-clock horizons are
    preserved by rescaling the bar-count constants: fill wait and forward
    horizon grow by 60/tf_minutes so 24 bars on 1H means the same 24 HOURS on
    15M. LOOKBACK_BARS stays a fixed 200 bars (a shorter history window on
    lower timeframes) and `step` remains bar-based, so lower timeframes are
    sampled more densely.

    Position state (open_positions, level_blocked_until) is local to this call,
    so concurrency limits apply PER SYMBOL â€” symbols are treated as independent
    accounts. That is deliberate for measuring whether an edge exists at all.
    It is NOT a realistic portfolio equity curve, which would need a shared
    capital pool and a cap on concurrent positions across all symbols
    (see RiskManager in risk_journal.py).

    Up to MAX_CONCURRENT_POSITIONS may be open at once, but never two on the
    same S/R zone: open_positions is keyed by level_key(), so a zone already
    held is refused rather than stacked.

    start_date/end_date (optional) restrict which test points are evaluated, for
    the on-demand /analyze endpoint. Left as None the bounds are exactly the
    original LOOKBACK_BARS .. n - CHECK_FORWARD_BARS range. Note that forward
    outcome resolution still reads past end_date â€” a trade opened near the end
    is allowed to resolve rather than being cut off mid-flight.

    Returns (counters, by_dir, next_trade_id).
    """
    df_1d, df_4h, df_exec = load_data(symbol, execution_tf)

    tf_min = _TF_MINUTES.get(execution_tf.upper(), 60)
    scale = 60.0 / tf_min
    fill_wait = int(round(MAX_FILL_WAIT_BARS * scale))
    forward = int(round(CHECK_FORWARD_BARS * scale))

    c = _blank_counters()
    by_dir = _blank_dirs()
    trade_id = trade_id_start
    n = len(df_exec)

    # level_key -> (exit_bar, direction) for every position or working order
    # currently occupying a slot.
    open_positions = {}
    level_blocked_until = {}

    lo, hi = LOOKBACK_BARS, n - forward
    if start_date is not None:
        lo = max(lo, int(df_exec.index.searchsorted(_align_tz(start_date, df_exec.index), side="left")))
    if end_date is not None:
        hi = min(hi, int(df_exec.index.searchsorted(_align_tz(end_date, df_exec.index), side="right")))

    points = range(lo, hi, step) if hi > lo else range(0)
    c["test_points"] = len(points)

    eval_count = 0
    for pos in points:
        eval_count += 1
        if eval_count % 100 == 0:
            print(f"{symbol} bar {pos}/{n} trades={c['trades']} skips_size={len(c['skips'])}", file=sys.stderr, flush=True)
        ts = df_exec.index[pos]

        hist_1d = slice_up_to(df_1d, ts)
        hist_4h = slice_up_to(df_4h, ts)
        hist_exec = df_exec.iloc[:pos + 1]  # up to and including current bar, no future

        # Counted, not silent: if the HTF CSVs start later than the execution
        # CSV, every early bar is unusable and would otherwise vanish without
        # a trace.
        if len(hist_1d) < 10 or len(hist_4h) < 10 or len(hist_exec) < LOOKBACK_BARS:
            c["insufficient_htf"] += 1
            continue

        # Additive diagnostics: resolve this bar's bias direction once and record
        # the baseline direction-quality row. Read-only; does not gate anything.
        if diag is not None:
            diag.begin_point(pos, hist_1d, hist_4h, hist_exec, df_exec)

        # Retire anything that has closed by this bar, then refuse only if every
        # slot is still occupied. Checked before analysis to skip pipeline cost.
        open_positions = {k: v for k, v in open_positions.items() if v[0] > pos}
        if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
            c["position_open"] += 1
            continue

        recent = hist_exec.tail(LOOKBACK_BARS)

        try:
            result = analyze_pair_with_bias(
                pair=symbol, timeframe=execution_tf,
                df_by_tf={"1D": hist_1d, "4H": hist_4h, "1H": hist_exec},
                opens=recent["open"].to_numpy(dtype=float),
                highs=recent["high"].to_numpy(dtype=float),
                lows=recent["low"].to_numpy(dtype=float),
                closes=recent["close"].to_numpy(dtype=float),
                volumes=recent["volume"].to_numpy(dtype=float),
                db_path=BACKTEST_DB,
                min_confluence_score=min_confluence_score,
            )
            if diag is not None and "skipped" not in result:
                diag.record_entry_result(result, pos)
            # Candidate-outcome logger (additive, gated)
            if os.environ.get("CANDIDATE_LOG") == "1":
                try:
                    _log_candidate_outcome(symbol, ts, result, df_1d, df_4h, df_exec, pos, execution_tf)
                except Exception as _e:
                    print(f"[candidate-log] {symbol} {ts} {type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
        except Exception as e:
            # Never swallow silently â€” a masked exception is indistinguishable
            # from "no signal found", which is what hid the real bug before.
            key = type(e).__name__
            c["errors"][key] = c["errors"].get(key, 0) + 1
            if c["errors"][key] <= 3:
                print(f"[{symbol} {ts}] ERROR {key}: {e}")
            continue

        if "skipped" in result:
            key = classify_skip(result["skipped"])
            c["skips"][key] = c["skips"].get(key, 0) + 1
            # Additive diagnostics only — records why the point was rejected.
            # The funnel counters above are untouched, so totals stay identical.
            # The context bundle is read-only references the S/R replica needs;
            # it is ignored by any other rejection bucket.
            if diag is not None:
                diag.record_skip(key, result, pos, context={
                    "recent": recent,
                    "hist_1d": hist_1d,
                    "hist_4h": hist_4h,
                    "hist_exec": hist_exec,
                    "pos": pos,
                })
            continue

        lk = level_key(result["entry"])
        # Distinct zones only: never stack a second position on a level already held.
        if lk in open_positions:
            c["zone_occupied"] += 1
            if diag is not None:
                diag.record_candidate("zone_occupied", pos, result["direction"])
            continue
        if pos < level_blocked_until.get(lk, -1):
            c["cooldown"] += 1
            if diag is not None:
                diag.record_candidate("cooldown", pos, result["direction"])
            continue

        # Diagnostic only, not a gate: how often we end up holding offsetting
        # exposure on one symbol (a LONG at support while a SHORT at resistance
        # is open). Legitimate range-trading, but worth knowing the frequency.
        if any(d != result["direction"] for _, d in open_positions.values()):
            c["opposing_concurrent"] += 1

        oc = evaluate_outcome(
            df_exec, pos, result["direction"], result["entry"], result["sl"], result["tp"],
            max_fill_wait=fill_wait, max_forward=forward
        )
        outcome, exit_price = oc["outcome"], oc["exit_price"]
        bars_held, bars_to_fill = oc["bars_held"], oc["bars_to_fill"]
        if outcome == "no_fill":
            c["no_fill"] += 1
            if diag is not None:
                diag.record_candidate("no_fill", pos, result["direction"])
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
            if diag is not None:
                diag.record_candidate("timeout", pos, result["direction"])
            continue

        # Filled and resolved (win/loss): the "accepted" direction-quality row.
        if diag is not None:
            diag.record_candidate("filled", pos, result["direction"])

        pnl = (exit_price - result["entry"]) if result["direction"] == "LONG" else (result["entry"] - exit_price)
        realized_rr = pnl / abs(result["entry"] - result["sl"]) if result["entry"] != result["sl"] else 0
        if oc["realized_rr"] is not None:
            # Managed resolution: combined-R comes from the manager (TP1 half
            # + trailed remainder); trust it over the flat re-derivation.
            realized_rr = oc["realized_rr"]
            pnl = oc["pnl"]

        # Trading frictions: accounting only (entry/exit levels, fill, SL/TP
        # untouched). realized_rr/pnl stored below are NET; gross kept for
        # before/after-cost reporting in c["rr_gross"]/by_dir gross buckets.
        realized_rr_gross = realized_rr
        realized_rr, pnl, cost_r = apply_trading_costs(
            result["entry"], exit_price, result["sl"], realized_rr_gross, pnl)
        # Costs can flip a marginal gross win into a net loss (and vice versa
        # never happens since costs are strictly >= 0): outcome follows NET.
        outcome = "win" if realized_rr > 0 else "loss"

        trade_id += 1
        c["trades"] += 1
        c["rr_gross"] += realized_rr_gross
        c["rr_net"] += realized_rr
        c["cost_r"] += cost_r
        d = by_dir[result["direction"]]
        d[outcome] += 1
        d["rr"] += realized_rr
        d["rr_gross"] += realized_rr_gross
        notes = f"score={result['confluence_score']} confidence={result['confidence']}"
        if oc["events"]:
            notes += " | " + "; ".join(oc["events"])
        journal.log_trade(
            trade_id=trade_id, timestamp=ts, pair=symbol, direction=result["direction"],
            entry_model="pipeline", htf_poi_tf="1D", poi_id=f"bt_{trade_id}",
            entry_price=result["entry"], stop_price=result["sl"], take_profit=result["tp"],
            stop_distance=abs(result["entry"] - result["sl"]), planned_rr=result["rr"],
            realized_rr=realized_rr, risk_pct_used=0.01, position_size=None,
            outcome=outcome, pnl=pnl, balance_after=None, notes=notes,
        )
        if verbose:
            print(f"[{symbol} {ts}] {result['direction']} entry={result['entry']:.4f} -> {outcome} "
                  f"(RR={realized_rr:.2f}, filled after {bars_to_fill}, held {bars_held} bars)")

    return c, by_dir, trade_id


def _merge(into, c):
    for k in ("insufficient_htf", "position_open", "cooldown", "no_fill",
              "timeouts", "trades", "test_points", "zone_occupied",
              "opposing_concurrent", "rr_gross", "rr_net", "cost_r"):
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
    t = c["trades"]
    if t:
        print(f"avg_rr gross (before costs): {c['rr_gross'] / t:+.3f}  "
              f"net (after {COST_BPS_PER_SIDE:.0f}bps/side+{SLIPPAGE_BPS:.0f}bps slip): "
              f"{c['rr_net'] / t:+.3f}  avg_cost: {c['cost_r'] / t:+.3f}R")

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
                  f"win_rate={s['win'] / t:.3f}  avg_rr_net={s['rr'] / t:+.3f}  "
                  f"avg_rr_gross={s.get('rr_gross', s['rr']) / t:+.3f}")


def run_multi(symbols=SYMBOLS, step=4, verbose=False):
    """Backtest every symbol into one shared journal, then report combined."""
    journal = TradeJournal()
    total = _blank_counters()
    all_dir = _blank_dirs()
    trade_id = 0
    done = []
    diags = []
    extra_diag = diagnostics_enabled()

    for sym in symbols:
        sym_diag = (RejectionDiagnostics(sym, EXECUTION_TF)
                    if extra_diag else None)
        try:
            c, by_dir, trade_id = run_symbol(sym, journal, step, trade_id, verbose,
                                             diag=sym_diag)
        except FileNotFoundError as e:
            print(f"{sym:10s} SKIPPED - missing data: {e}")
            continue
        if sym_diag is not None:
            diags.append((sym, sym_diag))
        _merge(total, c)
        for side in all_dir:
            for f in ("win", "loss", "rr", "rr_gross"):
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
    # Additive rejection diagnostics, printed per symbol directly under the funnel.
    for _sym, _diag in diags:
        _diag.print_report()
    print("\nNOTE: summary_stats() net_pnl/profit_factor are price-delta based and are")
    print("      NOT cross-symbol comparable. Use win_rate, avg_rr and profit_factor_R.")
    return journal, journal.summary_stats()


def run_backtest(step=4, symbol=SYMBOL):
    """Single-symbol backtest (original behaviour)."""
    journal = TradeJournal()
    diag = RejectionDiagnostics(symbol, EXECUTION_TF) if diagnostics_enabled() else None
    c, by_dir, _ = run_symbol(symbol, journal, step, 0, verbose=True, diag=diag)
    stats = journal.summary_stats()
    df = journal.to_dataframe()
    closed = df[df["outcome"].isin(["win", "loss"])]
    print_report(f"BACKTEST SUMMARY - {symbol}", c, by_dir, stats,
                 rr_pf=rr_profit_factor(closed) if not closed.empty else None)
    if diag is not None:
        diag.print_report()
    return journal, stats


if __name__ == "__main__":
    run_multi(step=4)
