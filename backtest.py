"""
backtest.py — replays historical OHLCV through the detection pipeline,
simulates entries/SL/TP using actual future candles (not just latest price),
logs results. This is what actually validates whether the strategy works.
"""

import numpy as np


def simulate_trade(entry_price, direction, sl_price, tp_price, future_highs, future_lows, max_bars=200):
    """
    Walks forward through future candles bar-by-bar to see which is hit first.
    This is the CORRECT way to check outcome (vs. just comparing to latest price).

    Returns: {"outcome": "WIN"/"LOSS"/"TIMEOUT", "bars_held": int, "exit_price": float}
    """
    for i in range(min(max_bars, len(future_highs))):
        h, l = future_highs[i], future_lows[i]

        if direction == "LONG":
            hit_sl = l <= sl_price
            hit_tp = h >= tp_price
        else:
            hit_sl = h >= sl_price
            hit_tp = l <= tp_price

        # if both hit same candle, assume worst case (SL first) — conservative
        if hit_sl and hit_tp:
            return {"outcome": "LOSS", "bars_held": i + 1, "exit_price": sl_price}
        elif hit_sl:
            return {"outcome": "LOSS", "bars_held": i + 1, "exit_price": sl_price}
        elif hit_tp:
            return {"outcome": "WIN", "bars_held": i + 1, "exit_price": tp_price}

    return {"outcome": "TIMEOUT", "bars_held": max_bars, "exit_price": None}


def run_backtest(candles: dict, signal_generator_fn, max_bars=200):
    """
    candles: {"open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]}
             all arrays same length, oldest -> newest
    signal_generator_fn: function(candles_slice, index) -> trade dict or None
        Must return None or:
        {"direction": "LONG"/"SHORT", "entry_price": float,
         "sl_price": float, "tp_price": float, "pattern": str (optional)}
        Called at each index using ONLY data up to that index (no lookahead).

    Returns: list of completed trade results with stats summary.
    """
    n = len(candles["close"])
    results = []

    for i in range(n - 1):
        # build a slice of candles up to (not including) i+1 — no lookahead
        sliced = {k: v[:i + 1] for k, v in candles.items()}
        signal = signal_generator_fn(sliced, i)
        if signal is None:
            continue

        future_highs = candles["high"][i + 1:i + 1 + max_bars]
        future_lows = candles["low"][i + 1:i + 1 + max_bars]
        if len(future_highs) == 0:
            continue

        sim = simulate_trade(
            signal["entry_price"], signal["direction"],
            signal["sl_price"], signal["tp_price"],
            future_highs, future_lows, max_bars
        )

        results.append({
            "index": i,
            "direction": signal["direction"],
            "pattern": signal.get("pattern"),
            "entry_price": signal["entry_price"],
            "sl_price": signal["sl_price"],
            "tp_price": signal["tp_price"],
            **sim,
        })

    return results


def summarize_backtest(results):
    total = len(results)
    if total == 0:
        return {"total": 0}
    wins = sum(1 for r in results if r["outcome"] == "WIN")
    losses = sum(1 for r in results if r["outcome"] == "LOSS")
    timeouts = sum(1 for r in results if r["outcome"] == "TIMEOUT")
    decided = wins + losses
    win_rate = round(100 * wins / decided, 1) if decided > 0 else 0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": win_rate,
        "avg_bars_held": round(np.mean([r["bars_held"] for r in results]), 1),
    }
