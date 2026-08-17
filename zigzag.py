"""
zigzag.py — ATR-based swing point detector.

Converts raw OHLCV candles into a clean sequence of swing highs/lows,
filtering out noise. This is the foundation for triangle/consolidation/
pattern-shape detection.

Usage:
    from zigzag import get_zigzag_swings

    swings = get_zigzag_swings(highs, lows, closes, atr_mult=1.5, atr_period=14)
    # swings -> list of dicts: {"index": int, "price": float, "type": "HIGH"/"LOW"}
"""

import numpy as np
import talib


def get_zigzag_swings(highs, lows, closes, atr_mult=1.5, atr_period=14):
    """
    Detect swing highs/lows using an ATR-scaled threshold.

    A new swing point is only confirmed once price reverses by
    >= atr_mult * ATR(atr_period) from the last confirmed swing.

    Args:
        highs, lows, closes: array-like of floats (same length), oldest -> newest
        atr_mult: ATR multiplier for reversal threshold (default 1.5)
        atr_period: ATR lookback period (default 14)

    Returns:
        List of swing points, oldest -> newest:
        [{"index": i, "price": p, "type": "HIGH" | "LOW"}, ...]
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    if len(closes) < atr_period + 2:
        return []

    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    swings = []

    # Bootstrap: start tracking from first bar with a valid ATR value
    start = atr_period
    direction = None          # "UP" or "DOWN" - direction we're currently tracking
    last_pivot_idx = start
    last_pivot_price = closes[start]
    running_extreme_idx = start
    running_extreme_price = closes[start]

    for i in range(start + 1, len(closes)):
        threshold = atr[i] * atr_mult
        if np.isnan(threshold) or threshold <= 0:
            continue

        high_i = highs[i]
        low_i = lows[i]

        if direction is None:
            # Not yet established a direction — check for first move
            if high_i - last_pivot_price >= threshold:
                direction = "UP"
                running_extreme_idx = i
                running_extreme_price = high_i
            elif last_pivot_price - low_i >= threshold:
                direction = "DOWN"
                running_extreme_idx = i
                running_extreme_price = low_i
            continue

        if direction == "UP":
            # Track new highs
            if high_i > running_extreme_price:
                running_extreme_idx = i
                running_extreme_price = high_i
            # Check for reversal down from the running high
            elif running_extreme_price - low_i >= threshold:
                # Confirm the HIGH swing at running_extreme
                swings.append({
                    "index": int(running_extreme_idx),
                    "price": float(running_extreme_price),
                    "type": "HIGH",
                })
                direction = "DOWN"
                last_pivot_idx = running_extreme_idx
                last_pivot_price = running_extreme_price
                running_extreme_idx = i
                running_extreme_price = low_i

        elif direction == "DOWN":
            # Track new lows
            if low_i < running_extreme_price:
                running_extreme_idx = i
                running_extreme_price = low_i
            # Check for reversal up from the running low
            elif high_i - running_extreme_price >= threshold:
                # Confirm the LOW swing at running_extreme
                swings.append({
                    "index": int(running_extreme_idx),
                    "price": float(running_extreme_price),
                    "type": "LOW",
                })
                direction = "UP"
                last_pivot_idx = running_extreme_idx
                last_pivot_price = running_extreme_price
                running_extreme_idx = i
                running_extreme_price = high_i

    return swings


def print_swings(swings, symbol="", timeframe=""):
    """Quick console printout for testing."""
    label = f"{symbol} {timeframe}".strip()
    print(f"\n--- Zigzag swings {label} ({len(swings)} found) ---")
    for s in swings:
        print(f"  idx={s['index']:>5}  {s['type']:<4}  price={s['price']:.5f}")


if __name__ == "__main__":
    # Quick self-test with synthetic data
    import random
    random.seed(42)
    n = 300
    price = 100.0
    closes, highs, lows = [], [], []
    for _ in range(n):
        move = random.uniform(-1.5, 1.5)
        price += move
        c = price
        h = c + random.uniform(0, 1)
        l = c - random.uniform(0, 1)
        closes.append(c)
        highs.append(h)
        lows.append(l)

    swings = get_zigzag_swings(highs, lows, closes, atr_mult=1.5, atr_period=14)
    print_swings(swings, symbol="TEST", timeframe="synthetic")
