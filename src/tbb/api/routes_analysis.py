"""tbb.api.routes_analysis — queued date-bounded backtests (/analyze*)."""

import traceback

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from tbb.analysis.risk_journal import TradeJournal
from tbb.backtesting import backtest
from tbb.storage import analysis_store
from tbb.api.common import ANALYSIS_TFS, _EXECUTOR, _clean_symbol, _json_safe, _require_csv, _split_pf
from tbb import logsetup

log = logsetup.get_logger("api")
router = APIRouter()

# ---------- ANALYSIS ----------

def _run_analysis_job(job_id, symbol, start_date, end_date, step):
    """
    Background worker. Reuses backtest.run_symbol unchanged and writes results
    only to analysis_runs.db â€” it never opens live_state.db.
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
        log.error("analysis job %s failed:\n%s", job_id, traceback.format_exc())


@router.post("/analyze")
def analyze(symbol: str = Query(...),
            start_date: str = Query(None, description="ISO date, inclusive"),
            end_date: str = Query(None, description="ISO date, inclusive"),
            step: int = Query(4, ge=1, le=200,
                              description="bars between test points; higher = faster, coarser")):
    """
    Kick off an on-demand backtest. Returns a job_id immediately; poll
    GET /analyze/status/{job_id} for progress and results.

    Writes only to analysis_runs.db â€” live state is never touched.
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


@router.get("/analyze/status/{job_id}")
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


@router.get("/analyze/jobs")
def analyze_jobs(limit: int = Query(25, ge=1, le=200)):
    """Recent analysis jobs, newest first."""
    return {"jobs": analysis_store.list_jobs(limit)}


