"""
market_data.py — on-demand Bybit market data for CHART DISPLAY only.

Two caches sit in front of ingestion_bybit's REST calls:

* the tradable-symbol list from /v5/market/instruments-info, refreshed every
  SYMBOL_TTL seconds;
* recent candles per (symbol, timeframe), with a TTL scaled to the bar duration
  so a 1M chart is not served a five-minute-old last bar.

Scope boundary, deliberately: everything here is chart-only. Full pipeline
analysis still requires local CSV history for 1D/4H/1H, so it stays limited to
the tracked symbols in backtest.SYMBOLS. Nothing in this module writes to disk
or to either database.
"""

import os
import threading
import time
from collections import OrderedDict

import pandas as pd

import ingestion_bybit

# Existing data/*.csv were fetched from the spot category, so chart-only
# fallbacks stay on spot for price continuity between local and remote bars.
CATEGORY = os.environ.get("BYBIT_CATEGORY", "spot")

SYMBOL_TTL = float(os.environ.get("BYBIT_SYMBOL_TTL", 900))       # 15 min

# Live charts want real depth, not a recent slice, so the default is a full
# 1000-bar chart and the ceiling is generous. Beyond 1000 the fetch paginates.
# The ceiling exists because the response is JSON in one shot: 1m history goes
# back years on major pairs, and serving all of it would be a multi-hundred-MB
# response, so "as much as Bybit will give" is bounded here rather than by
# accident. 20k bars is ~14 days of 1m, ~55 years of 1D — past any coin's life.
DEFAULT_CHART_BARS = int(os.environ.get("BYBIT_CHART_BARS", 1000))
MAX_CHART_BARS = int(os.environ.get("BYBIT_MAX_CHART_BARS", 20000))

# Bybit's per-request cap; MAX_CHART_BARS above it just means more pages.
BARS_PER_REQUEST = ingestion_bybit.MAX_KLINES_PER_REQUEST

# Cap resident candle data by BARS, not entry count — one 20k-bar frame is worth
# a hundred 200-bar ones, so an entry-count cap alone could hold ~250MB.
# 2M bars of float64 OHLCV is roughly 80MB.
MAX_OHLC_ENTRIES = int(os.environ.get("BYBIT_OHLC_CACHE_ENTRIES", 256))
MAX_OHLC_CACHE_BARS = int(os.environ.get("BYBIT_OHLC_CACHE_BARS", 2_000_000))

# Roughly a quarter of a bar, clamped to [15s, 5min]. Fresh enough that the
# forming candle keeps moving on every timeframe — an uncapped quarter-bar would
# freeze a 1W chart for over a day — loose enough that panning a chart does not
# hammer Bybit.
OHLC_TTL_FLOOR = 15.0
OHLC_TTL_CEILING = float(os.environ.get("BYBIT_OHLC_TTL_MAX", 300))
_OHLC_TTL = {
    tf: min(OHLC_TTL_CEILING, max(OHLC_TTL_FLOOR, mins * 60 / 4))
    for tf, mins in ingestion_bybit.TF_MINUTES.items()
}

SUPPORTED_TIMEFRAMES = tuple(sorted(
    ingestion_bybit.INTERVAL_MAP,
    key=lambda tf: ingestion_bybit.TF_MINUTES[tf],
))


class MarketDataError(RuntimeError):
    """Bybit was unreachable or returned an error we cannot interpret."""


class SymbolNotFound(MarketDataError):
    """Bybit does not list this symbol/timeframe pair."""


# ---------- tradable symbol list ----------

_sym_lock = threading.Lock()
_sym_cache = {
    "symbols": None,        # tuple[str] | None — None means never fetched
    "index": frozenset(),
    "fetched_at": 0.0,      # time.monotonic()
    "fetched_wall": None,   # pd.Timestamp, for the API payload
    "error": None,          # last refresh failure, even if stale data is served
}


def is_supported_timeframe(timeframe: str) -> bool:
    return bool(timeframe) and timeframe.upper() in ingestion_bybit.INTERVAL_MAP


def timeframe_minutes(timeframe: str):
    """Bar duration in minutes, or None for a label Bybit cannot serve."""
    return ingestion_bybit.TF_MINUTES.get((timeframe or "").upper())


def tradable_symbols(force_refresh: bool = False) -> tuple:
    """
    Cached tuple of tradable Bybit symbols for CATEGORY, alphabetical.

    A failed refresh keeps serving the previous list (recorded in
    symbol_cache_info()["error"]); only a failure with nothing cached raises.
    The HTTP call happens under the lock so a burst of requests triggers one
    fetch rather than one per request.
    """
    with _sym_lock:
        age = time.monotonic() - _sym_cache["fetched_at"]
        if _sym_cache["symbols"] is not None and not force_refresh and age < SYMBOL_TTL:
            return _sym_cache["symbols"]

        try:
            rows = ingestion_bybit.fetch_instruments(CATEGORY)
        except Exception as exc:
            _sym_cache["error"] = f"{type(exc).__name__}: {exc}"
            if _sym_cache["symbols"] is None:
                raise MarketDataError(
                    f"could not fetch Bybit {CATEGORY} instrument list: {exc}"
                ) from exc
            return _sym_cache["symbols"]   # stale beats empty

        symbols = tuple(sorted({
            r["symbol"].upper() for r in rows
            # Bybit marks delisted/pre-launch instruments with other statuses;
            # only "Trading" ones have klines to chart.
            if r.get("status") == "Trading"
        }))
        _sym_cache.update(
            symbols=symbols,
            index=frozenset(symbols),
            fetched_at=time.monotonic(),
            fetched_wall=pd.Timestamp.now(tz="UTC"),
            error=None,
        )
        return symbols


def is_tradable(symbol: str):
    """
    True / False, or None when the answer is unknown (Bybit unreachable and
    nothing cached). None means "do not conclude 404 from this".
    """
    try:
        tradable_symbols()
    except MarketDataError:
        return None
    with _sym_lock:
        return symbol.upper() in _sym_cache["index"]


def symbol_cache_info() -> dict:
    with _sym_lock:
        cached = _sym_cache["symbols"]
        return {
            "category": CATEGORY,
            "count": 0 if cached is None else len(cached),
            "fetched_at": (None if _sym_cache["fetched_wall"] is None
                           else _sym_cache["fetched_wall"].isoformat()),
            "age_seconds": (None if cached is None
                            else round(time.monotonic() - _sym_cache["fetched_at"], 1)),
            "refresh_seconds": SYMBOL_TTL,
            "stale": cached is not None and _sym_cache["error"] is not None,
            "error": _sym_cache["error"],
        }


# ---------- tickers (last price / 24h change) ----------

TICKER_TTL = float(os.environ.get("BYBIT_TICKER_TTL", 30))

_tick_lock = threading.Lock()
_tick_cache = {
    "map": None,        # {SYMBOL: {"price","change24h"}} | None = never fetched
    "fetched_at": 0.0,  # time.monotonic()
    "error": None,      # last refresh failure, even when stale data is served
}


def ticker_snapshots(force_refresh: bool = False) -> dict:
    """
    {SYMBOLUPPER: {"price": float|None, "change24h": float|None}} for CATEGORY.

    One Bybit call covers every symbol; cached TICKER_TTL seconds (tickers are
    display enrichment, not trading input). A failed refresh keeps serving the
    previous snapshot; a failure with nothing cached returns {} — callers must
    treat price/change as optional and never block the symbol list on it.
    """
    with _tick_lock:
        age = time.monotonic() - _tick_cache["fetched_at"]
        if _tick_cache["map"] is not None and not force_refresh and age < TICKER_TTL:
            return _tick_cache["map"]

        try:
            rows = ingestion_bybit.fetch_tickers(CATEGORY)
        except Exception as exc:
            _tick_cache["error"] = f"{type(exc).__name__}: {exc}"
            return _tick_cache["map"] or {}

        out = {}
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            if not sym:
                continue

            def _f(key):
                v = r.get(key)
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None

            out[sym] = {"price": _f("lastPrice"),
                        "change24h": _f("price24hPcnt")}
        _tick_cache.update(map=out, fetched_at=time.monotonic(), error=None)
        return out


def ticker_cache_info() -> dict:
    with _tick_lock:
        return {
            "count": 0 if _tick_cache["map"] is None else len(_tick_cache["map"]),
            "age_seconds": (None if _tick_cache["map"] is None
                            else round(time.monotonic() - _tick_cache["fetched_at"], 1)),
            "refresh_seconds": TICKER_TTL,
            "error": _tick_cache["error"],
        }


# ---------- recent candles ----------

_ohlc_lock = threading.Lock()
# (symbol, tf) -> {"df", "bars", "requested", "fetched_at"}
#   bars      = rows actually held
#   requested = rows asked of Bybit. A shorter `bars` than `requested` means the
#               symbol's history simply ends there, so the entry still satisfies
#               any limit up to `requested` — without this a young coin would
#               miss the cache on every single request.
_ohlc_cache = OrderedDict()


def _cache_get(key, limit):
    with _ohlc_lock:
        entry = _ohlc_cache.get(key)
        if entry is None:
            return None
        ttl = _OHLC_TTL.get(key[1], 60.0)
        if time.monotonic() - entry["fetched_at"] >= ttl or entry["requested"] < limit:
            return None
        _ohlc_cache.move_to_end(key)
        return entry["df"]


def _cache_put(key, df, requested):
    with _ohlc_lock:
        _ohlc_cache[key] = {"df": df, "bars": len(df), "requested": requested,
                            "fetched_at": time.monotonic()}
        _ohlc_cache.move_to_end(key)
        # Evict least-recently-served until both the entry count and the total
        # resident bar count are within budget. Never evict the entry just
        # inserted, even if it alone exceeds the bar budget.
        while len(_ohlc_cache) > 1 and (
            len(_ohlc_cache) > MAX_OHLC_ENTRIES
            or sum(e["bars"] for e in _ohlc_cache.values()) > MAX_OHLC_CACHE_BARS
        ):
            _ohlc_cache.popitem(last=False)


def recent_candles(symbol: str, timeframe: str, limit: int = None) -> pd.DataFrame:
    """
    Most recent `limit` candles for symbol/timeframe, straight from Bybit,
    memoised per (symbol, timeframe).

    limit above Bybit's 1000-per-request cap paginates backward rather than
    truncating, so a live chart can show full history depth. Raises
    SymbolNotFound if Bybit does not know the symbol, MarketDataError for
    anything else (network, rate limit, unexpected retCode).
    """
    symbol = symbol.upper()
    timeframe = (timeframe or "").upper()
    if not is_supported_timeframe(timeframe):
        raise SymbolNotFound(
            f"timeframe {timeframe!r} is not fetchable from Bybit; "
            f"supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
        )

    limit = DEFAULT_CHART_BARS if limit is None else int(limit)
    limit = max(1, min(limit, MAX_CHART_BARS))
    key = (symbol, timeframe)

    cached = _cache_get(key, limit)
    if cached is not None:
        return cached.tail(limit)

    # Round small requests up to one full page — the page costs the same, and it
    # stops a 5-bar probe from poisoning the cache for a full chart moments later.
    bars = max(limit, min(BARS_PER_REQUEST, MAX_CHART_BARS))
    try:
        df = ingestion_bybit.fetch_last_n_klines(symbol, timeframe, limit=bars,
                                                 category=CATEGORY)
    except ingestion_bybit.BybitError as exc:
        if exc.ret_code == 10001:   # "Not supported symbols"
            raise SymbolNotFound(f"Bybit does not list {symbol} on {CATEGORY}") from exc
        raise MarketDataError(str(exc)) from exc
    except Exception as exc:
        raise MarketDataError(f"Bybit request failed: {type(exc).__name__}: {exc}") from exc

    if df.empty:
        raise SymbolNotFound(f"Bybit returned no {timeframe} candles for {symbol}")

    # Store what actually came back, tagged with what we asked for.
    _cache_put(key, df, bars)
    return df.tail(limit)


def cache_stats() -> dict:
    """Diagnostics for /health."""
    with _ohlc_lock:
        entries = len(_ohlc_cache)
        bars = sum(e["bars"] for e in _ohlc_cache.values())
    return {
        "symbols": symbol_cache_info(),
        "ohlc_cache_entries": entries,
        "ohlc_cache_max_entries": MAX_OHLC_ENTRIES,
        "ohlc_cache_bars": bars,
        "ohlc_cache_max_bars": MAX_OHLC_CACHE_BARS,
        "chart_bars_default": DEFAULT_CHART_BARS,
        "chart_bars_max": MAX_CHART_BARS,
        "bars_per_request": BARS_PER_REQUEST,
        "timeframes": list(SUPPORTED_TIMEFRAMES),
    }


def reset_caches():
    """Drop both caches. For tests and manual refresh."""
    with _sym_lock:
        _sym_cache.update(symbols=None, index=frozenset(), fetched_at=0.0,
                          fetched_wall=None, error=None)
    with _ohlc_lock:
        _ohlc_cache.clear()
