"""tbb.api.routes_market — symbol discovery, chart candles, pattern stats."""

import os

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from tbb.backtesting import backtest
from tbb.marketdata import market_data
from tbb.storage import pattern_stats, signal_store
from tbb.api.common import (
    ANALYSIS_TFS, _analysis_ready, _clean_symbol, _clean_timeframe, _csv_path,
    _iso, _json_safe, _local_history, _ms, _read_local_candles, _remote_candles,
    _require_csv, _tf_sort_key,
)

router = APIRouter()

@router.get("/symbols")
def symbols(refresh: bool = Query(False, description="force a Bybit instrument-list refresh")):
    """
    Every symbol the frontend can chart: Bybit's live tradable list (cached, see
    `bybit.refresh_seconds`) unioned with whatever local CSV history exists.

    Per symbol:
      timeframes         â€” everything /ohlc will serve, local or on-demand
      local_timeframes   â€” served straight from CSV, no network call
      analysis_available â€” true only if POST /analyze can run it, i.e. local
                           1D/4H/1H history exists. Chart-only symbols get
                           candles but no pipeline overlays.
    """
    local = _local_history()
    warning = None
    try:
        tradable = set(market_data.tradable_symbols(force_refresh=refresh))
    except market_data.MarketDataError as exc:
        tradable = set()
        warning = f"Bybit instrument list unavailable â€” local history only: {exc}"

    # Display enrichment for the symbol switcher. Tickers are one cached Bybit
    # call; if they fail, price/change24h come back null and the list still
    # renders â€” never let cosmetics break symbol discovery.
    try:
        tickers = market_data.ticker_snapshots()
        ticker_warning = None
    except Exception as exc:
        tickers = {}
        ticker_warning = f"ticker snapshot unavailable â€” no prices shown: {exc}"

    chart_tfs = set(market_data.SUPPORTED_TIMEFRAMES)
    out = []
    for sym in sorted(tradable | set(local)):
        local_tfs = local.get(sym, set())
        fetchable = chart_tfs if sym in tradable else set()
        tk = tickers.get(sym, {})
        out.append({
            "symbol": sym,
            "timeframes": sorted(local_tfs | fetchable, key=_tf_sort_key),
            "local_timeframes": sorted(local_tfs, key=_tf_sort_key),
            "analysis_available": _analysis_ready(sym, local_tfs),
            "has_local_history": bool(local_tfs),
            "chart_source": ("local+bybit" if (local_tfs and fetchable)
                             else ("bybit" if fetchable else "local")),
            "price": tk.get("price"),
            "change24h": tk.get("change24h"),   # fraction: 0.0123 = +1.23%
        })

    return {
        "count": len(out),
        "analysis_count": sum(1 for s in out if s["analysis_available"]),
        "analysis_timeframes": list(ANALYSIS_TFS),
        "chart_timeframes": sorted(chart_tfs, key=_tf_sort_key),
        "bybit": market_data.symbol_cache_info(),
        "tickers": market_data.ticker_cache_info(),
        "warning": "; ".join(w for w in (warning, ticker_warning) if w) or None,
        "symbols": out,
    }


# ---------- OHLC (charting) ----------

@router.get("/ohlc")
def ohlc(symbol: str = Query(...), timeframe: str = Query("1H"),
         limit: int = Query(market_data.DEFAULT_CHART_BARS, ge=1,
                            le=market_data.MAX_CHART_BARS,
                            description="bars to return; defaults to a full chart"),
         source: str = Query("auto", pattern="^(auto|local|bybit)$")):
    """
    Recent candles for charting, oldest -> newest.

    LIVE VIEW endpoint: no date range, no tracked-symbol restriction. Works for
    any symbol Bybit lists, at any interval Bybit offers (1M/3M/5M/15M/30M,
    1H/2H/4H/6H/12H, 1D, 1W, 1MO â€” note 1M is one MINUTE, 1MO is one month).
    Date-range selection belongs to POST /analyze, not here.

    With source=auto (the default) every timeframe is served LIVE from Bybit; the
    local CSV is only used as a fallback, when Bybit is unreachable or the
    timeframe label is not one Bybit can serve. Force either side with
    source=local | source=bybit.

    `limit` defaults to a full chart and is capped at max_limit (both settable via
    BYBIT_CHART_BARS / BYBIT_MAX_CHART_BARS). Requests past Bybit's 1000-per-call
    cap paginate rather than truncate, so nothing is silently cut short; when
    Bybit has less history than asked for, `note` says how much it actually has.

    The candles here are live; the structural overlays on /live/* are not. See
    the `overlay_freshness` block on those endpoints before drawing them onto
    this chart as though they were simultaneous.
    """
    symbol = _clean_symbol(symbol)
    timeframe = _clean_timeframe(timeframe) or "1H"

    path = _csv_path(symbol, timeframe)
    has_local = os.path.exists(path)

    if source == "local":
        use_local = True
    elif source == "bybit":
        use_local = False
    else:
        # Live first at every timeframe; only a label Bybit cannot serve (e.g. a
        # 2H CSV someone dropped in data/) goes straight to disk.
        use_local = not market_data.is_supported_timeframe(timeframe)

    notes = []
    if use_local:
        df = _read_local_candles(symbol, timeframe, limit)
        resolved = "local_csv"
    else:
        try:
            df = _remote_candles(symbol, timeframe, limit)
            resolved = "bybit"
        except HTTPException as exc:
            # Serving stale local bars beats blanking a chart, but only when the
            # caller let us choose. source=bybit means bybit or nothing.
            if source != "auto" or not has_local:
                raise
            df = _read_local_candles(symbol, timeframe, limit)
            resolved = "local_csv"
            notes.append(f"bybit unavailable ({exc.detail}) â€” served local CSV "
                         f"history, which may lag live price")

    candles = [{
        "ts": _iso(ts), "ts_ms": _ms(ts),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r["volume"]) if "volume" in r and pd.notna(r["volume"]) else None,
    } for ts, r in df.iterrows()]

    if len(candles) < limit:
        where = "bybit" if resolved == "bybit" else "the local CSV"
        notes.append(f"{where} has {len(candles)} bars for {symbol} {timeframe}, "
                     f"fewer than the {limit} requested â€” that is all the "
                     f"history available, not a truncation")

    return {"symbol": symbol, "timeframe": timeframe,
            "source": resolved, "has_local_history": has_local,
            "analysis_available": _analysis_ready(symbol),
            "requested_limit": limit, "max_limit": market_data.MAX_CHART_BARS,
            "note": "; ".join(notes) or None,
            "count": len(candles), "candles": candles}


@router.get("/patterns/stats")
def patterns_stats(min_occurrences: int = Query(5, ge=1, le=1000)):
    """
    Per-pattern win rates from the signal ledger (signals.db), one row per
    (pattern, pair, timeframe): {pattern, pair, timeframe, occurrences, wins,
    win_rate (percent), avg_rr (planned R:R)}.

    Only RESOLVED signals count. Rows with fewer than `min_occurrences`
    samples are withheld â€” a pattern that fired once and won is noise, not an
    edge. Empty list means the ledger has no resolved outcomes yet, which is
    the honest answer until live/predicted signals get their outcomes closed.
    """
    signal_store.init_db()
    rows = pattern_stats.get_stats(min_occurrences=min_occurrences)
    return {
        "source": "signals.db",
        "min_occurrences": min_occurrences,
        "count": len(rows),
        "patterns": _json_safe(rows),
    }


