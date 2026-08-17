"""
Multi-timeframe confluence analysis.
Pulls recent candles for Weekly, Daily, 4h, 1h, 15m on one pair,
runs TA-Lib pattern + trend + RSI analysis on each, and prints a
compiled comparison so you can see alignment (or disagreement)
across timeframes before making a decision.
"""

import numpy as np
import requests
import talib

SYMBOL = "BTCUSDT"
REST_URL = "https://api.bybit.com/v5/market/kline"

# Bybit interval codes: 1,3,5,15,30,60,120,240,360,720,D,W,M
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
    "Hanging Man": talib.CDLHANGINGMAN,
    "Doji": talib.CDLDOJI,
    "Morning Star": talib.CDLMORNINGSTAR,
    "Evening Star": talib.CDLEVENINGSTAR,
    "Shooting Star": talib.CDLSHOOTINGSTAR,
    "Three White Soldiers": talib.CDL3WHITESOLDIERS,
    "Three Black Crows": talib.CDL3BLACKCROWS,
    "Harami": talib.CDLHARAMI,
    "Piercing": talib.CDLPIERCING,
    "Dark Cloud Cover": talib.CDLDARKCLOUDCOVER,
    "Marubozu": talib.CDLMARUBOZU,
}


def fetch_candles(interval, limit=100):
    params = {"category": "spot", "symbol": SYMBOL, "interval": interval, "limit": limit}
    resp = requests.get(REST_URL, params=params, timeout=10)
    resp.raise_for_status()
    rows = list(reversed(resp.json()["result"]["list"]))

    o = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    l = np.array([float(r[3]) for r in rows])
    c = np.array([float(r[4]) for r in rows])
    return o, h, l, c


def trend_direction(closes, lookback=20):
    """Simple trend check: compare recent close to SMA of lookback period."""
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


def analyze_timeframe(o, h, l, c):
    found = []
    for name, func in PATTERNS.items():
        result = func(o, h, l, c)
        latest = result[-1]
        if latest != 0:
            direction = "Bullish" if latest > 0 else "Bearish"
            found.append(f"{direction} {name}")

    rsi_val = None
    if len(c) >= 15:
        rsi = talib.RSI(c, timeperiod=14)
        rsi_val = rsi[-1]

    trend = trend_direction(c)

    # Overall bias for this timeframe: majority of pattern signals + trend
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
    }


def run():
    print(f"Multi-timeframe analysis for {SYMBOL}\n")
    results = {}

    for label, interval in TIMEFRAMES.items():
        try:
            o, h, l, c = fetch_candles(interval)
            results[label] = analyze_timeframe(o, h, l, c)
        except Exception as e:
            results[label] = {"error": str(e)}

    # Print per-timeframe breakdown
    for label in TIMEFRAMES:
        r = results[label]
        print(f"--- {label} ---")
        if "error" in r:
            print(f"  Error: {r['error']}\n")
            continue
        print(f"  Trend: {r['trend']}")
        print(f"  RSI(14): {r['rsi']:.2f}" if r["rsi"] is not None else "  RSI(14): N/A")
        print(f"  Patterns: {', '.join(r['patterns']) if r['patterns'] else 'None'}")
        print(f"  Bias: {r['bias']}\n")

    # Compiled confluence summary
    biases = [results[label]["bias"] for label in TIMEFRAMES if "bias" in results[label]]
    bull_tf = [label for label in TIMEFRAMES if results[label].get("bias") == "Bullish"]
    bear_tf = [label for label in TIMEFRAMES if results[label].get("bias") == "Bearish"]

    print("=== Confluence Summary ===")
    print(f"Bullish on: {', '.join(bull_tf) if bull_tf else 'None'}")
    print(f"Bearish on: {', '.join(bear_tf) if bear_tf else 'None'}")

    if len(bull_tf) == len(biases):
        print("→ FULL ALIGNMENT: Bullish across all timeframes")
    elif len(bear_tf) == len(biases):
        print("→ FULL ALIGNMENT: Bearish across all timeframes")
    else:
        print("→ MIXED: timeframes disagree — lower confidence / wait for alignment")


if __name__ == "__main__":
    run()