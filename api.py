"""
api.py — read-only HTTP surface for the frontend's LIVE and ANALYSIS modes.

Run with:
    uvicorn api:app --reload --port 8000

Design rules baked in here:

* Read-only except POST /analyze, which only triggers a backtest job, and POST
  /predict, which writes nothing at all. There is no order execution anywhere in
  this file.
* THREE ways to get a signal, and they never mix. POST /predict runs the pipeline
  once on live in-memory candles and persists nothing ("Run Analysis"). POST
  /analyze queues a real backtest over a date range and stores it in
  analysis_runs.db ("Run backtest"). /live/* reports what the separate
  live_runner process computed, via live_state.db.
* LIVE and ANALYSIS never share storage. Live reads live_state.db through
  live_store; analysis reads analysis_runs.db through analysis_store. They are
  separate files, so a live query cannot surface analysis rows or vice versa.
* Every payload carries explicit symbol / timeframe / timestamp fields, and every
  timestamp is repeated as `*_ms` epoch milliseconds for direct use as a chart
  x-coordinate.
* CHARTS and ANALYSIS have different reach. /ohlc will serve any symbol Bybit
  lists, falling back to a lightweight on-demand fetch through market_data when
  there is no local CSV. Full pipeline analysis needs local 1D/4H/1H history, so
  it stays limited to the symbols that have it — /symbols reports which is which
  via `analysis_available`.
* CHARTS are live, OVERLAYS are not. /ohlc serves live Bybit candles at every
  timeframe by default, while bias / zones / S-R levels come from live_store and
  lag by however far behind live_runner is. Every overlay endpoint therefore
  carries an `overlay_freshness` block saying when it was computed, so the
  frontend can show a "overlays may lag live price" note instead of implying the
  structural analysis is real-time.
* LIVE VIEW vs ANALYSIS MODE. /ohlc and /live/* are the live view: no date
  range, every Bybit symbol, every Bybit interval, as much history as Bybit will
  give. POST /analyze is the only place a date range or stored CSV history
  matters. live_state.db is a short rolling hand-off buffer between the
  live_runner process and this one, not an archive — GET /health reports its
  retention window and current contents.
"""

import math
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import analysis_store
import backtest
import live_runner
import live_store
import market_data
import Signal_formatter
from phase4_risk_journal import TradeJournal
from pipeline import analyze_pair_with_bias

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

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
# — they come from live_store and lag live price, which is what the
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

app = FastAPI(
    title="Trading Bot API",
    description="LIVE state feed + on-demand ANALYSIS backtests. Read-only except POST /analyze.",
    version="1.0.0",
)

# Open CORS for local dev — tighten before exposing this beyond localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    freshness is the SNAPSHOT timestamp — the execution bar the analysis last ran
    on — not the CSV tail; a long-running live_runner is well ahead of what is on
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
        note = ("no overlay snapshot recorded yet — live_runner.py does not appear "
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
    502 when Bybit itself is the problem — never a 500.
    """
    if not market_data.is_supported_timeframe(timeframe):
        raise HTTPException(404, f"no data for {symbol} {timeframe} — "
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
    allow_nan=False, so an infinite profit factor (a no-loss run — the codebase's
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


# ---------- meta ----------

@app.get("/health")
def health():
    return {"status": "ok", "live_db": live_store.LIVE_DB,
            "analysis_db": analysis_store.ANALYSIS_DB,
            "live_retention": live_store.retention_info(),
            "market_data": market_data.cache_stats()}


@app.get("/symbols")
def symbols(refresh: bool = Query(False, description="force a Bybit instrument-list refresh")):
    """
    Every symbol the frontend can chart: Bybit's live tradable list (cached, see
    `bybit.refresh_seconds`) unioned with whatever local CSV history exists.

    Per symbol:
      timeframes         — everything /ohlc will serve, local or on-demand
      local_timeframes   — served straight from CSV, no network call
      analysis_available — true only if POST /analyze can run it, i.e. local
                           1D/4H/1H history exists. Chart-only symbols get
                           candles but no pipeline overlays.
    """
    local = _local_history()
    warning = None
    try:
        tradable = set(market_data.tradable_symbols(force_refresh=refresh))
    except market_data.MarketDataError as exc:
        tradable = set()
        warning = f"Bybit instrument list unavailable — local history only: {exc}"

    chart_tfs = set(market_data.SUPPORTED_TIMEFRAMES)
    out = []
    for sym in sorted(tradable | set(local)):
        local_tfs = local.get(sym, set())
        fetchable = chart_tfs if sym in tradable else set()
        out.append({
            "symbol": sym,
            "timeframes": sorted(local_tfs | fetchable, key=_tf_sort_key),
            "local_timeframes": sorted(local_tfs, key=_tf_sort_key),
            "analysis_available": _analysis_ready(sym, local_tfs),
            "chart_source": ("local+bybit" if (local_tfs and fetchable)
                             else ("bybit" if fetchable else "local")),
        })

    return {
        "count": len(out),
        "analysis_count": sum(1 for s in out if s["analysis_available"]),
        "analysis_timeframes": list(ANALYSIS_TFS),
        "chart_timeframes": sorted(chart_tfs, key=_tf_sort_key),
        "bybit": market_data.symbol_cache_info(),
        "warning": warning,
        "symbols": out,
    }


# ---------- LIVE ----------

@app.get("/live/state")
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
                "message": "no live ticks recorded yet — is live_runner.py running?",
                "timeframes": {}, "recent_skips": [], "skip_reason_counts": {},
                "overlay_freshness": _overlay_freshness(symbol)}

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
        "latest_skip_reason": tick["skip_reason"],
        "latest_skip_reason_raw": tick["skip_reason_raw"],
        "recent_skips": live_store.recent_skips(symbol, limit=skip_history),
        "skip_reason_counts": live_store.skip_reason_counts(symbol),
    }


@app.get("/live/zones")
def live_zones(symbol: str = Query(...), timeframe: str = Query(None)):
    """
    Active FVG/imbalance zones and consolidation zone boundaries as price ranges
    with start/end timestamps. Omit `timeframe` for every timeframe at once.

    These are drawn over live chart candles, so `overlay_freshness` reports the
    bar they were computed on — check `stale` before presenting them as current.
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


@app.get("/live/levels")
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


@app.get("/live/stats")
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


# ---------- OHLC (charting) ----------

@app.get("/ohlc")
def ohlc(symbol: str = Query(...), timeframe: str = Query("1H"),
         limit: int = Query(market_data.DEFAULT_CHART_BARS, ge=1,
                            le=market_data.MAX_CHART_BARS,
                            description="bars to return; defaults to a full chart"),
         source: str = Query("auto", pattern="^(auto|local|bybit)$")):
    """
    Recent candles for charting, oldest -> newest.

    LIVE VIEW endpoint: no date range, no tracked-symbol restriction. Works for
    any symbol Bybit lists, at any interval Bybit offers (1M/3M/5M/15M/30M,
    1H/2H/4H/6H/12H, 1D, 1W, 1MO — note 1M is one MINUTE, 1MO is one month).
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
            notes.append(f"bybit unavailable ({exc.detail}) — served local CSV "
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
                     f"fewer than the {limit} requested — that is all the "
                     f"history available, not a truncation")

    return {"symbol": symbol, "timeframe": timeframe,
            "source": resolved, "has_local_history": has_local,
            "analysis_available": _analysis_ready(symbol),
            "requested_limit": limit, "max_limit": market_data.MAX_CHART_BARS,
            "note": "; ".join(notes) or None,
            "count": len(candles), "candles": candles}


# ---------- ANALYSIS ----------

def _run_analysis_job(job_id, symbol, start_date, end_date, step):
    """
    Background worker. Reuses backtest.run_symbol unchanged and writes results
    only to analysis_runs.db — it never opens live_state.db.
    """
    try:
        analysis_store.mark_running(job_id)
        journal = TradeJournal()
        counters, by_dir, _ = backtest.run_symbol(
            symbol, journal, step=step, trade_id_start=0, verbose=False,
            start_date=start_date, end_date=end_date,
        )

        df = journal.to_dataframe()
        closed = df[df["outcome"].isin(["win", "loss"])] if not df.empty else df

        trades = []
        for _, r in df.iterrows():
            trades.append({
                "trade_id": r["trade_id"], "symbol": r["pair"],
                "timeframe": backtest.EXECUTION_TF, "direction": r["direction"],
                "signal_ts": r["timestamp"], "entry_price": r["entry_price"],
                "stop_price": r["stop_price"], "take_profit": r["take_profit"],
                "stop_distance": r["stop_distance"], "planned_rr": r["planned_rr"],
                "realized_rr": r["realized_rr"], "outcome": r["outcome"],
                "pnl": r["pnl"], "notes": r["notes"],
            })

        pf, pf_inf = _split_pf(backtest.rr_profit_factor(closed) if not closed.empty else None)
        summary = {
            "source": "analysis",
            "symbol": symbol,
            "total_trades": int(len(closed)),
            "wins": int((closed["outcome"] == "win").sum()) if not closed.empty else 0,
            "losses": int((closed["outcome"] == "loss").sum()) if not closed.empty else 0,
            "win_rate": float((closed["outcome"] == "win").mean()) if not closed.empty else None,
            "avg_rr": float(closed["realized_rr"].mean()) if not closed.empty else None,
            "profit_factor_R": pf,
            "profit_factor_R_infinite": pf_inf,   # True = wins but zero losses
            "by_direction": {
                side: {
                    "trades": s["win"] + s["loss"],
                    "wins": s["win"],
                    "win_rate": (s["win"] / (s["win"] + s["loss"])
                                 if (s["win"] + s["loss"]) else None),
                    "avg_rr": (s["rr"] / (s["win"] + s["loss"])
                               if (s["win"] + s["loss"]) else None),
                } for side, s in by_dir.items()
            },
        }

        funnel = {
            "test_points": counters["test_points"],
            "insufficient_htf": counters["insufficient_htf"],
            "all_slots_busy": counters["position_open"],
            "zone_already_held": counters["zone_occupied"],
            "level_in_cooldown": counters["cooldown"],
            "no_fill": counters["no_fill"],
            "timeout": counters["timeouts"],
            "trades_resolved": counters["trades"],
            "skips": counters["skips"],
            "errors": counters["errors"],
            "opposing_concurrent": counters["opposing_concurrent"],
        }

        analysis_store.save_result(job_id, trades, _json_safe(summary), _json_safe(funnel))
    except Exception:
        analysis_store.mark_error(job_id, traceback.format_exc())


@app.post("/analyze")
def analyze(symbol: str = Query(...),
            start_date: str = Query(None, description="ISO date, inclusive"),
            end_date: str = Query(None, description="ISO date, inclusive"),
            step: int = Query(4, ge=1, le=200,
                              description="bars between test points; higher = faster, coarser")):
    """
    Kick off an on-demand backtest. Returns a job_id immediately; poll
    GET /analyze/status/{job_id} for progress and results.

    Writes only to analysis_runs.db — live state is never touched.
    """
    symbol = _clean_symbol(symbol)
    # load_data reads all three off disk, so require all three up front rather
    # than letting the background job die on a missing 4H/1D file. This is the
    # same condition /symbols reports as analysis_available.
    for tf in ANALYSIS_TFS:
        _require_csv(symbol, tf)

    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if value:
            try:
                pd.Timestamp(value)
            except (ValueError, TypeError):
                raise HTTPException(400, f"invalid {label} {value!r}")

    job_id = analysis_store.create_job(
        symbol, timeframe=backtest.EXECUTION_TF, start_date=start_date,
        end_date=end_date, step=step,
    )
    _EXECUTOR.submit(_run_analysis_job, job_id, symbol, start_date, end_date, step)

    return {"job_id": job_id, "status": analysis_store.QUEUED, "symbol": symbol,
            "start_date": start_date, "end_date": end_date, "step": step,
            "poll": f"/analyze/status/{job_id}"}


@app.get("/analyze/status/{job_id}")
def analyze_status(job_id: str, include_trades: bool = Query(True)):
    """
    Job status plus, once done, resolved trades (chart markers) and summary stats.
    status: queued | running | done | error
    """
    job = analysis_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job_id {job_id}")

    payload = {
        "job_id": job["job_id"], "status": job["status"], "symbol": job["symbol"],
        "timeframe": job["timeframe"], "start_date": job["start_date"],
        "end_date": job["end_date"], "step": job["step"],
        "created_at": job["created_at"], "started_at": job["started_at"],
        "finished_at": job["finished_at"], "error": job["error"],
        "summary": job["summary"], "funnel": job["funnel"],
        "trades": [],
    }

    if job["status"] == analysis_store.DONE and include_trades:
        payload["trades"] = [{
            "trade_id": t["trade_id"], "symbol": t["symbol"],
            "timeframe": t["timeframe"], "direction": t["direction"],
            "outcome": t["outcome"],
            "signal_ts": t["signal_ts"], "signal_ts_ms": t["signal_ts_ms"],
            "entry_price": t["entry_price"], "stop_price": t["stop_price"],
            "take_profit": t["take_profit"],
            "planned_rr": t["planned_rr"], "realized_rr": t["realized_rr"],
            "pnl": t["pnl"], "notes": t["notes"],
        } for t in analysis_store.get_job_trades(job_id)]

    return _json_safe(payload)


@app.get("/analyze/jobs")
def analyze_jobs(limit: int = Query(25, ge=1, le=200)):
    """Recent analysis jobs, newest first."""
    return {"jobs": analysis_store.list_jobs(limit)}


# ---------- PREDICT (run the pipeline once on live data) ----------

class PredictRequest(BaseModel):
    symbol: str = Field(..., description="any symbol Bybit lists, e.g. BTCUSDT")
    timeframe: str = Field(backtest.EXECUTION_TF,
                           description="execution timeframe, e.g. 1H / 15M / 5M")


def _predict_bars(tf: str, is_execution: bool) -> int:
    """
    How many bars to pull for one timeframe.

    Bias depth mirrors live_runner.SEED_DAYS so /predict computes bias over the
    same window live_runner would — ema_trend_filter derives its flat/steep
    threshold from the std of whatever frame it is handed, so a different depth
    can yield a different bias on identical candles.
    """
    minutes = market_data.timeframe_minutes(tf)
    days = live_runner.SEED_DAYS.get(tf)
    if days and minutes:
        bars = min(live_runner.MAX_ROWS, max(1, round(days * 24 * 60 / minutes)))
    else:
        bars = live_runner.MAX_ROWS
    return max(bars, PREDICT_EXEC_BARS) if is_execution else bars


def _live_frames(symbol: str, execution_tf: str) -> dict:
    """
    Build {timeframe: DataFrame} from LIVE Bybit candles held in memory.

    Same data source live_runner seeds from (ingestion_bybit REST -> DataFrame,
    via market_data's cache) and deliberately NOT data/*.csv or either database.
    Raises HTTPException with an explicit message rather than propagating a
    Bybit/pandas error.
    """
    wanted = list(PREDICT_BIAS_TFS)
    if execution_tf not in wanted:
        wanted.append(execution_tf)

    frames = {}
    for tf in wanted:
        if not market_data.is_supported_timeframe(tf):
            raise HTTPException(400, f"timeframe {tf!r} cannot be fetched live from "
                                     f"Bybit; live-capable timeframes: "
                                     f"{', '.join(market_data.SUPPORTED_TIMEFRAMES)}")
        try:
            frames[tf] = market_data.recent_candles(
                symbol, tf, _predict_bars(tf, tf == execution_tf))
        except market_data.SymbolNotFound as exc:
            raise HTTPException(404, f"no live data for {symbol} {tf}: {exc}") from exc
        except market_data.MarketDataError as exc:
            raise HTTPException(
                503, f"live market data unavailable for {symbol} {tf} — cannot "
                     f"predict without it: {exc}") from exc
    return frames


@app.post("/predict")
def predict(req: PredictRequest):
    """
    Run the signal pipeline ONCE against current live candles and return what it
    decides. This is "Run Analysis", not "Run backtest".

    Distinct from POST /analyze in every respect: no date range, no job queue, no
    stored history, and no persistence. Candles come from live Bybit into memory
    (the same source live_runner.py seeds from — never data/*.csv, never
    analysis_runs.db), the pipeline runs synchronously, and the result is returned
    without a row being written anywhere. pipeline's signal log is suppressed via
    db_path=None, so signals.db is untouched too.

    Works for any symbol Bybit lists. Note that unlike /live/state this needs no
    live_runner process at all — it fetches its own frames — so it answers for
    chart-only symbols as well as the analysis-tracked ones.

    A pipeline "skip" is a valid outcome, returned as HTTP 200 with
    signal_fired=false and the reason. Errors are reserved for genuinely being
    unable to run: 400 bad input, 404 unknown symbol/timeframe, 422 not enough
    live history yet, 503 Bybit unreachable, 500 unexpected pipeline failure.
    """
    symbol = _clean_symbol(req.symbol)
    timeframe = _clean_timeframe(req.timeframe) or backtest.EXECUTION_TF

    frames = _live_frames(symbol, timeframe)

    # "No live buffer yet" surfaces here: a freshly listed coin can be tradable
    # and still have too few bars for the structure engine to say anything.
    exec_df = frames[timeframe]
    if len(exec_df) < backtest.LOOKBACK_BARS:
        raise HTTPException(
            422, f"not enough live history for {symbol} {timeframe}: "
                 f"{len(exec_df)} bars available, {backtest.LOOKBACK_BARS} needed. "
                 f"Bybit has no deeper history for this pair yet.")
    for tf in PREDICT_BIAS_TFS:
        if len(frames[tf]) < PREDICT_MIN_BIAS_BARS:
            raise HTTPException(
                422, f"not enough live {tf} history for {symbol} to resolve bias: "
                     f"{len(frames[tf])} bars available, "
                     f"{PREDICT_MIN_BIAS_BARS} needed.")

    recent = exec_df.tail(backtest.LOOKBACK_BARS)
    ts = exec_df.index[-1]

    # Exactly live_runner.run_analysis's call shape: bias from 1D/4H/1H frames,
    # entry/SL/TP from the execution timeframe's OHLCV.
    bias_frames = {tf: frames[tf] for tf in PREDICT_BIAS_TFS}
    try:
        result = analyze_pair_with_bias(
            pair=symbol,
            timeframe=timeframe,
            df_by_tf=bias_frames,
            opens=recent["open"].to_numpy(dtype=float),
            highs=recent["high"].to_numpy(dtype=float),
            lows=recent["low"].to_numpy(dtype=float),
            closes=recent["close"].to_numpy(dtype=float),
            volumes=recent["volume"].to_numpy(dtype=float),
            db_path=None,               # stateless: do not log this signal
        )
    except HTTPException:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(
            500, f"pipeline failed for {symbol} {timeframe}: "
                 f"{type(exc).__name__}: {exc}") from exc

    skipped = result.get("skipped") if isinstance(result, dict) else None
    fired = skipped is None

    return _json_safe({
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": _iso(ts),
        "timestamp_ms": _ms(ts),
        "source": "bybit_live_memory",
        "persisted": False,
        "analysis_available": _analysis_ready(symbol),
        "signal_fired": fired,
        # Same classified categories the live funnel uses, so a /predict skip and
        # a /live/state skip read identically.
        "skip_reason": None if fired else backtest.classify_skip(skipped),
        "skip_reason_raw": skipped,
        "signal": result if fired else None,
        "bars_used": {tf: {"bars": int(len(df)),
                           "last_bar": _iso(df.index[-1]) if len(df) else None}
                      for tf, df in frames.items()},
        "execution_bars": int(len(recent)),
    })
