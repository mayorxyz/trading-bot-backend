"""
Full candlestick pattern detection — all 61 TA-Lib CDL* functions.
Drop-in replacement/extension for the 13-pattern subset currently in use.

Usage:
    df = pd.DataFrame(...)  # needs columns: open, high, low, close
    results = detect_all_patterns(df)
    # results: dict of {pattern_name: pd.Series} where nonzero = signal
    #   +100 = strong bullish, +/-100 varies by function (some are +/-100 only)
    active = get_active_patterns(df)  # last-bar signals only, name: value
"""

import talib
import pandas as pd
import numpy as np

# All 61 pattern recognition functions, auto-discovered from TA-Lib
PATTERN_FUNCTIONS = talib.get_function_groups()['Pattern Recognition']  # 61 names


def detect_all_patterns(df: pd.DataFrame) -> dict:
    """
    Run every TA-Lib candlestick pattern function against OHLC data.
    df must have columns: open, high, low, close (case-insensitive).
    Returns dict {pattern_name: pd.Series of ints (0 = no signal)}.
    """
    cols = {c.lower(): c for c in df.columns}
    o = df[cols['open']].values.astype(float)
    h = df[cols['high']].values.astype(float)
    l = df[cols['low']].values.astype(float)
    c = df[cols['close']].values.astype(float)

    results = {}
    for name in PATTERN_FUNCTIONS:
        func = getattr(talib, name)
        try:
            out = func(o, h, l, c)
            results[name] = pd.Series(out, index=df.index, name=name)
        except Exception as e:
            # e.g. CDLMATHOLD/CDLRISEFALL3METHODS need penetration param on some builds
            results[name] = pd.Series(np.zeros(len(df)), index=df.index, name=name)
    return results


def get_active_patterns(df: pd.DataFrame, bar_index: int = -1) -> dict:
    """
    Return only patterns firing (nonzero) at a given bar (default: latest/last bar).
    Returns {pattern_name: signal_value}. Positive = bullish, negative = bearish
    (per TA-Lib convention; magnitude varies 100/-100/200/-200 by pattern).
    """
    all_patterns = detect_all_patterns(df)
    active = {}
    for name, series in all_patterns.items():
        val = series.iloc[bar_index]
        if val != 0:
            active[name] = int(val)
    return active


def summarize_bias(active_patterns: dict) -> dict:
    """
    Quick bullish/bearish tally from a get_active_patterns() result.
    """
    bullish = [k for k, v in active_patterns.items() if v > 0]
    bearish = [k for k, v in active_patterns.items() if v < 0]
    return {
        "bullish_count": len(bullish),
        "bearish_count": len(bearish),
        "bullish_patterns": bullish,
        "bearish_patterns": bearish,
        "net_bias": "bullish" if len(bullish) > len(bearish)
                    else "bearish" if len(bearish) > len(bullish)
                    else "neutral",
    }


if __name__ == "__main__":
    # Smoke test with synthetic data
    import numpy as np
    np.random.seed(0)
    n = 200
    close = 100 + np.cumsum(np.random.randn(n))
    open_ = close + np.random.randn(n) * 0.5
    high = np.maximum(open_, close) + np.abs(np.random.randn(n))
    low = np.minimum(open_, close) - np.abs(np.random.randn(n))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})

    print(f"Loaded {len(PATTERN_FUNCTIONS)} pattern functions")
    active = get_active_patterns(df)
    print(f"Active on last synthetic bar: {active}")
    print(summarize_bias(active))
