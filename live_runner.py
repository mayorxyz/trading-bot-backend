"""Live bybit signal runner for BTCUSDT — 1D/4H/1H/15M data, predicts on 1H and 15M close."""

import asyncio
import json
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import websockets

from ingestion_bybit import fetch_and_save_all
from pipeline import analyze_pair_with_bias

SYMBOL = "BTCUSDT"
STREAM_URL = "wss://stream.bybit.com/v5/public/spot"
TF_TOPIC = {
    "1D": "kline.D.BTCUSDT",
    "4H": "kline.240.BTCUSDT",
    "1H": "kline.60.BTCUSDT",
    "15M": "kline.15.BTCUSDT",
}
TRIGGER_TFS = {"1H", "15M"}  # analysis runs on close of these TFs
MAX_ROWS = 2000


def seed_history():
    tf_months = {"1D": 6, "4H": 2, "1H": 1, "15M": 0.25}  # ~1 week for 15M
    for tf, months in tf_months.items():
        fetch_and_save_all(symbol=SYMBOL, months_back=months, timeframes=[tf])

    df_by_tf = {}
    for tf in TF_TOPIC:
        path = Path("./data") / f"{SYMBOL.lower()}_{tf.lower()}.csv"
        if path.exists():
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if df.empty:
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        for c in ["open", "high", "low", "close", "volume"]:
            if c not in df.columns:
                df[c] = np.nan
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        if len(df) > MAX_ROWS:
            df = df.iloc[-MAX_ROWS:]
        df_by_tf[tf] = df
    return df_by_tf


def append_or_replace_row(df, candle):
    ts = pd.to_datetime(candle["start"], unit="ms", utc=True)
    row = {
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": float(candle["volume"]),
    }
    replacement = pd.DataFrame([row], index=[ts])
    df = pd.concat([df, replacement], axis=0)
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    if len(df) > MAX_ROWS:
        df = df.iloc[-MAX_ROWS:]
    return df


def run_analysis(df_by_tf, execution_tf):
    """Run pipeline using 1D/4H/1H for bias, execution_tf's data for entry/SL-TP."""
    recent = df_by_tf[execution_tf].tail(200)
    opens = recent["open"].to_numpy(dtype=float)
    highs = recent["high"].to_numpy(dtype=float)
    lows = recent["low"].to_numpy(dtype=float)
    closes = recent["close"].to_numpy(dtype=float)
    volumes = recent["volume"].to_numpy(dtype=float)

    # bias always from 1D/4H/1H (top-down); execution TF supplies the OHLCV for entry calc
    bias_tfs = {"1D": df_by_tf["1D"], "4H": df_by_tf["4H"], "1H": df_by_tf["1H"]}

    result = analyze_pair_with_bias(
        pair=SYMBOL,
        timeframe=execution_tf,
        df_by_tf=bias_tfs,
        opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes,
    )
    if "skipped" in result:
        print(f"[{execution_tf}] SKIPPED:", result["skipped"])
    else:
        print(f"[{execution_tf}] SIGNAL:", result)


async def run_live_stream(df_by_tf):
    topic_to_tf = {v: k for k, v in TF_TOPIC.items()}
    while True:
        try:
            async with websockets.connect(
                STREAM_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                subscribe_msg = {"op": "subscribe", "args": list(TF_TOPIC.values())}
                await ws.send(json.dumps(subscribe_msg))
                async for raw in ws:
                    data = json.loads(raw)
                    topic = data.get("topic")
                    if not topic:
                        continue
                    for candle in data.get("data", []):
                        if not candle.get("confirm"):
                            continue
                        tf = topic_to_tf.get(topic)
                        if tf is None:
                            continue
                        df_by_tf[tf] = append_or_replace_row(df_by_tf[tf], candle)

                        if tf in TRIGGER_TFS:
                            try:
                                run_analysis(df_by_tf, execution_tf=tf)
                            except Exception:
                                traceback.print_exc()
                                print("Analysis cycle failed but live loop is still running.")
        except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            print(f"WS connection dropped ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)


def main():
    df_by_tf = seed_history()
    asyncio.run(run_live_stream(df_by_tf))


if __name__ == "__main__":
    main()