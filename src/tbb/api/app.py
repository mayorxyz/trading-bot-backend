"""
tbb.api.app — FastAPI application assembly.

Read-only HTTP surface: LIVE state feed, chart candles, queued backtests and a
stateless /predict. Route bodies live in routes_live / routes_market /
routes_analysis / routes_predict; shared helpers in common.py.

Run with:
    uvicorn tbb.api.app:app --reload --port 8000
"""

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tbb import config as paths, logsetup
from tbb.marketdata import market_data
from tbb.storage import analysis_store, live_store, scan_store, signal_store
from tbb.api import routes_analysis, routes_live, routes_market, routes_predict, routes_scan
from tbb.api.common import DATA_DIR

log = logsetup.get_logger("api")

app = FastAPI(
    title="Trading Bot API",
    description="LIVE state feed + on-demand ANALYSIS backtests. Read-only except POST /analyze.",
    version="1.0.0",
)

# CORS: comma-separated origins via CORS_ORIGINS ("*" keeps local dev open).
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- API-key gate (production) ----
# Set API_KEY to require `X-API-Key: <key>` on every route except /health
# (load balancers / uptime probes stay keyless). Unset = open, for local dev.
API_KEY = os.environ.get("API_KEY", "").strip()

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if API_KEY and request.url.path != "/health":
        if request.headers.get("X-API-Key") != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing X-API-Key"})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Never leak tracebacks through HTTP; they go to the rotating log instead.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(routes_live.router)
app.include_router(routes_market.router)
app.include_router(routes_analysis.router)
app.include_router(routes_predict.router)
app.include_router(routes_scan.router)


# ---------- meta ----------

@app.get("/health")
def health():
    retention = live_store.retention_info()
    heartbeat = live_store.runner_heartbeat()
    return {
        "status": "ok",
        "live_db": live_store.LIVE_DB,
        "analysis_db": analysis_store.ANALYSIS_DB,
        "signals_db": signal_store.DB_PATH,
        "paths": {
            "live_db": live_store.LIVE_DB,
            "analysis_db": analysis_store.ANALYSIS_DB,
            "signals_db": signal_store.DB_PATH,
            "data_dir": paths.DATA_DIR,
            "logs_dir": os.path.join(paths.DATA_DIR, "logs"),
            "scan_db": scan_store.SCAN_DB,
        },
        # Seconds since the newest live tick was written â€” the UI's staleness
        # dot. None when the runner has never written a tick.
        "db_freshness_seconds": heartbeat["last_tick_age_seconds"],
        "runner": heartbeat,
        "live_retention": retention,
        "market_data": market_data.cache_stats(),
        "scan": scan_store.active_job(),
    }



