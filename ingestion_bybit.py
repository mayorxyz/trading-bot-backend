"""
ingestion_bybit.py — one-time historical klines fetch via Bybit REST API.
Separate from ingestion.py (which handles live WS ticks).

Fetches 1W/1D/4H/1H/15M candles for a symbol, saves each as CSV in ./data/.
Bybit REST caps 1000 candles/request, so this paginates backward automatically.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
import os
import time

BYBIT_REST = "https://api.bybit.com/v5/market/kline"

INTERVAL_MAP = {
    "1W": "W", "1D": "D", "4H": "240", "1H": "60", "15M": "15",
}


def fetch_klines(symbol: str, interval_label: str, months_back: int = 6, category: str = "spot") -> pd.DataFrame:
    """
    Fetch historical klines, paginating backward until months_back is covered.
    Returns DataFrame: open, high, low, close, volume, indexed by UTC datetime, oldest->newest.
    """
    interval = INTERVAL_MAP[interval_label]
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=months_back * 30)).timestamp() * 1000)

    all_rows = []
    cursor_end = end_time

    while cursor_end > start_time:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "end": cursor_end,
            "limit": 1000,
        }
        resp = requests.get(BYBIT_REST, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")

        rows = data["result"]["list"]  # newest->oldest, each: [start, open, high, low, close, volume, turnover]
        if not rows:
            break

        all_rows.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_time:
            break
        cursor_end = oldest_ts - 1
        time.sleep(0.1)  # rate limit courtesy

    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"ts": "int64", "open": "float64", "high": "float64",
                     "low": "float64", "close": "float64", "volume": "float64"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").set_index("ts")
    df = df[df.index >= pd.Timestamp(start_time, unit="ms", tz="UTC")]
    return df[["open", "high", "low", "close", "volume"]]


def fetch_and_save_all(symbol: str = "BTCUSDT", months_back: int = 6,
                        timeframes: list = None, category: str = "spot",
                        out_dir: str = "./data") -> dict:
    """Fetch all timeframes, save CSVs, return dict of DataFrames."""
    timeframes = timeframes or ["1W", "1D", "4H", "1H", "15M"]
    os.makedirs(out_dir, exist_ok=True)

    result = {}
    for tf in timeframes:
        print(f"Fetching {symbol} {tf} ({months_back}mo)...")
        df = fetch_klines(symbol, tf, months_back, category)
        result[tf] = df
        path = os.path.join(out_dir, f"{symbol.lower()}_{tf.lower()}.csv")
        df.to_csv(path)
        print(f"  {len(df)} rows -> {path}")

    return result


if __name__ == "__main__":
    fetch_and_save_all(symbol="BTCUSDT", months_back=6, timeframes=["1D", "4H", "1H"])