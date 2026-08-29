"""
tbb.api.common — shared constants and helpers for the API layer.

Pure helpers only: no FastAPI app object lives here, so route modules can
import from this without cycles. Storage rules stay exactly as documented in
readme.md (LIVE vs ANALYSIS vs PREDICT never mix).
"""

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from fastapi import HTTPException

from tbb import config as paths
from tbb.backtesting import backtest
from tbb.live import runner as live_runner
from tbb.marketdata import market_data
from tbb.storage import live_store

DATA_DIR = paths.DATA_DIR

# `symbol` reaches the filesystem via backtest.load_data's f-string, so it is
# whitelisted by shape here and then checked against files that actually exist.
# Without this, a crafted symbol could escape DATA_DIR.
SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,20}$")
# 1M/5M/30M = minutes, 1H/4H, 1D, 1W, and 1MO = one month. "1MO" rather than
# "1M" for monthly because "1M" already means one minute across this codebase
# (btcusdt_15m.csv); see ingestion_bybit.INTERVAL_MAP.
TIMEFRAME_RE = re.compile(r"^[0-9]{1,3}(?:[mMhHdDwW]|[mM][oO])$")

# The timeframes backtest.load_data reads off disk. All three must be present
# locally for a symbol to be analysable; this is what `analysis_available` means.
ANALYSIS_TFS = ("1D", "4H", "1H")

# Charts show LIVE price at every timeframe: source=auto prefers Bybit whenever
# Bybit can serve the timeframe, and only falls back to local CSV if the fetch
# fails or the label is not fetchable. Structural overlays are a separate matter
# â€” they come from live_store and lag live price, which is what the
# `overlay_freshness` block on /live/state, /live/zones and /live/levels reports.

# One heavy backtest at a time: run_symbol is CPU-bound, and letting several
# run concurrently would just make them all slow.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyze")

# path -> (mtime_ns, size, last_bar_ts). One entry per file, so it stays small.
_CSV_TAIL_CACHE = {}

# ---- POST /predict ----
# Bias always comes from these three, in this order, exactly as live_runner does.
# analyze_pair_with_bias takes the LAST frame in df_by_tf for pattern detection,
# so the order is load-bearing: patterns come off 1H, matching live behaviour
# whatever the execution timeframe is.
PREDICT_BIAS_TFS = live_runner.BIAS_TFS            # ("1D", "4H", "1H")

# Enough execution bars for the tail(200) the pipeline slices, plus warmup for
# ATR(14) and the zigzag. Below backtest.LOOKBACK_BARS we refuse rather than hand
# the pipeline a frame it cannot judge.
PREDICT_EXEC_BARS = 300
PREDICT_MIN_BIAS_BARS = 50

# ---------- validation / helpers ----------

def _clean_symbol(symbol: str) -> str:
    if not symbol or not SYMBOL_RE.match(symbol):
        raise HTTPException(400, f"invalid symbol {symbol!r}")
    return symbol.upper()


def _clean_timeframe(timeframe):
    if timeframe is None:
        return None
    if not TIMEFRAME_RE.match(timeframe):
        raise HTTPException(400, f"invalid timeframe {timeframe!r}")
    return timeframe.upper()


def _csv_path(symbol: str, timeframe: str) -> str:
    path = os.path.join(DATA_DIR, f"{symbol.lower()}_{timeframe.lower()}.csv")
    # Defence in depth: the regexes should make this unreachable.
    if os.path.dirname(os.path.abspath(path)) != DATA_DIR:
        raise HTTPException(400, "invalid symbol/timeframe")
    return path


def _require_csv(symbol: str, timeframe: str) -> str:
    path = _csv_path(symbol, timeframe)
    if not os.path.exists(path):
        raise HTTPException(404, f"no data for {symbol} {timeframe}")
    return path


def _local_history() -> dict:
    """{SYMBOL: {TF, ...}} discovered from data/*.csv."""
    found = {}
    if os.path.isdir(DATA_DIR):
        for fn in os.listdir(DATA_DIR):
            if not fn.endswith(".csv") or "_" not in fn:
                continue
            sym, _, tf = fn[:-4].rpartition("_")
            if sym:
                found.setdefault(sym.upper(), set()).add(tf.upper())
    return found


def _analysis_ready(symbol: str, local_tfs=None) -> bool:
    """True when every timeframe backtest.load_data needs is on disk."""
    if local_tfs is None:
        return all(os.path.exists(_csv_path(symbol, tf)) for tf in ANALYSIS_TFS)
    return all(tf in local_tfs for tf in ANALYSIS_TFS)


def _tf_sort_key(tf: str):
    """Order timeframes by bar duration; unrecognised labels sort last, by name."""
    minutes = market_data.timeframe_minutes(tf)
    return (0, minutes, tf) if minutes is not None else (1, 0, tf)


def _read_local_candles(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    path = _require_csv(symbol, timeframe)
    return pd.read_csv(path, index_col=0, parse_dates=True).tail(limit)


def _last_csv_timestamp(path: str):
    """
    Newest bar timestamp in a CSV, as a UTC Timestamp, or None.

    Reads the file tail instead of parsing the whole thing: this runs on every
    overlay request and the 1H files are thousands of rows. Memoised on
    (mtime, size) so an unchanged file is read once.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None

    cached = _CSV_TAIL_CACHE.get(path)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, st.st_size - 4096))
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        if not lines:
            return None
        # Column 0 is the index (ts). A header-only file lands on "ts", which
        # fails to parse and correctly yields None.
        ts = pd.Timestamp(lines[-1].decode("utf-8", "replace").split(",")[0])
    except (OSError, ValueError, IndexError):
        return None

    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    _CSV_TAIL_CACHE[path] = (st.st_mtime_ns, st.st_size, ts)
    return ts


def _overlay_freshness(symbol: str, computed_at=None) -> dict:
    """
    How current the structural overlays (bias / regime / zones / S-R levels) are.

    These come out of live_store, written by live_runner, which seeds from the
    local CSVs and then appends live WS bars in memory. So the bound on overlay
    freshness is the SNAPSHOT timestamp â€” the execution bar the analysis last ran
    on â€” not the CSV tail; a long-running live_runner is well ahead of what is on
    disk, and a stopped one is behind it. `last_local_bar` is reported alongside
    because it is what a fresh restart or a POST /analyze would see.

    /ohlc now serves live Bybit candles by default, so a chart can show a price
    the overlays have never seen. This block is what lets the frontend say so
    instead of implying the structural analysis is real-time.
    """
    bar_seconds = (market_data.timeframe_minutes(backtest.EXECUTION_TF) or 60) * 60
    computed_at = None if computed_at is None else pd.Timestamp(computed_at)
    if computed_at is not None and computed_at.tzinfo is None:
        computed_at = computed_at.tz_localize("UTC")

    lag = None
    if computed_at is not None:
        lag = max(0.0, (pd.Timestamp.now(tz="UTC") - computed_at).total_seconds())

    # One bar of lag is just the current candle not having closed yet. Past two,
    # the overlays are genuinely behind live price.
    stale = lag is None or lag > 2 * bar_seconds
    if computed_at is None:
        note = ("no overlay snapshot recorded yet â€” live_runner.py does not appear "
                "to be running, so overlays are unavailable rather than merely stale")
    elif stale:
        note = ("overlays were computed on an earlier bar and may lag the live "
                "candles /ohlc returns")
    else:
        note = None

    return {
        "overlay_timeframe": backtest.EXECUTION_TF,
        "computed_at": _iso(computed_at),
        "computed_at_ms": _ms(computed_at),
        "last_local_bar": _iso(_last_csv_timestamp(
            _csv_path(symbol, backtest.EXECUTION_TF))),
        "last_local_bar_by_timeframe": {
            tf: _iso(_last_csv_timestamp(_csv_path(symbol, tf)))
            for tf in ANALYSIS_TFS
        },
        "lag_seconds": None if lag is None else round(lag),
        "bar_seconds": bar_seconds,
        "stale": stale,
        "note": note,
    }


def _snapshot_ts(rows, symbol):
    """Newest snapshot ts across overlay rows, falling back to the latest tick."""
    stamps = [r["ts"] for r in rows if r["ts"] is not None]
    if stamps:
        return max(stamps)
    tick = live_store.latest_tick(symbol)
    return None if tick is None else tick["ts"]


def _remote_candles(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """
    On-demand chart candles from Bybit. 404 when Bybit does not list the pair,
    502 when Bybit itself is the problem â€” never a 500.
    """
    if not market_data.is_supported_timeframe(timeframe):
        raise HTTPException(404, f"no data for {symbol} {timeframe} â€” "
                                 f"fetchable timeframes: "
                                 f"{', '.join(market_data.SUPPORTED_TIMEFRAMES)}")
    # is_tradable returns None when the instrument list is unavailable; in that
    # case fall through and let the kline request decide.
    if market_data.is_tradable(symbol) is False:
        raise HTTPException(404, f"no data for {symbol} {timeframe}")
    try:
        return market_data.recent_candles(symbol, timeframe, limit)
    except market_data.SymbolNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except market_data.MarketDataError as exc:
        raise HTTPException(502, f"upstream market data unavailable: {exc}") from exc


def _ms(ts):
    if ts is None:
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def _iso(ts):
    if ts is None:
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def _json_safe(obj):
    """
    Replace non-finite floats with None, recursively.

    JSON has no literal for inf/nan and Starlette serialises with
    allow_nan=False, so an infinite profit factor (a no-loss run â€” the codebase's
    convention from rr_profit_factor) would otherwise turn the whole response
    into a 500. Companion *_infinite flags tell the frontend when None means "no
    losses yet" rather than "no data".
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _split_pf(pf):
    """(finite value or None, was_infinite) for a profit factor."""
    if pf is None:
        return None, False
    pf = float(pf)
    return (pf, False) if math.isfinite(pf) else (None, True)


