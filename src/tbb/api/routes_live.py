"""tbb.api.routes_live — /live/* endpoints backed ONLY by live_state.db."""

import json

from fastapi import APIRouter, Query

from tbb.storage import live_store
from tbb.api.common import (
    _clean_symbol, _clean_timeframe, _json_safe, _overlay_freshness,
    _snapshot_ts, _split_pf,
)

router = APIRouter()

# ---------- LIVE ----------

@router.get("/live/state")
def live_state(symbol: str = Query(...), skip_history: int = Query(20, ge=0, le=500)):
    """
    Current bias per timeframe, regime status, latest skip reason, and the last N
    skip reasons for the live funnel feed.

    `overlay_freshness` says when this structural picture was computed. /ohlc
    serves live candles, so treat it as the authority on whether the overlays are
    simultaneous with the chart.
    """
    symbol = _clean_symbol(symbol)
    tick = live_store.latest_tick(symbol)
    if tick is None:
        return {"symbol": symbol, "has_state": False,
                "message": "no live ticks recorded yet â€” is live_runner.py running?",
                "timeframes": {}, "recent_skips": [], "skip_reason_counts": {},
                "signal": None,
                "overlay_freshness": _overlay_freshness(symbol)}

    # The full pipeline verdict for the latest tick, verbatim as the runner
    # stored it (direction/entry/sl/tp/rr/confluence_score/confidence/
    # confluence_breakdown + engine blocks). Null unless signal_fired.
    signal = None
    if tick["signal_json"]:
        try:
            signal = json.loads(tick["signal_json"])
        except (json.JSONDecodeError, TypeError):
            signal = None

    return {
        "symbol": symbol,
        "has_state": True,
        "timestamp": tick["ts"],
        "timestamp_ms": tick["ts_ms"],
        "recorded_at": tick["recorded_at"],
        "execution_timeframe": tick["execution_tf"],
        "overlay_freshness": _overlay_freshness(symbol, tick["ts"]),
        "timeframes": live_store.tf_state_for_tick(tick["id"]),
        "topdown_bias": tick["topdown_bias"],
        "aligned_count": tick["aligned_count"],
        "bias_gate_passed": bool(tick["tradable"]) if tick["tradable"] is not None else None,
        "direction": tick["direction"],
        "signal_fired": tick["signal_json"] is not None,
        "signal": signal,
        "latest_skip_reason": tick["skip_reason"],
        "latest_skip_reason_raw": tick["skip_reason_raw"],
        "recent_skips": live_store.recent_skips(symbol, limit=skip_history),
        "skip_reason_counts": live_store.skip_reason_counts(symbol),
    }


@router.get("/live/skips")
def live_skips(symbol: str = Query(...), limit: int = Query(50, ge=1, le=500)):
    """
    Rolling skip history for the funnel drawer: every recorded skip reason with
    its candle timestamp and classified category, newest first, plus all-time
    per-category counts. Same data /live/state embeds, standalone so a
    skip-focused UI does not pull the whole snapshot each poll.
    """
    symbol = _clean_symbol(symbol)
    return {
        "symbol": symbol,
        "count": limit,
        "skips": live_store.recent_skips(symbol, limit=limit),
        "counts": live_store.skip_reason_counts(symbol),
    }


@router.get("/live/zones")
def live_zones(symbol: str = Query(...), timeframe: str = Query(None)):
    """
    Active FVG/imbalance zones and consolidation zone boundaries as price ranges
    with start/end timestamps. Omit `timeframe` for every timeframe at once.

    These are drawn over live chart candles, so `overlay_freshness` reports the
    bar they were computed on â€” check `stale` before presenting them as current.
    """
    symbol = _clean_symbol(symbol)
    timeframe = _clean_timeframe(timeframe)
    rows = live_store.current_zones(symbol, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(rows),
        "overlay_freshness": _overlay_freshness(symbol, _snapshot_ts(rows, symbol)),
        "zones": [{
            "timeframe": r["timeframe"],
            "kind": r["kind"],                  # 'fvg' | 'consolidation'
            "direction": r["direction"],
            "price_low": r["price_low"],
            "price_high": r["price_high"],
            "start_ts": r["start_ts"], "start_ts_ms": r["start_ts_ms"],
            "end_ts": r["end_ts"], "end_ts_ms": r["end_ts_ms"],
            "tested": None if r["tested"] is None else bool(r["tested"]),
            "snapshot_ts": r["ts"], "snapshot_ts_ms": r["ts_ms"],
        } for r in rows],
    }


@router.get("/live/levels")
def live_levels(symbol: str = Query(...), timeframe: str = Query(None)):
    """
    Current S/R levels, strongest (most touches) first, with SUPPORT/RESISTANCE/BOTH type.

    Same caveat as /live/zones: see `overlay_freshness` before drawing these onto
    live chart candles.
    """
    symbol = _clean_symbol(symbol)
    timeframe = _clean_timeframe(timeframe)
    rows = live_store.current_levels(symbol, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(rows),
        "overlay_freshness": _overlay_freshness(symbol, _snapshot_ts(rows, symbol)),
        "levels": [{
            "timeframe": r["timeframe"],
            "price": r["price"],
            "touches": r["touches"],
            "type": r["level_type"],            # SUPPORT | RESISTANCE | BOTH
            "snapshot_ts": r["ts"], "snapshot_ts_ms": r["ts_ms"],
        } for r in rows],
    }


@router.get("/live/stats")
def live_stats(symbol: str = Query(None), trade_limit: int = Query(50, ge=0, le=500)):
    """
    Running win_rate / avg_rr / profit_factor_R over RESOLVED LIVE trades only.
    Entirely separate from backtest/analysis numbers. Omit `symbol` for all symbols.
    """
    symbol = _clean_symbol(symbol) if symbol else None
    stats = live_store.live_stats(symbol)
    pf, pf_inf = _split_pf(stats.pop("profit_factor_R", None))
    trades = live_store.resolved_trades(symbol, limit=trade_limit) if trade_limit else []
    return _json_safe({
        "source": "live",
        **stats,
        "profit_factor_R": pf,
        "profit_factor_R_infinite": pf_inf,   # True = wins but zero losses so far
        "trades": [{
            "id": t["id"], "symbol": t["symbol"], "timeframe": t["timeframe"],
            "direction": t["direction"], "outcome": t["outcome"],
            "entry_price": t["entry_price"], "stop_price": t["stop_price"],
            "take_profit": t["take_profit"],
            "planned_rr": t["planned_rr"], "realized_rr": t["realized_rr"],
            "pnl": t["pnl"],
            "opened_at": t["opened_at"], "opened_at_ms": t["opened_at_ms"],
            "filled_at": t["filled_at"], "filled_at_ms": t["filled_at_ms"],
            "resolved_at": t["resolved_at"], "resolved_at_ms": t["resolved_at_ms"],
        } for t in trades],
    })


