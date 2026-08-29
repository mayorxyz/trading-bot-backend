"""tbb.api.routes_predict — stateless single-shot pipeline run (/predict)."""

import traceback

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tbb.backtesting import backtest
from tbb.live import runner as live_runner
from tbb.marketdata import market_data
from tbb.pipeline import analyze_pair_with_bias
from tbb.api.common import (
    PREDICT_BIAS_TFS, PREDICT_EXEC_BARS, PREDICT_MIN_BIAS_BARS,
    _analysis_ready, _clean_symbol, _clean_timeframe, _iso, _json_safe, _ms,
)

router = APIRouter()

# ---------- PREDICT (run the pipeline once on live data) ----------

class PredictRequest(BaseModel):
    symbol: str = Field(..., description="any symbol Bybit lists, e.g. BTCUSDT")
    timeframe: str = Field(backtest.EXECUTION_TF,
                           description="execution timeframe, e.g. 1H / 15M / 5M")


def _predict_bars(tf: str, is_execution: bool) -> int:
    """
    How many bars to pull for one timeframe.

    Bias depth mirrors live_runner.SEED_DAYS so /predict computes bias over the
    same window live_runner would â€” ema_trend_filter derives its flat/steep
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


def _live_frames(symbol: str, execution_tf: str) -> tuple:
    """
    Build ({timeframe: DataFrame} bias frames, execution DataFrame) from LIVE
    Bybit candles held in memory.

    Bias frames and the execution frame are fetched INDEPENDENTLY, even when
    execution_tf is one of PREDICT_BIAS_TFS (e.g. 1H): collapsing the two roles
    on tf string equality would size the shared frame by execution depth and
    feed the bias window a non-bias-depth slice, letting /predict disagree with
    the pipeline's own bias semantics for that timeframe. Same data source
    live_runner seeds from (ingestion_bybit REST -> DataFrame, via market_data's
    cache) and deliberately NOT data/*.csv or either database. Raises
    HTTPException with an explicit message rather than propagating a
    Bybit/pandas error.
    """
    for tf in PREDICT_BIAS_TFS:
        if not market_data.is_supported_timeframe(tf):
            raise HTTPException(400, f"timeframe {tf!r} cannot be fetched live from "
                                     f"Bybit; live-capable timeframes: "
                                     f"{', '.join(market_data.SUPPORTED_TIMEFRAMES)}")
    if not market_data.is_supported_timeframe(execution_tf):
        raise HTTPException(400, f"timeframe {execution_tf!r} cannot be fetched live "
                                 f"from Bybit; live-capable timeframes: "
                                 f"{', '.join(market_data.SUPPORTED_TIMEFRAMES)}")

    bias_frames = {tf: _fetch_live_frame(symbol, tf, False)
                   for tf in PREDICT_BIAS_TFS}
    exec_df = _fetch_live_frame(symbol, execution_tf, True)
    return bias_frames, exec_df


def _fetch_live_frame(symbol: str, tf: str, is_execution: bool):
    """One recent_candles call at /predict depth, with /predict's error mapping."""
    try:
        return market_data.recent_candles(symbol, tf, _predict_bars(tf, is_execution))
    except market_data.SymbolNotFound as exc:
        raise HTTPException(404, f"no live data for {symbol} {tf}: {exc}") from exc
    except market_data.MarketDataError as exc:
        raise HTTPException(
                503, f"live market data unavailable for {symbol} {tf} â€” cannot "
                     f"predict without it: {exc}") from exc


@router.post("/predict")
def predict(req: PredictRequest):
    """
    Run the signal pipeline ONCE against current live candles and return what it
    decides. This is "Run Analysis", not "Run backtest".

    Distinct from POST /analyze in every respect: no date range, no job queue, no
    stored history, and no persistence. Candles come from live Bybit into memory
    (the same source live_runner.py seeds from â€” never data/*.csv, never
    analysis_runs.db), the pipeline runs synchronously, and the result is returned
    without a row being written anywhere. pipeline's signal log is suppressed via
    db_path=None, so signals.db is untouched too.

    Works for any symbol Bybit lists. Note that unlike /live/state this needs no
    live_runner process at all â€” it fetches its own frames â€” so it answers for
    chart-only symbols as well as the analysis-tracked ones.

    A pipeline "skip" is a valid outcome, returned as HTTP 200 with
    signal_fired=false and the reason. Errors are reserved for genuinely being
    unable to run: 400 bad input, 404 unknown symbol/timeframe, 422 not enough
    live history yet, 503 Bybit unreachable, 500 unexpected pipeline failure.
    """
    symbol = _clean_symbol(req.symbol)
    timeframe = _clean_timeframe(req.timeframe) or backtest.EXECUTION_TF

    bias_frames, exec_df = _live_frames(symbol, timeframe)

    # "No live buffer yet" surfaces here: a freshly listed coin can be tradable
    # and still have too few bars for the structure engine to say anything.
    if len(exec_df) < backtest.LOOKBACK_BARS:
        raise HTTPException(
            422, f"not enough live history for {symbol} {timeframe}: "
                 f"{len(exec_df)} bars available, {backtest.LOOKBACK_BARS} needed. "
                 f"Bybit has no deeper history for this pair yet.")
    for tf in PREDICT_BIAS_TFS:
        if len(bias_frames[tf]) < PREDICT_MIN_BIAS_BARS:
            raise HTTPException(
                422, f"not enough live {tf} history for {symbol} to resolve bias: "
                     f"{len(bias_frames[tf])} bars available, "
                     f"{PREDICT_MIN_BIAS_BARS} needed.")

    recent = exec_df.tail(backtest.LOOKBACK_BARS)
    ts = exec_df.index[-1]

    # Exactly live_runner.run_analysis's call shape: bias from 1D/4H/1H frames,
    # entry/SL/TP from the execution timeframe's OHLCV.
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
                      for tf, df in {**bias_frames, timeframe: exec_df}.items()},
        "execution_bars": int(len(recent)),
    })
