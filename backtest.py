"""
backtest.py — walks historical data bar-by-bar, no lookahead, runs pipeline
at each step, tracks SL/TP outcomes via TradeJournal.

Usage: python backtest.py
"""

import pandas as pd
import numpy as np
from pipeline import analyze_pair_with_bias
from phase4_risk_journal import TradeJournal

# TODO: Ensure this import matches your project structure
# If detect_all_patterns is in patterns.py, use: from patterns import detect_all_patterns
try:
    from patterns import detect_all_patterns
except ImportError:
    # Fallback if you haven't created patterns.py yet or it's named differently
    def detect_all_patterns(df):
        return {} 

SYMBOL = "BTCUSDT"
EXECUTION_TF = "1H"
LOOKBACK_BARS = 200      # min bars needed before we start testing
CHECK_FORWARD_BARS = 100  # max bars to look ahead for SL/TP hit


def load_data():
    df_1d = pd.read_csv("./data/btcusdt_1d.csv", index_col=0, parse_dates=True)
    df_4h = pd.read_csv("./data/btcusdt_4h.csv", index_col=0, parse_dates=True)
    df_1h = pd.read_csv("./data/btcusdt_1h.csv", index_col=0, parse_dates=True)
    return df_1d, df_4h, df_1h


def slice_up_to(df, ts):
    """No-lookahead: only bars up to and including ts."""
    return df[df.index <= ts]


def check_outcome(df_1h, entry_idx_pos, direction, sl, tp, max_forward=CHECK_FORWARD_BARS):
    """
    Walk forward from entry_idx_pos, check which hits first: SL or TP.
    Returns ('win'/'loss'/'timeout', exit_price, bars_held)
    """
    highs = df_1h["high"].values
    lows = df_1h["low"].values
    n = len(df_1h)
    end = min(entry_idx_pos + max_forward, n)

    for i in range(entry_idx_pos + 1, end):
        h, l = highs[i], lows[i]
        if direction == "LONG":
            if l <= sl:
                return "loss", sl, i - entry_idx_pos
            if h >= tp:
                return "win", tp, i - entry_idx_pos
        else:  # SHORT
            if h >= sl:
                return "loss", sl, i - entry_idx_pos
            if l <= tp:
                return "win", tp, i - entry_idx_pos

    return "timeout", None, end - entry_idx_pos


def run_backtest(step=4):
    """step: bars to skip between test points (4 = check every 4th 1H bar, faster)"""
    df_1d, df_4h, df_1h = load_data()
    journal = TradeJournal()

    trade_id = 0
    n = len(df_1h)

    for pos in range(LOOKBACK_BARS, n - CHECK_FORWARD_BARS, step):
        ts = df_1h.index[pos]

        hist_1d = slice_up_to(df_1d, ts)
        hist_4h = slice_up_to(df_4h, ts)
        hist_1h = df_1h.iloc[:pos + 1]  # up to and including current bar, no future

        if len(hist_1d) < 10 or len(hist_4h) < 10 or len(hist_1h) < LOOKBACK_BARS:
            continue

        recent = hist_1h.tail(LOOKBACK_BARS)
        
        # --- DIAGNOSTIC START ---
        exec_tf_df = hist_1h # Use full history for pattern detection context if needed, or recent
        raw = detect_all_patterns(exec_tf_df)
        nonzero = {k: v.iloc[-1] for k, v in raw.items() if v.iloc[-1] != 0}
        if pos % 40 == 0: # Print occasionally to avoid spamming console
            print(f"DEBUG RAW NONZERO at {ts}: {nonzero}")
            print(exec_tf_df[['open','high','low','close']].dtypes)
            print(exec_tf_df[['open','high','low','close']].tail(10))
            print(exec_tf_df.isna().sum())
        # --- DIAGNOSTIC END ---

        try:
            result = analyze_pair_with_bias(
                pair=SYMBOL, timeframe=EXECUTION_TF,
                df_by_tf={"1D": hist_1d, "4H": hist_4h, "1H": hist_1h},
                opens=recent["open"].to_numpy(dtype=float),
                highs=recent["high"].to_numpy(dtype=float),
                lows=recent["low"].to_numpy(dtype=float),
                closes=recent["close"].to_numpy(dtype=float),
                volumes=recent["volume"].to_numpy(dtype=float),
                db_path="/tmp/backtest_signals.db",
            )
        except Exception as e:
            continue

        if "skipped" in result:
            if trade_id == 0 and pos % 40 == 0:
                print(f"[{ts}] skip: {result['skipped'][:100]}")
            if "regime=consolidation" in result["skipped"] and "'tradable': True" in result["skipped"]:
                run_backtest.regime_blocked = getattr(run_backtest, "regime_blocked", 0) + 1
            else:
                run_backtest.topdown_blocked = getattr(run_backtest, "topdown_blocked", 0) + 1
            continue
             
        outcome, exit_price, bars_held = check_outcome(
            df_1h, pos, result["direction"], result["sl"], result["tp"]
        )
        if outcome == "timeout":
            continue

        pnl = (exit_price - result["entry"]) if result["direction"] == "LONG" else (result["entry"] - exit_price)
        realized_rr = pnl / abs(result["entry"] - result["sl"]) if result["entry"] != result["sl"] else 0

        trade_id += 1
        journal.log_trade(
            trade_id=trade_id, timestamp=ts, pair=SYMBOL, direction=result["direction"],
            entry_model="pipeline", htf_poi_tf="1D", poi_id=f"bt_{trade_id}",
            entry_price=result["entry"], stop_price=result["sl"], take_profit=result["tp"],
            stop_distance=abs(result["entry"] - result["sl"]), planned_rr=result["rr"],
            realized_rr=realized_rr, risk_pct_used=0.01, position_size=None,
            outcome=outcome, pnl=pnl, balance_after=None, notes=f"confidence={result['confidence']}",
        )
        print(f"[{ts}] {result['direction']} entry={result['entry']:.2f} -> {outcome} "
              f"(RR={realized_rr:.2f}, held {bars_held} bars)")

    stats = journal.summary_stats()
    print("\n=== BACKTEST SUMMARY ===")
    print(stats)
    print(f"regime-blocked (aligned but chop): {getattr(run_backtest, 'regime_blocked', 0)}")
    print(f"topdown-blocked (bias not aligned): {getattr(run_backtest, 'topdown_blocked', 0)}")
    return journal, stats


if __name__ == "__main__":
    run_backtest(step=4)