"""
profile_pipeline.py Ã¢â‚¬â€ locate hot spots across a representative SAMPLE of test
points, mirroring backtest.run_symbol's loop.

Profiling one point is misleading: most points exit early on the bias gate and
never reach pattern/imbalance detection. This walks a real stride of points and
aggregates, so the numbers reflect the actual backtest cost mix.
"""

import cProfile
import pstats
import io
import os
import time

import pandas as pd

from tbb.backtesting.backtest import (load_data, slice_up_to, LOOKBACK_BARS, CHECK_FORWARD_BARS,
                       EXECUTION_TF, BACKTEST_DB)
from tbb.pipeline import analyze_pair_with_bias

SYMBOL = os.environ.get("PROFILE_SYMBOL", "BTCUSDT")
STEP = int(os.environ.get("PROFILE_STEP", "25"))


def iter_inputs(symbol=SYMBOL, step=STEP):
    """Yield (pos, kwargs) for each test point, exactly as run_symbol builds them."""
    df_1d, df_4h, df_1h = load_data(symbol)
    n = len(df_1h)
    for pos in range(LOOKBACK_BARS, n - CHECK_FORWARD_BARS, step):
        ts = df_1h.index[pos]
        hist_1d = slice_up_to(df_1d, ts)
        hist_4h = slice_up_to(df_4h, ts)
        hist_1h = df_1h.iloc[:pos + 1]
        if len(hist_1d) < 10 or len(hist_4h) < 10 or len(hist_1h) < LOOKBACK_BARS:
            continue
        recent = hist_1h.tail(LOOKBACK_BARS)
        yield pos, dict(
            pair=symbol, timeframe=EXECUTION_TF,
            df_by_tf={"1D": hist_1d, "4H": hist_4h, "1H": hist_1h},
            opens=recent["open"].to_numpy(dtype=float),
            highs=recent["high"].to_numpy(dtype=float),
            lows=recent["low"].to_numpy(dtype=float),
            closes=recent["close"].to_numpy(dtype=float),
            volumes=recent["volume"].to_numpy(dtype=float),
            db_path=BACKTEST_DB,
        )


def run_sample(profile=False):
    points = list(iter_inputs())
    pr = cProfile.Profile() if profile else None

    early, deep, times = 0, 0, []
    if pr:
        pr.enable()
    for pos, kw in points:
        t0 = time.perf_counter()
        res = analyze_pair_with_bias(**kw)
        times.append(time.perf_counter() - t0)
        if isinstance(res, dict) and str(res.get("skipped", "")).startswith("not tradable"):
            early += 1
        else:
            deep += 1
    if pr:
        pr.disable()

    total = sum(times)
    times_sorted = sorted(times)
    print(f"\nsymbol={SYMBOL} step={STEP}  points={len(points)}")
    print(f"  bias-gate early exits : {early}")
    print(f"  reached full pipeline : {deep}")
    print(f"  TOTAL wall            : {total:.1f}s")
    print(f"  mean  per point       : {total / len(points) * 1000:.1f}ms")
    print(f"  median per point      : {times_sorted[len(times) // 2] * 1000:.1f}ms")
    print(f"  p90   per point       : {times_sorted[int(len(times) * 0.9)] * 1000:.1f}ms")
    print(f"  max   per point       : {times_sorted[-1] * 1000:.1f}ms")
    return pr


if __name__ == "__main__":
    pr = run_sample(profile=True)
    out = os.environ.get("PROFILE_OUT", "profile_stats.prof")
    pstats.Stats(pr).dump_stats(out)
    print(f"[stats dumped to {out}]")
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(22)
    print(s.getvalue())
