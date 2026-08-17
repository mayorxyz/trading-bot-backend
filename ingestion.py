"""
Step 1: Live candle ingestion from Bybit (crypto) — alternative to Binance
if Binance is geo-blocked. Connects via WebSocket, subscribes to one pair,
prints each candle as it updates/closes.
"""

import asyncio
import json
import websockets

SYMBOL = "BTCUSDT"
INTERVAL = "1"          # Bybit uses minutes as plain numbers: "1", "5", "60", "D"

STREAM_URL = "wss://stream.bybit.com/v5/public/spot"


async def listen():
    print(f"Connecting to {STREAM_URL} ...")
    async with websockets.connect(STREAM_URL) as ws:
        print("Connected.")

        subscribe_msg = {
            "op": "subscribe",
            "args": [f"kline.{INTERVAL}.{SYMBOL}"]
        }
        await ws.send(json.dumps(subscribe_msg))
        print(f"Subscribed to kline.{INTERVAL}.{SYMBOL}\n")

        async for message in ws:
            data = json.loads(message)

            if "topic" not in data:
                # subscription confirmation / ping-pong messages
                continue

            for k in data["data"]:
                candle = {
                    "symbol": SYMBOL,
                    "open_time": k["start"],
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": float(k["volume"]),
                    "is_closed": k["confirm"],  # True when candle has closed
                }

                status = "CLOSED" if candle["is_closed"] else "live"
                print(
                    f"[{status}] {candle['symbol']} "
                    f"O:{candle['open']} H:{candle['high']} "
                    f"L:{candle['low']} C:{candle['close']} "
                    f"V:{candle['volume']}"
                )


if __name__ == "__main__":
    asyncio.run(listen())