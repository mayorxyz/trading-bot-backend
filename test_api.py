"""
test_api.py — exercises every endpoint against a REAL running uvicorn server
(no TestClient, so no extra HTTP client dependency needed).

Start the server first, pointing it at the scratch DBs:

    LIVE_DB_PATH=live_state_test.db ANALYSIS_DB_PATH=analysis_runs_test.db \
        python -m uvicorn api:app --port 8111

then run this script. BASE can be overridden with API_BASE.

Also asserts the hard requirement that ANALYSIS never touches LIVE storage: live
row counts are snapshotted before the analyze job and compared after.

The chart-data checks hit Bybit for real. With no network they report 502 from
/ohlc (upstream unavailable) rather than 404 — that is the correct code, but it
does mean this script needs connectivity to pass in full.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8111")
SYMBOL = os.environ.get("API_SYMBOL", "BTCUSDT")
# A symbol Bybit lists but we keep no local history for — chart-only path.
CHART_ONLY_SYMBOL = os.environ.get("API_CHART_SYMBOL", "LINKUSDT")
SUB_HOURLY = ("1m", "5m", "15m", "30m")
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_DB = os.environ.get("LIVE_DB_PATH", os.path.join(HERE, "live_state_test.db"))


def call(method, path, timeout=30):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


FAILURES = []


def show(method, path, keys=None, limit=3):
    status, body = call(method, path)
    print(f"\n### {method} {path}  -> HTTP {status}")
    if status != 200:
        print("   ", body)
        FAILURES.append(f"{method} {path} returned HTTP {status}")
        return None
    if keys:
        for k in keys:
            v = body.get(k)
            if isinstance(v, list):
                print(f"    {k}: {len(v)} items; first {min(limit, len(v))}:")
                for item in v[:limit]:
                    print(f"      {item}")
            else:
                print(f"    {k}: {v}")
    else:
        print("   ", str(body)[:500])
    return body


def live_row_counts():
    conn = sqlite3.connect(LIVE_DB)
    out = {}
    for t in ("live_ticks", "live_tf_state", "live_zones", "live_levels", "live_trades"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    return out


def main():
    status, body = call("GET", "/health")
    if status != 200:
        print(f"server not reachable at {BASE}: {body}")
        print("start it with:\n  LIVE_DB_PATH=live_state_test.db "
              "ANALYSIS_DB_PATH=analysis_runs_test.db python -m uvicorn api:app --port 8111")
        sys.exit(1)

    print("=" * 72)
    print("LIVE + META ENDPOINTS")
    print("=" * 72)
    print("\n### GET /health -> HTTP 200\n   ", body)

    syms = show("GET", "/symbols",
                keys=["count", "analysis_count", "analysis_timeframes",
                      "chart_timeframes", "bybit", "warning", "symbols"], limit=3)

    show("GET", f"/live/state?symbol={SYMBOL}",
         keys=["symbol", "has_state", "timestamp", "timestamp_ms", "timeframes",
               "topdown_bias", "aligned_count", "bias_gate_passed", "signal_fired",
               "latest_skip_reason", "skip_reason_counts", "recent_skips"])

    show("GET", f"/live/zones?symbol={SYMBOL}&timeframe=1H",
         keys=["symbol", "timeframe", "count", "zones"], limit=3)
    show("GET", f"/live/zones?symbol={SYMBOL}", keys=["count"])
    show("GET", f"/live/levels?symbol={SYMBOL}&timeframe=1H",
         keys=["symbol", "timeframe", "count", "levels"], limit=5)
    show("GET", f"/live/stats?symbol={SYMBOL}",
         keys=["source", "total_trades", "in_flight_trades", "wins", "losses",
               "win_rate", "avg_rr", "profit_factor_R", "trades"], limit=2)
    show("GET", f"/ohlc?symbol={SYMBOL}&timeframe=1H&limit=3",
         keys=["symbol", "timeframe", "source", "count", "candles"], limit=3)

    print("\n" + "=" * 72)
    print("SYMBOL LIST (live Bybit instruments, not just local CSVs)")
    print("=" * 72)
    ok = True
    if syms:
        rows = syms["symbols"]
        n, n_analysis = syms["count"], syms["analysis_count"]
        # Local data/ holds a handful of symbols; the live list is in the hundreds.
        print(f"  {n} symbols, {n_analysis} with analysis_available")
        if n < 50:
            print(f"  FAIL — only {n} symbols; expected the full Bybit list. "
                  f"warning={syms.get('warning')}")
            ok = False
        if not 0 < n_analysis <= n:
            print(f"  FAIL — analysis_count {n_analysis} out of range")
            ok = False
        missing_flag = [r["symbol"] for r in rows if "analysis_available" not in r]
        if missing_flag:
            print(f"  FAIL — analysis_available missing on {len(missing_flag)} entries")
            ok = False
        tracked = next((r for r in rows if r["symbol"] == SYMBOL), None)
        if tracked is None or not tracked["analysis_available"]:
            print(f"  FAIL — {SYMBOL} should be analysis_available; got {tracked}")
            ok = False
        else:
            print(f"  {SYMBOL}: {tracked}")
        chart_only = next((r for r in rows if r["symbol"] == CHART_ONLY_SYMBOL), None)
        if chart_only is None:
            print(f"  FAIL — {CHART_ONLY_SYMBOL} absent from the list")
            ok = False
        elif chart_only["analysis_available"]:
            print(f"  FAIL — {CHART_ONLY_SYMBOL} has no local history, "
                  f"analysis_available should be False")
            ok = False
        else:
            print(f"  {CHART_ONLY_SYMBOL}: {chart_only}")
    else:
        ok = False

    print("\n" + "=" * 72)
    print("SUB-HOURLY OHLC (1m/5m/15m/30m must be 200, not 404)")
    print("=" * 72)
    for sym in (SYMBOL, CHART_ONLY_SYMBOL):
        for tf in SUB_HOURLY:
            st, b = call("GET", f"/ohlc?symbol={sym}&timeframe={tf}&limit=5")
            if st != 200 or not b.get("candles"):
                print(f"  {sym} {tf:3s} -> HTTP {st}  FAIL {str(b)[:160]}")
                ok = False
                continue
            last = b["candles"][-1]
            print(f"  {sym} {tf:3s} -> HTTP {st}  source={b['source']:9s} "
                  f"n={b['count']} last={last['ts']} close={last['close']}")
            if last["ts_ms"] is None:
                print(f"  FAIL — {sym} {tf} candle has no ts_ms")
                ok = False

    print("\n" + "=" * 72)
    print("CHART SOURCE (source=auto must serve LIVE bybit at every timeframe)")
    print("=" * 72)
    for tf in ("1m", "3m", "30m", "1h", "2h", "6h", "12h", "1d", "1w", "1mo"):
        st, b = call("GET", f"/ohlc?symbol={SYMBOL}&timeframe={tf}&limit=3")
        if st != 200:
            print(f"  {tf:4s} auto -> HTTP {st}  FAIL {str(b)[:160]}")
            ok = False
            continue
        good = b["source"] == "bybit"
        if not good:
            ok = False
        print(f"  {tf:4s} auto -> source={b['source']:9s} last={b['candles'][-1]['ts']}"
              f"  {'ok' if good else 'FAIL expected bybit'}")

    # source=local must still be honoured, and must stay behind live for the
    # tracked symbols whose CSVs are not being refreshed.
    st, b = call("GET", f"/ohlc?symbol={SYMBOL}&timeframe=1h&limit=3&source=local")
    if st != 200 or b["source"] != "local_csv":
        print(f"  1h  source=local -> HTTP {st} source={b.get('source')}  FAIL")
        ok = False
    else:
        print(f"  1h  source=local -> source={b['source']} last={b['candles'][-1]['ts']}  ok")

    print("\n" + "=" * 72)
    print("HISTORY DEPTH (must paginate past bybit's 1000/request, not truncate)")
    print("=" * 72)
    st, h = call("GET", "/health")
    md = h.get("market_data", {}) if isinstance(h, dict) else {}
    per_req = md.get("bars_per_request", 1000)
    print(f"  default={md.get('chart_bars_default')} max={md.get('chart_bars_max')} "
          f"per_request={per_req}")
    if (md.get("chart_bars_default") or 0) < 500:
        print("  FAIL default limit is a short slice, not a full chart")
        ok = False
    if (md.get("chart_bars_max") or 0) <= per_req:
        print("  FAIL max limit does not exceed bybit's per-request cap")
        ok = False

    deep = per_req * 3
    st, b = call("GET", f"/ohlc?symbol={SYMBOL}&timeframe=1h&limit={deep}", timeout=240)
    if st != 200:
        print(f"  1h limit={deep} -> HTTP {st}  FAIL {str(b)[:160]}")
        ok = False
    elif b["count"] <= per_req:
        print(f"  1h limit={deep} -> count={b['count']}  FAIL truncated at the "
              f"per-request cap instead of paginating")
        ok = False
    else:
        print(f"  1h limit={deep} -> count={b['count']} "
              f"{b['candles'][0]['ts']} .. {b['candles'][-1]['ts']}  ok (paginated)")

    # Exhausted history must be reported, not silently returned short.
    st, b = call("GET", f"/ohlc?symbol={SYMBOL}&timeframe=1mo&limit=1000")
    if st != 200 or b["count"] >= 1000 or not b["note"]:
        print(f"  1mo limit=1000 -> count={b.get('count')} note={b.get('note')}  "
              f"FAIL expected a short result WITH an explanatory note")
        ok = False
    else:
        print(f"  1mo limit=1000 -> count={b['count']} with note  ok")

    print("\n" + "=" * 72)
    print("LIVE STATE RETENTION (rolling buffer, not an archive)")
    print("=" * 72)
    ret = h.get("live_retention") if isinstance(h, dict) else None
    if not isinstance(ret, dict):
        print("  FAIL /health has no live_retention block")
        ok = False
    else:
        print(f"  retention_hours={ret['retention_hours']} purged_on={ret['purged_on']}")
        print(f"  keeps_latest_tick_per_symbol={ret['keeps_latest_tick_per_symbol']} "
              f"trades_retained_indefinitely={ret['trades_retained_indefinitely']}")
        print(f"  row_counts={ret['row_counts']}")
        print(f"  oldest_tick={ret['oldest_tick_recorded_at']} "
              f"newest_tick={ret['newest_tick_recorded_at']} db_bytes={ret['db_bytes']}")
        if not 0 < ret["retention_hours"] <= 72:
            print(f"  FAIL retention_hours={ret['retention_hours']} is not a "
                  f"short rolling window")
            ok = False

    print("\n" + "=" * 72)
    print("OVERLAY FRESHNESS (overlays lag live candles — must be declared)")
    print("=" * 72)
    required = {"overlay_timeframe", "computed_at", "computed_at_ms", "last_local_bar",
                "last_local_bar_by_timeframe", "lag_seconds", "bar_seconds",
                "stale", "note"}
    for path in (f"/live/state?symbol={SYMBOL}",
                 f"/live/zones?symbol={SYMBOL}",
                 f"/live/levels?symbol={SYMBOL}"):
        st, b = call("GET", path)
        fresh = b.get("overlay_freshness") if isinstance(b, dict) else None
        if st != 200 or not isinstance(fresh, dict):
            print(f"  {path:34s} -> HTTP {st}  FAIL no overlay_freshness")
            ok = False
            continue
        missing = required - set(fresh)
        if missing:
            print(f"  {path:34s} -> FAIL missing keys {sorted(missing)}")
            ok = False
            continue
        # A stale snapshot must carry an explanation; a fresh one must not claim staleness.
        if fresh["stale"] and not fresh["note"]:
            print(f"  {path:34s} -> FAIL stale=True with no note")
            ok = False
        print(f"  {path:34s} -> computed_at={fresh['computed_at']} "
              f"last_local_bar={fresh['last_local_bar']} "
              f"lag={fresh['lag_seconds']}s stale={fresh['stale']}")
    st, b = call("GET", f"/live/state?symbol={SYMBOL}")
    if isinstance(b, dict) and b.get("overlay_freshness"):
        print(f"    note: {b['overlay_freshness']['note']}")

    print("\n" + "=" * 72)
    print("VALIDATION (bad input should 4xx, never 500)")
    print("=" * 72)
    checks = [
        ("path-traversal symbol", "/ohlc?symbol=../../etc/passwd&timeframe=1H", 400),
        ("unknown symbol",        "/ohlc?symbol=NOPEUSDT&timeframe=1H",         404),
        ("unknown symbol 1m",     "/ohlc?symbol=NOPEUSDT&timeframe=1m",         404),
        ("bad timeframe",         f"/ohlc?symbol={SYMBOL}&timeframe=99X",       400),
        # 2D is a well-formed label bybit does not offer -> no remote, no CSV.
        ("unfetchable timeframe", f"/ohlc?symbol={CHART_ONLY_SYMBOL}&timeframe=2d", 404),
        ("source=local, no CSV",  f"/ohlc?symbol={CHART_ONLY_SYMBOL}&timeframe=1m&source=local", 404),
        ("bad source value",      f"/ohlc?symbol={SYMBOL}&timeframe=1m&source=nope", 422),
        ("limit over max",        f"/ohlc?symbol={SYMBOL}&timeframe=1h&limit=99999999", 422),
        ("unknown job id",        "/analyze/status/deadbeef",                   404),
        ("missing symbol param",  "/live/state",                                422),
    ]
    for label, url, want in checks:
        st, _ = call("GET", url)
        flag = "ok" if st == want else f"EXPECTED {want}"
        if st != want:
            ok = False
        print(f"  {label:24s} -> HTTP {st}  {flag}")

    # Chart-only symbols must not be analysable: /analyze needs local 1D/4H/1H.
    st, _ = call("POST", f"/analyze?symbol={CHART_ONLY_SYMBOL}")
    flag = "ok" if st == 404 else "EXPECTED 404"
    if st != 404:
        ok = False
    print(f"  {'analyze chart-only sym':24s} -> HTTP {st}  {flag}")

    print("\n" + "=" * 72)
    print("ANALYSIS ENDPOINT (background job)")
    print("=" * 72)
    before = live_row_counts()
    print(f"live row counts BEFORE analyze: {before}")

    body = show("POST", f"/analyze?symbol={SYMBOL}&start_date=2026-06-01"
                        f"&end_date=2026-07-01&step=30",
                keys=["job_id", "status", "symbol", "start_date", "end_date", "step", "poll"])
    if not body:
        sys.exit(1)
    job_id = body["job_id"]

    deadline, status = time.time() + 900, None
    while time.time() < deadline:
        st, js = call("GET", f"/analyze/status/{job_id}")
        status = js.get("status") if isinstance(js, dict) else None
        if status in ("done", "error"):
            break
        time.sleep(3)
    print(f"\n    job finished with status={status}")

    final = show("GET", f"/analyze/status/{job_id}",
                 keys=["job_id", "status", "symbol", "start_date", "end_date",
                       "step", "error", "summary", "funnel", "trades"], limit=3)
    show("GET", "/analyze/jobs", keys=["jobs"], limit=2)

    after = live_row_counts()
    print(f"\nlive row counts AFTER analyze:  {after}")
    if before == after:
        print("PASS — analysis did not touch live storage.")
    else:
        print("FAIL — live tables changed during an analysis run!")
        ok = False

    if final and final.get("status") == "error":
        print("\nJOB ERROR:\n", final.get("error"))
        ok = False

    if FAILURES:
        ok = False
        print("\nENDPOINT FAILURES:")
        for f in FAILURES:
            print("  -", f)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
