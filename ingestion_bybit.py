"""
ingestion_bybit.py — historical klines + instrument metadata via Bybit REST API.
Separate from ingestion.py (which handles live WS ticks).

fetch_and_save_all() writes CSVs into ./data/ and is the EXPLICIT, user-invoked
historical path (its CLI, and whatever POST /analyze needs on disk). The live
path never calls it — live_runner seeds straight into memory via fetch_klines,
and api.py's chart path uses fetch_last_n_klines. Nothing here writes to disk
unless fetch_and_save_all is called by name.

Timeframe labels follow the repo-wide {symbol}_{tf}.csv convention, where a
trailing M means MINUTES (15M = 15 minutes, matching the existing
btcusdt_15m.csv). Monthly is "1MO" — see INTERVAL_MAP.

Bybit REST caps 1000 candles/request, so both fetch_klines (by time window) and
fetch_last_n_klines (by bar count) paginate backward automatically.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
import os
import time

BYBIT_REST = "https://api.bybit.com/v5/market/kline"
BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"

MAX_KLINES_PER_REQUEST = 1000   # Bybit's hard cap

# Transient connection resets and read timeouts are routine when a chart load
# fans out across many symbol/timeframe pairs. Retrying them here keeps a blip
# from surfacing as a failed chart; 4xx and Bybit retCode errors are real answers
# and are never retried.
RETRY_ATTEMPTS = int(os.environ.get("BYBIT_RETRY_ATTEMPTS", 3))
RETRY_BACKOFF = float(os.environ.get("BYBIT_RETRY_BACKOFF", 0.4))

# Every interval Bybit v5 klines offers. Labels follow the repo-wide
# {symbol}_{tf}.csv convention, where a trailing M means MINUTES (15M = 15
# minutes, matching the existing btcusdt_15m.csv).
#
# MONTHLY is therefore "1MO", not "1M" — "1M" is already one minute here, and
# silently reusing it would make a monthly chart request return minute bars. This
# is the one label that does not match Bybit's own naming (Bybit calls it "M").
INTERVAL_MAP = {
    "1M": "1", "3M": "3", "5M": "5", "15M": "15", "30M": "30",
    "1H": "60", "2H": "120", "4H": "240", "6H": "360", "12H": "720",
    "1D": "D", "1W": "W", "1MO": "M",
}

# Approximate bar duration, used to size a backward-paginating fetch window and
# to order timeframes for display. Month is nominal (30d) — only the ordering and
# the fetch window depend on it, never a timestamp.
TF_MINUTES = {
    "1M": 1, "3M": 3, "5M": 5, "15M": 15, "30M": 30,
    "1H": 60, "2H": 120, "4H": 240, "6H": 360, "12H": 720,
    "1D": 24 * 60, "1W": 7 * 24 * 60, "1MO": 30 * 24 * 60,
}


class BybitError(RuntimeError):
    """A non-zero retCode from Bybit. retCode 10001 = unknown/unsupported symbol."""

    def __init__(self, ret_code, ret_msg):
        super().__init__(f"Bybit API error {ret_code}: {ret_msg}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg


def _get(url: str, params: dict, timeout: float = 15) -> dict:
    """
    GET + retCode check, retrying only transient transport failures.

    A BybitError (non-zero retCode) and any 4xx are definitive answers — they are
    raised immediately. Connection resets, read timeouts and 5xx get
    RETRY_ATTEMPTS tries with exponential backoff.
    """
    last_exc = None
    for attempt in range(max(1, RETRY_ATTEMPTS)):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("retCode") != 0:
                raise BybitError(data.get("retCode"), data.get("retMsg"))
            return data["result"]
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is None or status < 500:
                raise
            last_exc = exc

        if attempt < max(1, RETRY_ATTEMPTS) - 1:
            time.sleep(RETRY_BACKOFF * (2 ** attempt))

    raise last_exc


def _rows_to_frame(rows: list) -> pd.DataFrame:
    """
    Bybit kline rows -> OHLCV DataFrame indexed by UTC datetime, oldest->newest.

    Each row is [start, open, high, low, close, volume, turnover] with every
    field a string.
    """
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"ts": "int64", "open": "float64", "high": "float64",
                     "low": "float64", "close": "float64", "volume": "float64"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").set_index("ts")
    return df[["open", "high", "low", "close", "volume"]]


def _interval(interval_label: str) -> str:
    try:
        return INTERVAL_MAP[interval_label.upper()]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {interval_label!r}; "
            f"expected one of {sorted(INTERVAL_MAP)}"
        ) from None


def fetch_klines(symbol: str, interval_label: str, months_back: int = 6,
                 category: str = "spot", days_back: float = None) -> pd.DataFrame:
    """
    Fetch historical klines, paginating backward until the window is covered.
    Returns DataFrame: open, high, low, close, volume, indexed by UTC datetime, oldest->newest.

    Pass days_back to override months_back — useful for the sub-hourly
    timeframes, where 6 months of 1M candles is ~260k bars.
    """
    interval = _interval(interval_label)
    span_days = days_back if days_back is not None else months_back * 30
    now = datetime.now(timezone.utc)
    end_time = int(now.timestamp() * 1000)
    start_time = int((now - timedelta(days=span_days)).timestamp() * 1000)

    all_rows = []
    cursor_end = end_time

    while cursor_end > start_time:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "end": cursor_end,
            "limit": MAX_KLINES_PER_REQUEST,
        }
        rows = _get(BYBIT_REST, params)["list"]  # newest->oldest
        if not rows:
            break

        all_rows.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_time:
            break
        cursor_end = oldest_ts - 1
        time.sleep(0.1)  # rate limit courtesy

    df = _rows_to_frame(all_rows)
    if df.empty:
        return df
    return df[df.index >= pd.Timestamp(start_time, unit="ms", tz="UTC")]


def fetch_recent_klines(symbol: str, interval_label: str, limit: int = 200,
                        category: str = "spot", end: int = None) -> pd.DataFrame:
    """
    Single-request fetch of the most recent `limit` candles (no pagination).

    This is the chart-only path: cheap enough to serve on demand for any symbol,
    unlike fetch_klines which walks months of history. limit is clamped to
    Bybit's 1000-per-request cap. Pass `end` (epoch ms) to fetch the window
    ending there instead of now — that is how fetch_last_n_klines pages back.
    """
    interval = _interval(interval_label)
    params = {
        "category": category,
        "symbol": symbol,
        "interval": interval,
        "limit": max(1, min(int(limit), MAX_KLINES_PER_REQUEST)),
    }
    if end is not None:
        params["end"] = int(end)
    return _rows_to_frame(_get(BYBIT_REST, params)["list"])


def fetch_last_n_klines(symbol: str, interval_label: str, limit: int,
                        category: str = "spot", pause: float = 0.1) -> pd.DataFrame:
    """
    Most recent `limit` candles, paginating backward past Bybit's 1000-per-request
    cap until `limit` is reached or the symbol's history runs out.

    Used for full-depth live charts. One request when limit <= 1000, so the
    common case costs exactly what fetch_recent_klines does.
    """
    limit = max(1, int(limit))
    interval = _interval(interval_label)
    all_rows, cursor_end, remaining = [], None, limit

    while remaining > 0:
        want = min(remaining, MAX_KLINES_PER_REQUEST)
        params = {"category": category, "symbol": symbol,
                  "interval": interval, "limit": want}
        if cursor_end is not None:
            params["end"] = cursor_end
        rows = _get(BYBIT_REST, params)["list"]   # newest -> oldest
        if not rows:
            break

        all_rows.extend(rows)
        remaining -= len(rows)
        # A short page means Bybit has no more history for this symbol.
        if len(rows) < want:
            break
        cursor_end = int(rows[-1][0]) - 1        # rows[-1] is the oldest in the page
        if remaining > 0:
            time.sleep(pause)                    # rate limit courtesy

    return _rows_to_frame(all_rows).tail(limit)


def fetch_instruments(category: str = "spot", timeout: float = 15) -> list:
    """
    Every instrument Bybit lists for `category`, following nextPageCursor.

    Returns dicts of {symbol, base_coin, quote_coin, status}. Spot currently fits
    in one page (~555 symbols), but linear/inverse paginate, so the cursor is
    honoured regardless.
    """
    out, cursor, seen_cursors = [], None, set()

    while True:
        params = {"category": category, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = _get(BYBIT_INSTRUMENTS, params, timeout=timeout)

        for row in result.get("list") or []:
            symbol = row.get("symbol")
            if not symbol:
                continue
            out.append({
                "symbol": symbol,
                "base_coin": row.get("baseCoin"),
                "quote_coin": row.get("quoteCoin"),
                "status": row.get("status"),
            })

        cursor = result.get("nextPageCursor")
        # Bybit returns "" / None on the last page; the seen-check is a guard
        # against a repeating cursor spinning this loop forever.
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return out


def fetch_tickers(category: str = "spot", timeout: float = 15) -> list:
    """
    One ticker row per tradable symbol in `category`, from GET /v5/market/tickers.

    A single call covers every symbol. Spot rows carry lastPrice and
    price24hPcnt (a fraction as text: "0.0123" means +1.23% over 24h).
    """
    result = _get(BYBIT_TICKERS, {"category": category}, timeout=timeout)
    return result.get("list") or []


def fetch_and_save_all(symbol: str = "BTCUSDT", months_back: int = 6,
                        timeframes: list = None, category: str = "spot",
                        out_dir: str = "./data", max_bars: int = None) -> dict:
    """
    Fetch all timeframes, save CSVs, return dict of DataFrames.

    max_bars caps how far back each timeframe reaches, so one months_back can be
    shared across 1D and 1M without pulling a quarter-million minute bars.
    """
    timeframes = timeframes or ["1W", "1D", "4H", "1H", "15M"]
    os.makedirs(out_dir, exist_ok=True)

    result = {}
    for tf in timeframes:
        tf = tf.upper()
        days_back = None
        if max_bars:
            capped = max_bars * TF_MINUTES[tf] / (24 * 60)
            days_back = min(months_back * 30, capped)
        span = f"{days_back:.1f}d" if days_back is not None else f"{months_back}mo"
        print(f"Fetching {symbol} {tf} ({span})...")
        df = fetch_klines(symbol, tf, months_back, category, days_back=days_back)
        result[tf] = df
        path = os.path.join(out_dir, f"{symbol.lower()}_{tf.lower()}.csv")
        df.to_csv(path)
        print(f"  {len(df)} rows -> {path}")

    return result


def _cli():
    import argparse

    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--symbols", default="BTCUSDT",
                   help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    p.add_argument("--timeframes", default="1D,4H,1H",
                   help=f"comma-separated, from {sorted(INTERVAL_MAP)}")
    p.add_argument("--months-back", type=float, default=6)
    p.add_argument("--max-bars", type=int, default=None,
                   help="cap bars per timeframe (sane for 1M/5M: 5000)")
    p.add_argument("--category", default="spot")
    p.add_argument("--out-dir", default="./data")
    args = p.parse_args()

    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        fetch_and_save_all(
            symbol=symbol,
            months_back=args.months_back,
            timeframes=[t.strip().upper() for t in args.timeframes.split(",") if t.strip()],
            category=args.category,
            out_dir=args.out_dir,
            max_bars=args.max_bars,
        )


if __name__ == "__main__":
    _cli()