"""
Extended multi-timeframe analysis.
Adds:
- Missing TA-Lib patterns (Dragonfly Doji, Gravestone Doji, Inverted Hammer, etc.)
- Custom Tweezer Top/Bottom detector (not in TA-Lib)
- Custom Inside Bar False Breakout detector (not in TA-Lib) — the "stop hunt" setup
- Fibonacci retracement levels (50%, 61.8%) for the current swing
"""

import numpy as np
import requests
import talib
get_zigzag_swings = None
try:
    from multi_pair_scanner import get_zigzag_swings
except ImportError:
    print("Warning: get_zigzag_swings not found. Please ensure multi_pair_scanner.py is in the same directory.")

REST_URL = "https://api.bybit.com/v5/market/kline"

TIMEFRAMES = {
    "Weekly": "W",
    "Daily": "D",
    "4h": "240",
    "1h": "60",
    "15m": "15",
}

PATTERNS = {
    "Engulfing": talib.CDLENGULFING,
    "Hammer": talib.CDLHAMMER,
    "Inverted Hammer": talib.CDLINVERTEDHAMMER,
    "Hanging Man": talib.CDLHANGINGMAN,
    "Doji": talib.CDLDOJI,
    "Dragonfly Doji": talib.CDLDRAGONFLYDOJI,
    "Gravestone Doji": talib.CDLGRAVESTONEDOJI,
    "Long-Legged Doji": talib.CDLLONGLEGGEDDOJI,
    "Morning Star": talib.CDLMORNINGSTAR,
    "Evening Star": talib.CDLEVENINGSTAR,
    "Shooting Star": talib.CDLSHOOTINGSTAR,
    "Three White Soldiers": talib.CDL3WHITESOLDIERS,
    "Three Black Crows": talib.CDL3BLACKCROWS,
    "Harami": talib.CDLHARAMI,
    "Piercing": talib.CDLPIERCING,
    "Dark Cloud Cover": talib.CDLDARKCLOUDCOVER,
    "Marubozu": talib.CDLMARUBOZU,
    "Spinning Top": talib.CDLSPINNINGTOP,
}


# ---------- Data fetch ----------

def fetch_candles(symbol, interval, limit=100):
    params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(REST_URL, params=params, timeout=10)
    resp.raise_for_status()
    result = resp.json().get("result", {})
    rows = list(reversed(result.get("list", [])))
    if len(rows) < 5:
        return None
    o = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    l = np.array([float(r[3]) for r in rows])
    c = np.array([float(r[4]) for r in rows])
    return o, h, l, c


# ---------- Custom pattern: Tweezer Top / Bottom ----------

def detect_tweezer(o, h, l, c, tolerance=0.001):
    """
    Tweezer Top: two candles, first bullish then bearish, with matching highs.
    Tweezer Bottom: two candles, first bearish then bullish, with matching lows.
    tolerance = relative difference allowed between the two highs/lows to count as 'matching'.
    Checks only the most recent 2 candles.
    """
    if len(c) < 2:
        return None

    o1, o2 = o[-2], o[-1]
    h1, h2 = h[-2], h[-1]
    l1, l2 = l[-2], l[-1]
    c1, c2 = c[-2], c[-1]

    bullish1 = c1 > o1
    bearish1 = c1 < o1
    bullish2 = c2 > o2
    bearish2 = c2 < o2

    # Tweezer Top: bullish candle then bearish candle, highs match
    if bullish1 and bearish2:
        if abs(h1 - h2) / max(h1, h2) <= tolerance:
            return "Bearish Tweezer Top"

    # Tweezer Bottom: bearish candle then bullish candle, lows match
    if bearish1 and bullish2:
        if abs(l1 - l2) / max(l1, l2) <= tolerance:
            return "Bullish Tweezer Bottom"

    return None


# ---------- Custom pattern: Inside Bar False Breakout ----------

def detect_inside_bar_false_breakout(o, h, l, c, lookback=10):
    """
    Inside Bar: a candle whose high/low is fully contained within the prior candle's (mother candle) range.
    False Breakout: price subsequently breaks outside the inside bar's range, then closes back inside the
    mother candle's range within the next few candles — signaling a stop-hunt / trap.

    Returns a description string if detected on the most recent candle, else None.
    Simplified single-pass version checking the last 3 candles: [mother, inside_bar, breakout_candle].
    """
    if len(c) < 3:
        return None

    mo, mh, ml, mc = o[-3], h[-3], l[-3], c[-3]   # mother candle
    io, ih, il, ic = o[-2], h[-2], l[-2], c[-2]   # inside bar
    bo, bh, bl, bc = o[-1], h[-1], l[-1], c[-1]   # latest candle (potential false breakout)

    is_inside = (ih <= mh) and (il >= ml)
    if not is_inside:
        return None

    # Bullish false breakout: price breaks below inside bar low, then closes back above it (trap for sellers)
    broke_below = bl < il
    closed_back_inside = bc > il and bc <= mh
    if broke_below and closed_back_inside and bc > bo:
        return "Bullish Inside Bar False Breakout (stop-hunt trap on sellers)"

    # Bearish false breakout: price breaks above inside bar high, then closes back below it (trap for buyers)
    broke_above = bh > ih
    closed_back_inside_bear = bc < ih and bc >= ml
    if broke_above and closed_back_inside_bear and bc < bo:
        return "Bearish Inside Bar False Breakout (stop-hunt trap on buyers)"

    return None


# ---------- Fibonacci retracement ----------

def fibonacci_levels(h, l, lookback=50):
    """
    Calculate Fibonacci retracement levels (50%, 61.8%) based on the most recent
    swing high and swing low within the lookback window.
    """
    recent_h = h[-lookback:] if len(h) >= lookback else h
    recent_l = l[-lookback:] if len(l) >= lookback else l

    swing_high = np.max(recent_h)
    swing_low = np.min(recent_l)
    diff = swing_high - swing_low

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "fib_50": swing_high - 0.5 * diff,
        "fib_618": swing_high - 0.618 * diff,
    }


def price_near_level(price, level, tolerance_pct=0.5):
    """Check if price is within tolerance_pct% of a given level."""
    if level == 0:
        return False
    return abs(price - level) / level * 100 <= tolerance_pct


# ---------- Trend ----------

def trend_direction(closes, lookback=20):
    if len(closes) < lookback:
        return "N/A"
    sma = talib.SMA(closes, timeperiod=lookback)
    latest_close = closes[-1]
    latest_sma = sma[-1]
    if np.isnan(latest_sma):
        return "N/A"
    if latest_close > latest_sma:
        return "Uptrend"
    elif latest_close < latest_sma:
        return "Downtrend"
    return "Flat"


# ---------- Combined analysis ----------

def analyze_timeframe(o, h, l, c):
    found = []
    for name, func in PATTERNS.items():
        result = func(o, h, l, c)
        latest = result[-1]
        if latest != 0:
            direction = "Bullish" if latest > 0 else "Bearish"
            found.append(f"{direction} {name}")

    tweezer = detect_tweezer(o, h, l, c)
    if tweezer:
        found.append(tweezer)

    false_breakout = detect_inside_bar_false_breakout(o, h, l, c)
    if false_breakout:
        found.append(false_breakout)

    rsi_val = None
    if len(c) >= 15:
        rsi_val = talib.RSI(c, timeperiod=14)[-1]

    trend = trend_direction(c)
    fib = fibonacci_levels(h, l)

    latest_close = c[-1]
    near_fib_50 = price_near_level(latest_close, fib["fib_50"])
    near_fib_618 = price_near_level(latest_close, fib["fib_618"])

    bull_count = sum(1 for p in found if p.startswith("Bullish"))
    bear_count = sum(1 for p in found if p.startswith("Bearish"))

    if trend == "Uptrend":
        bias = "Bullish"
    elif trend == "Downtrend":
        bias = "Bearish"
    else:
        if bull_count > bear_count:
            bias = "Bullish"
        elif bear_count > bull_count:
            bias = "Bearish"
        else:
            bias = "Neutral"

    return {
        "patterns": found,
        "rsi": rsi_val,
        "trend": trend,
        "bias": bias,
        "fib": fib,
        "near_fib_50": near_fib_50,
        "near_fib_618": near_fib_618,
    }


def analyze_symbol(symbol, verbose=True):
    results = {}
    for label, interval in TIMEFRAMES.items():
        candles = fetch_candles(symbol, interval)
        if candles is None:
            results[label] = {"error": "insufficient data"}
            continue
        o, h, l, c = candles
        results[label] = analyze_timeframe(o, h, l, c)

    if verbose:
        print(f"\n=== {symbol} ===")
        for label in TIMEFRAMES:
            r = results[label]
            if "error" in r:
                print(f"  {label}: {r['error']}")
                continue

            fib_note = ""
            if r["near_fib_50"]:
                fib_note = " [near 50% Fib]"
            elif r["near_fib_618"]:
                fib_note = " [near 61.8% Fib]"

            rsi_str = f"{r['rsi']:.1f}" if r["rsi"] is not None else "N/A"
            print(f"  {label}: {r['bias']} (trend={r['trend']}, rsi={rsi_str}){fib_note}")
            if r["patterns"]:
                print(f"      Patterns: {', '.join(r['patterns'])}")

    biases = [results[l]["bias"] for l in TIMEFRAMES if "bias" in results[l]]
    bull_tf = [l for l in TIMEFRAMES if results[l].get("bias") == "Bullish"]
    bear_tf = [l for l in TIMEFRAMES if results[l].get("bias") == "Bearish"]

    if biases and len(bull_tf) == len(biases):
        alignment = "FULL BULLISH ALIGNMENT"
    elif biases and len(bear_tf) == len(biases):
        alignment = "FULL BEARISH ALIGNMENT"
    else:
        alignment = "MIXED"

    if verbose:
        print(f"  → {alignment}")

    return results, alignment


if __name__ == "__main__":
    my_pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    for pair in my_pairs:
        analyze_symbol(pair)