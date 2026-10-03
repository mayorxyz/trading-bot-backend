"""
tbb.api.routes_scan - API-triggered multi-symbol predict loop (/scan*).

The backend behind the frontend's Results page: POST /scan starts a sequential
predict pass over every requested symbol (default: all Bybit spot pairs quoted
in the requested quote coin), GET /scan/status/{job_id} reports progress and
per-symbol rows, GET /scan/results serves the latest scan's rows, and
POST /scan/stop ends a run cooperatively after its current symbol.

Each symbol runs EXACTLY the POST /predict path - same fetch depth, same frame
checks, same analyze_pair_with_bias call with db_path=None - so a scanned
symbol and a single predicted symbol cannot disagree. The only persistence is
scan_runs.db (scan_store.py): live_state.db, analysis_runs.db, signals.db and
CSV history are never touched.
"""

import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from tbb import logsetup
from tbb.backtesting import backtest
from tbb.live import scanner
from tbb.live import runner as live_runner
from tbb.marketdata import market_data
from tbb.pipeline import analyze_pair_with_bias
from tbb.storage import scan_store
from tbb.api.common import (
    PREDICT_BIAS_TFS, PREDICT_EXEC_BARS, PREDICT_MIN_BIAS_BARS,
    _clean_symbol, _clean_timeframe, _iso, _json_safe,
)

log = logsetup.get_logger("api")
router = APIRouter()

# One scan at a time. The loop is a long, rate-limited walk over the whole
# quote book, so a second POST /scan gets an immediate 409 instead of queueing
# behind a 40-minute job. _SCAN_EXECUTOR's single worker enforces the same
# guarantee from the other side; _SCAN_LOCK is what the route itself checks.
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan")
_SCAN_LOCK = threading.Lock()
_STOP_EVENTS = {}   # job_id -> threading.Event, checked between symbols
_PAUSE_EVENTS = {}  # job_id -> threading.Event

@router.post("/scan/pause")
def scan_pause():
    for jid, event in list(_STOP_EVENTS.items()):
        if not event.is_set():
            _PAUSE_EVENTS.setdefault(jid, threading.Event()).set()
            return {"paused": jid, "status": "paused"}
    return {"paused": None, "note": "no scan is running"}

@router.post("/scan/resume")
def scan_resume():
    for jid, event in list(_PAUSE_EVENTS.items()):
        if event.is_set():
            event.clear()
            return {"resumed": jid, "status": "running"}
    return {"resumed": None, "note": "scan is not paused"}

# Same rate-limit courtesy between symbols as tbb/live/scanner.py.
SYMBOL_PAUSE_SECONDS = 0.5

QUOTE_RE = re.compile(r"^[A-Za-z0-9]{2,10}$")


def _now_pair():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp() * 1000)


def _predict_bars(tf: str, is_execution: bool) -> int:
    """
    How many bars to pull for one timeframe - the identical depth POST /predict
    uses (routes_predict._predict_bars): bias depth mirrors live_runner.SEED_DAYS
    so scanned, predicted and live symbols all compute bias over the same window.
    """
    minutes = market_data.timeframe_minutes(tf)
    days = live_runner.SEED_DAYS.get(tf)
    if days and minutes:
        bars = min(live_runner.MAX_ROWS, max(1, round(days * 24 * 60 / minutes)))
    else:
        bars = live_runner.MAX_ROWS
    return max(bars, PREDICT_EXEC_BARS) if is_execution else bars


def _scan_symbol(symbol: str, timeframe: str) -> dict:
    """
    One predict for one symbol on live candles, in memory. Never raises for
    expected data conditions: shortages become skips, fetch errors become
    no-data skips, unexpected crashes become per-symbol failures - one bad
    symbol must never end the loop.
    """
    predicted_at, predicted_at_ms = _now_pair()
    row = {
        "symbol": symbol, "timeframe": timeframe,
        "predicted_at": predicted_at, "predicted_at_ms": predicted_at_ms,
        "bar_ts": None, "bar_ts_ms": None,
        "signal_fired": False, "skip_reason": None, "skip_reason_raw": None,
        "direction": None, "entry": None, "sl": None, "tp": None, "rr": None,
        "confluence_score": None, "confidence": None, "signal": None, "error": None,
        "confluence_breakdown_json": None, "skip_score": None,
    }

    # Fetch bias frames and the execution frame INDEPENDENTLY. When the
    # execution timeframe is also one of PREDICT_BIAS_TFS (e.g. 1H), a single
    # per-TF loop collapses both roles onto one frame via tf == timeframe, so
    # the bias window would get execution depth instead of bias depth and a
    # scanned symbol could disagree with a standalone POST /predict on the
    # same candles. Two calls keep each role at its own depth; market_data's
    # cache serves both from one Bybit page when depths differ.
    bias_frames = {}
    try:
        for tf in PREDICT_BIAS_TFS:
            bias_frames[tf] = market_data.recent_candles(
                symbol, tf, _predict_bars(tf, False))
        exec_df = market_data.recent_candles(
            symbol, timeframe, _predict_bars(timeframe, True))
    except market_data.SymbolNotFound as exc:
        row["skip_reason"] = "no_live_data"
        row["skip_reason_raw"] = str(exc)
        return row
    except market_data.MarketDataError as exc:
        row["skip_reason"] = "market_data_unavailable"
        row["skip_reason_raw"] = str(exc)
        return row

    row["bar_ts"] = _iso(exec_df.index[-1]) if len(exec_df) else None
    if len(exec_df) < backtest.LOOKBACK_BARS:
        row["skip_reason"] = "insufficient_history"
        row["skip_reason_raw"] = (f"insufficient history for {symbol} {timeframe}: "
                                  f"{len(exec_df)} bars available, "
                                  f"{backtest.LOOKBACK_BARS} needed")
        return row
    for tf in PREDICT_BIAS_TFS:
        if len(bias_frames[tf]) < PREDICT_MIN_BIAS_BARS:
            row["skip_reason"] = "insufficient_history"
            row["skip_reason_raw"] = (f"insufficient live {tf} history for {symbol}: "
                                      f"{len(bias_frames[tf])} bars available, "
                                      f"{PREDICT_MIN_BIAS_BARS} needed")
            return row

    recent = exec_df.tail(backtest.LOOKBACK_BARS)
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
            db_path=None,               # stateless per symbol: signals.db untouched
        )
    except Exception as exc:
        row["error"] = f"pipeline failed: {type(exc).__name__}: {exc}"
        return row

    skipped = result.get("skipped") if isinstance(result, dict) else None
    if skipped is not None:
        # Same classified categories the live funnel and /predict use.
        row["skip_reason"] = backtest.classify_skip(skipped)
        row["skip_reason_raw"] = skipped
        # Logging only: keep the confluence breakdown/score for low-conviction
        # skips. Other skip kinds (bias/entry/invalid-trade) have no breakdown
        # and keep NULL for both. Fired-signal path below is untouched.
        breakdown = result.get("confluence_breakdown") if isinstance(result, dict) else None
        if isinstance(breakdown, dict) and breakdown:
            score = result.get("confluence_score")
            if not isinstance(score, (int, float)):
                m = re.search(r"confluence (\d+)/100", skipped or "")
                score = int(m.group(1)) if m else None
            row["skip_score"] = score
            row["confluence_breakdown_json"] = json.dumps(breakdown, default=str)
        return row

    row.update({
        "signal_fired": True,
        "direction": result.get("direction"),
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "rr": result.get("rr"),
        "confluence_score": result.get("confluence_score"),
        "confidence": result.get("confidence"),
        "signal": result,
    })
    return row


def _run_scan_job(job_id, symbols, timeframe, quote):
    """
    Background worker: one sequential predict pass over `symbols`, one
    scan_results row per symbol, job row updated as it goes. Releases
    _SCAN_LOCK whatever the outcome, so the next POST /scan always gets through.
    """
    try:
        scan_store.mark_running(job_id, symbols_total=len(symbols))
        done = fired = skipped = failed = 0

        for n, symbol in enumerate(symbols, 1):
            stop_event = _STOP_EVENTS.get(job_id)
            pause_event = _PAUSE_EVENTS.get(job_id)

            if stop_event is not None and stop_event.is_set():
                scan_store.mark_stopped(job_id)
                log.info("scan %s: stopped by request at %d/%d",
                         job_id, n - 1, len(symbols))
                return

            if pause_event is not None and pause_event.is_set():
                scan_store.mark_paused(job_id)
                log.info("scan %s: paused before symbol %s", job_id, symbol)
                while pause_event.is_set():
                    if stop_event is not None and stop_event.is_set():
                        scan_store.mark_stopped(job_id)
                        return
                    time.sleep(0.25)
                scan_store.mark_running(job_id)
                log.info("scan %s: resumed", job_id)

            scan_store.update_progress(job_id, current_symbol=symbol)
            row = _scan_symbol(symbol, timeframe)

            if row["error"]:
                failed += 1
            elif row["signal_fired"]:
                fired += 1
            else:
                skipped += 1

            scan_store.add_result(job_id, row)
            done += 1
            scan_store.update_progress(job_id, symbols_done=done,
                                       signals_fired=fired,
                                       skipped=skipped,
                                       failed=failed)

            if n < len(symbols):
                time.sleep(SYMBOL_PAUSE_SECONDS)
                if _STOP_EVENTS.get(job_id) and _STOP_EVENTS[job_id].is_set():
                    scan_store.mark_stopped(job_id)
                    return

        scan_store.mark_done(job_id)
    except Exception:
        log.error("scan job %s failed:\n%s", job_id, traceback.format_exc())
        scan_store.mark_error(job_id, traceback.format_exc())
    finally:
        _STOP_EVENTS.pop(job_id, None)
        _PAUSE_EVENTS.pop(job_id, None)
        if _SCAN_LOCK.locked():
            _SCAN_LOCK.release()


# ---------- SCAN routes ----------

@router.post("/scan")
def start_scan(quote: str = Query("USDT", description="quote coin filter for discovery"),
               timeframe: str = Query(backtest.EXECUTION_TF,
                                      description="execution timeframe each symbol is predicted on"),
               symbols: str = Query(None, description="comma-separated whitelist; default = every tradable {quote} pair"),
               limit: int = Query(None, ge=1, le=2000,
                                  description="cap the discovered list (quick tests / top-N)")):
    """
    Start one multi-symbol predict loop. Returns a job_id immediately; poll
    GET /scan/status/{job_id} for progress and GET /scan/results for rows.

    One scan at a time (409 while one is queued/running). Writes only to
    scan_runs.db - live state, analysis jobs and the signal ledger are untouched.
    """
    timeframe = _clean_timeframe(timeframe) or backtest.EXECUTION_TF
    if not market_data.is_supported_timeframe(timeframe):
        raise HTTPException(400, f"timeframe {timeframe!r} cannot be fetched live "
                                 f"from Bybit; fetchable: "
                                 f"{', '.join(market_data.SUPPORTED_TIMEFRAMES)}")
    if not QUOTE_RE.match(quote or ""):
        raise HTTPException(400, f"invalid quote coin {quote!r}")

    whitelist = None
    if symbols:
        whitelist, seen = [], set()
        for part in symbols.split(","):
            part = part.strip()
            if not part:
                continue
            sym = _clean_symbol(part)
            if sym not in seen:
                seen.add(sym)
                whitelist.append(sym)
        if not whitelist:
            raise HTTPException(400, "symbols parameter produced no usable symbols")

    if not _SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a scan is already running - check GET /scan/jobs "
                                 "or POST /scan/stop first")

    submitted = False
    try:
        if whitelist:
            resolved = whitelist
        else:
            try:
                resolved = scanner.get_symbols(quote)
            except Exception as exc:
                raise HTTPException(503, f"symbol discovery failed: {exc}") from exc
            if limit:
                resolved = resolved[:limit]
        if not resolved:
            raise HTTPException(503, f"no Trading spot instruments quoted in {quote}")

        job_id = scan_store.create_job(
            quote=quote, timeframe=timeframe, symbols_total=len(resolved),
            params={"requested": "explicit_list" if whitelist else f"all_{quote}",
                    "limit": limit},
        )
        _STOP_EVENTS[job_id] = threading.Event()
        _SCAN_EXECUTOR.submit(_run_scan_job, job_id, resolved, timeframe, quote)
        submitted = True
    finally:
        if not submitted:
            # The worker never started, so it cannot release the lock itself.
            if _SCAN_LOCK.locked():
                _SCAN_LOCK.release()

    return {
        "job_id": job_id,
        "status": scan_store.QUEUED,
        "quote": quote,
        "timeframe": timeframe,
        "symbols_total": len(resolved),
        "symbols_preview": resolved[:25],
        "poll": f"/scan/status/{job_id}",
        "note": "one scan at a time; results land per symbol as the loop runs",
    }


@router.get("/scan/status/{job_id}")
def scan_status(job_id: str,
                include_results: bool = Query(True),
                fired: bool = Query(None, description="true=only signals, false=only skips/failures"),
                limit: int = Query(500, ge=1, le=2000)):
    """
    Job state plus live progress (symbols_done / symbols_total / current_symbol)
    and, when include_results, the per-symbol rows gathered so far. Poll with
    include_results=false while running for a light payload, then fetch
    GET /scan/results once at the end.
    """
    job = scan_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job_id {job_id}")
    payload = _json_safe(job)
    payload["results"] = []
    payload["result_count"] = 0
    if include_results:
        rows = scan_store.get_results(job_id, fired=fired, limit=limit)
        payload["results"] = _json_safe(rows)
        payload["result_count"] = len(rows)
    return payload


@router.get("/scan/results")
def scan_results(job_id: str = Query(None, description="default: the newest scan job"),
                 fired: bool = Query(None, description="true=only signals, false=only skips/failures"),
                 limit: int = Query(500, ge=1, le=2000)):
    """
    The Results page feed: per-symbol predict rows (symbol, predicted_at, the
    trade plan or skip reason) from one scan, or the newest scan when job_id is
    omitted. Each fired row carries the full signal object.
    """
    resolved_job = job_id or scan_store.latest_job_id()
    if resolved_job is None:
        return {"job_id": None, "status": None, "summary": None,
                "message": "no scan has run yet - POST /scan first",
                "count": 0, "results": []}
    job = scan_store.get_job(resolved_job)
    rows = scan_store.get_results(resolved_job, fired=fired, limit=limit)
    return _json_safe({
        "job_id": resolved_job,
        "status": job["status"] if job else None,
        "summary": {k: job[k] for k in ("quote", "timeframe", "symbols_total",
                                        "symbols_done", "signals_fired", "skipped",
                                        "failed", "created_at", "finished_at")}
                   if job else None,
        "count": len(rows),
        "results": rows,
    })


@router.get("/scan/jobs")
def scan_jobs(limit: int = Query(25, ge=1, le=200)):
    """Recent scan jobs, newest first - progress counters included."""
    return {"jobs": scan_store.list_jobs(limit)}


@router.post("/scan/stop")
def scan_stop():
    """
    Ask the running scan to finish after its current symbol. Cooperative: the
    loop checks between symbols, marks the job stopped, and keeps every result
    already gathered. 200 with stopped=null when nothing is running.
    """
    for jid, event in list(_STOP_EVENTS.items()):
        if not event.is_set():
            event.set()
            return {"stopped": jid,
                    "note": "loop finishes its current symbol, marks the job "
                            "stopped, and keeps completed results"}
    return {"stopped": None, "note": "no scan is running"}