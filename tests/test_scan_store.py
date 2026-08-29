"""
Black-box checks for scan_store (the /scan job + results store) using a scratch
database. No network, no pipeline - pure storage behaviour.

Run:  python tests/test_scan_store.py
"""

import os
import sys
import tempfile

# Point the store at a scratch file BEFORE importing tbb.config (env is read at
# import time), mirroring how test_api.py overrides LIVE_DB_PATH etc.
_SCRATCH = os.path.join(tempfile.gettempdir(), "tbb_scan_store_test.db")
if os.path.exists(_SCRATCH):
    os.remove(_SCRATCH)
os.environ["SCAN_DB_PATH"] = _SCRATCH

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from tbb.storage import scan_store  # noqa: E402


def main():
    # --- lifecycle: queued -> running -> done ---
    job_id = scan_store.create_job(quote="USDT", timeframe="1H",
                                   params={"requested": "all_USDT", "limit": None})
    assert job_id, "create_job returned an empty job_id"
    job = scan_store.get_job(job_id)
    assert job["status"] == "queued" and job["quote"] == "USDT", job
    assert job["params"] == {"requested": "all_USDT", "limit": None}, job
    assert job["created_at_ms"] is not None, job

    scan_store.mark_running(job_id, symbols_total=3)
    job = scan_store.get_job(job_id)
    assert job["status"] == "running" and job["symbols_total"] == 3, job
    assert job["started_at_ms"] is not None, job

    active = scan_store.active_job()
    assert active and active["job_id"] == job_id, active

    # --- results: fired / skipped / failed rows ---
    scan_store.update_progress(job_id, current_symbol="BTCUSDT")
    scan_store.add_result(job_id, {
        "symbol": "BTCUSDT", "timeframe": "1H",
        "predicted_at": "2026-01-01T00:00:00+00:00",
        "bar_ts": "2026-01-01T00:00:00+00:00",
        "signal_fired": True, "direction": "LONG",
        "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0,
        "confluence_score": 72.5, "confidence": "HIGH",
        "signal": {"direction": "LONG", "confluence_score": 72.5},
    })
    scan_store.add_result(job_id, {
        "symbol": "ETHUSDT", "timeframe": "1H",
        "predicted_at": "2026-01-01T00:00:05+00:00",
        "signal_fired": False, "skip_reason": "bias_gate",
        "skip_reason_raw": "bias not tradable",
    })
    scan_store.add_result(job_id, {
        "symbol": "BADUSDT", "timeframe": "1H",
        "predicted_at": "2026-01-01T00:00:10+00:00",
        "signal_fired": False, "error": "pipeline failed: ValueError: x",
    })
    scan_store.update_progress(job_id, symbols_done=3, signals_fired=1,
                               skipped=1, failed=1)

    rows = scan_store.get_results(job_id)
    assert len(rows) == 3 and [r["symbol"] for r in rows] == [
        "BTCUSDT", "ETHUSDT", "BADUSDT"], rows  # scan order preserved
    assert rows[0]["signal_fired"] is True and rows[0]["direction"] == "LONG"
    assert rows[0]["predicted_at_ms"] is not None and rows[0]["bar_ts_ms"] is not None
    assert rows[0]["signal"]["direction"] == "LONG", rows[0]["signal"]
    assert rows[1]["skip_reason"] == "bias_gate" and rows[1]["signal"] is None
    assert rows[2]["error"] and "ValueError" in rows[2]["error"]

    fired = scan_store.get_results(job_id, fired=True)
    assert len(fired) == 1 and fired[0]["symbol"] == "BTCUSDT", fired
    not_fired = scan_store.get_results(job_id, fired=False)
    assert len(not_fired) == 2, not_fired

    # --- REPLACE idempotency: re-adding the same symbol updates, not duplicates ---
    scan_store.add_result(job_id, {
        "symbol": "BTCUSDT", "timeframe": "1H",
        "predicted_at": "2026-01-01T01:00:00+00:00", "signal_fired": True,
        "direction": "SHORT", "rr": 2.0,
    })
    rows = scan_store.get_results(job_id)
    assert len(rows) == 3, len(rows)
    btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
    assert btc["direction"] == "SHORT" and btc["rr"] == 2.0, btc

    # --- finish paths ---
    scan_store.mark_done(job_id)
    job = scan_store.get_job(job_id)
    assert job["status"] == "done" and job["finished_at_ms"] is not None, job
    assert scan_store.active_job() is None
    assert scan_store.latest_job_id() == job_id

    stop_id = scan_store.create_job(quote="USDT", timeframe="15M")
    scan_store.mark_running(stop_id, symbols_total=10)
    scan_store.mark_stopped(stop_id)
    assert scan_store.get_job(stop_id)["status"] == "stopped"
    assert scan_store.latest_job_id() == stop_id  # newest by created_at

    err_id = scan_store.create_job(quote="USDT", timeframe="1H")
    scan_store.mark_error(err_id, "boom")
    assert scan_store.get_job(err_id)["status"] == "error"

    jobs = scan_store.list_jobs(limit=10)
    assert len(jobs) == 3 and jobs[0]["job_id"] == err_id, jobs

    # --- db_path override resolves at call time (tests point at scratch files) ---
    override = os.path.join(tempfile.gettempdir(), "tbb_scan_store_test2.db")
    if os.path.exists(override):
        os.remove(override)
    scan_store.init_db(override)
    other_id = scan_store.create_job(db_path=override)
    assert scan_store.get_job(other_id, db_path=override)["job_id"] == other_id
    assert scan_store.latest_job_id(db_path=override) == other_id
    os.remove(override)

    print("scan_store: all checks passed (3 jobs, result rows, fired filter, "
          "replace idempotency, lifecycle, override DB)")


if __name__ == "__main__":
    main()