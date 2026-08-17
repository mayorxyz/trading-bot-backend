"""
Step 2: Live candle ingestion from Bybit + TA-Lib pattern detection.
Maintains a rolling buffer of CLOSED candles. Each time a new candle closes,
runs TA-Lib candlestick pattern functions + a couple indicators on the buffer
and prints anything detected on the latest candle.
"""

import asyncio
import json
import numpy as np
import requests
import talib
import websockets

SYMBOL = "BTCUSDT"
INTERVAL = "1"  # 1-minute candles
STREAM_URL = "wss://stream.bybit.com/v5/public/spot"
REST_URL = "https://api.bybit.com/v5/market/kline"

MAX_BUFFER = 200  # keep last 200 closed candles for indicator lookback

# Rolling OHLCV buffers
opens, highs, lows, closes, volumes = [], [], [], [], []

# Pattern functions to check — name -> TA-Lib function
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
    "Spinning Top": talib.CDLSPINNINGTOP,
}


def analyze():
    """Run TA-Lib pattern + indicator functions on the current buffer."""
    o = np.array(opens, dtype=float)
    h = np.array(highs, dtype=float)
    l = np.array(lows, dtype=float)
    c = np.array(closes, dtype=float)

    found = []
    for name, func in PATTERNS.items():
        result = func(o, h, l, c)
        latest = result[-1]
        if latest != 0:
            direction = "Bullish" if latest > 0 else "Bearish"
            found.append(f"{direction} {name}")

    # A couple of indicators for context (need enough candles first)
    rsi_val = None
    atr_val = None
    if len(c) >= 15:
        rsi = talib.RSI(c, timeperiod=14)
        rsi_val = rsi[-1]
    if len(c) >= 15:
        atr = talib.ATR(h, l, c, timeperiod=14)
        atr_val = atr[-1]

    return found, rsi_val, atr_val


def prefill_history(limit=100):
    """Fetch recent closed candles via REST so analysis can start immediately."""
    params = {
        "category": "spot",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": limit,
    }
    resp = requests.get(REST_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # Bybit returns newest-first: [start, open, high, low, close, volume, turnover]
    rows = list(reversed(data["result"]["list"]))

    for row in rows:
        opens.append(float(row[1]))
        highs.append(float(row[2]))
        lows.append(float(row[3]))
        closes.append(float(row[4]))
        volumes.append(float(row[5]))

    print(f"Pre-filled {len(rows)} historical candles.\n")


async def listen():
    print(f"Connecting to {STREAM_URL} ...")
    async with websockets.connect(STREAM_URL) as ws:
        print("Connected.")

        subscribe_msg = {"op": "subscribe", "args": [f"kline.{INTERVAL}.{SYMBOL}"]}
        await ws.send(json.dumps(subscribe_msg))
        print(f"Subscribed to kline.{INTERVAL}.{SYMBOL}\n")

        async for message in ws:
            data = json.loads(message)
            if "topic" not in data:
                continue

            for k in data["data"]:
                is_closed = k["confirm"]
                if not is_closed:
                    continue  # only analyze fully closed candles

                o, h, l, c, v = (
                    float(k["open"]),
                    float(k["high"]),
                    float(k["low"]),
                    float(k["close"]),
                    float(k["volume"]),
                )

                opens.append(o)
                highs.append(h)
                lows.append(l)
                closes.append(c)
                volumes.append(v)

                # trim buffer
                if len(closes) > MAX_BUFFER:
                    opens.pop(0)
                    highs.pop(0)
                    lows.pop(0)
                    closes.pop(0)
                    volumes.pop(0)

                print(f"\n[CLOSED] {SYMBOL} O:{o} H:{h} L:{l} C:{c} V:{v}")

                if len(closes) < 5:
                    print(f"  (buffering... {len(closes)}/5 candles minimum)")
                    continue

                patterns, rsi_val, atr_val = analyze()

                if patterns:
                    print(f"  Patterns detected: {', '.join(patterns)}")
                else:
                    print("  No patterns detected")

                if rsi_val is not None:
                    print(f"  RSI(14): {rsi_val:.2f}")
                if atr_val is not None:
                    print(f"  ATR(14): {atr_val:.2f}")


if __name__ == "__main__":
    prefill_history(limit=100)

    # Run initial analysis immediately on the pre-filled history
    if len(closes) >= 5:
        patterns, rsi_val, atr_val = analyze()
        print("Initial analysis on most recent historical candle:")
        print(f"  Patterns detected: {', '.join(patterns) if patterns else 'None'}")
        if rsi_val is not None:
            print(f"  RSI(14): {rsi_val:.2f}")
        if atr_val is not None:
            print(f"  ATR(14): {atr_val:.2f}")
        print()

    asyncio.run(listen())