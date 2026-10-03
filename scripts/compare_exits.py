"""compare_exits.py — flat (BACKTEST_MANAGED_EXITS=0) vs managed (=1) A/B.

For BTCUSDT, ETHUSDT over the full CSV range (run_symbol defaults:
LOOKBACK_BARS..n-CHECK_FORWARD_BARS, step=4), run the backtest in each exit
mode and print per mode: trades, win_rate, avg_rr, pf_R, avg win R,
avg loss R, plus by-direction splits.

Strategy logic is untouched: each combination runs in an isolated subprocess
with BACKTEST_MANAGED_EXITS set, because backtest.py reads that flag (and the
COST_BPS_PER_SIDE / SLIPPAGE_BPS cost flags) at import time.

Usage:  python scripts/compare_exits.py [--step 4]
"""
import json
import os
import statistics
import subprocess
import sys

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
MODES = [("flat", "0"), ("managed", "1")]

WORKER = """
import json
from tbb.analysis.risk_journal import TradeJournal
from tbb.backtesting import backtest as bt
j = TradeJournal()
c, by_dir, _ = bt.run_symbol(__SYM__, j, step=__STEP__, trade_id_start=0, verbose=False)
import pandas as pd
df = j.to_dataframe()
closed = df[df["outcome"].isin(["win", "loss"])] if not df.empty else df
recs = []
if not closed.empty:
    for _, r in closed.iterrows():
        recs.append({"direction": r["direction"], "outcome": r["outcome"],
                     "rr": float(r["realized_rr"])})
print(json.dumps({"symbol": __SYM__, "mode": bt.MANAGED_EXITS,
                  "cost_bps": bt.COST_BPS_PER_SIDE, "slip_bps": bt.SLIPPAGE_BPS,
                  "trades": c["trades"], "rr_gross": c["rr_gross"],
                  "rr_net": c["rr_net"], "cost_r": c["cost_r"],
                  "by_dir": by_dir, "recs": recs}))
"""


def run_one(symbol, mode_val, step):
    env = dict(os.environ)
    env["BACKTEST_MANAGED_EXITS"] = mode_val
    code = WORKER.replace("__SYM__", repr(symbol)).replace("__STEP__", repr(step))
    p = subprocess.run([sys.executable, "-u", "-c", code],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print(f"--- worker failed {symbol} mode={mode_val} ---\n{p.stderr[-3000:]}")
        raise SystemExit(1)
    line = p.stdout.strip().splitlines()[-1]
    return json.loads(line)


def summarize(recs):
    wins = [r["rr"] for r in recs if r["outcome"] == "win"]
    losses = [r["rr"] for r in recs if r["outcome"] == "loss"]
    n = len(recs)
    wr = len(wins) / n if n else 0.0
    avg = sum(recs) / n if n else 0.0
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = gw / gl if gl > 0 else float("inf")
    return {"n": n, "wr": wr, "avg": avg, "pf": pf,
            "avg_win": statistics.fmean(wins) if wins else 0.0,
            "avg_loss": statistics.fmean(losses) if losses else 0.0}


def main():
    step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 4
    print(f"compare_exits: symbols={SYMBOLS} step={step} "
          f"cost={os.environ.get('COST_BPS_PER_SIDE', '10')}bps/side "
          f"slip={os.environ.get('SLIPPAGE_BPS', '5')}bps "
          f"(R values below are NET of costs)")
    results = {}
    for label, val in MODES:
        for sym in SYMBOLS:
            print(f"... running {sym} mode={label} ...", flush=True)
            results[(label, sym)] = run_one(sym, val, step)

    hdr = (f"{'mode':<8}{'symbol':<9}{'trades':>7}{'win_rate':>9}{'avg_rr':>8}"
           f"{'pf_R':>7}{'avg_win':>9}{'avg_loss':>9}")
    print("\n=== flat vs managed (full range, NET of costs) ===")
    print(hdr)
    print("-" * len(hdr))
    for label, _ in MODES:
        for sym in SYMBOLS:
            r = results[(label, sym)]
            s = summarize(r["recs"])
            print(f"{label:<8}{sym:<9}{s['n']:>7}{s['wr']:>9.3f}"
                  f"{s['avg']:>+8.3f}{s['pf']:>7.3f}{s['avg_win']:>+9.3f}"
                  f"{s['avg_loss']:>+9.3f}")
    print("\n--- by direction (NET R) ---")
    for label, _ in MODES:
        for sym in SYMBOLS:
            r = results[(label, sym)]
            print(f"{label} {sym}:")
            for side in ("LONG", "SHORT"):
                d = r["by_dir"][side]
                t = d["win"] + d["loss"]
                if t:
                    print(f"  {side:5s} trades={t:3d} wins={d['win']:3d} "
                          f"win_rate={d['win'] / t:.3f} "
                          f"avg_rr_net={d['rr'] / t:+.3f} "
                          f"avg_rr_gross={d.get('rr_gross', d['rr']) / t:+.3f}")
    print("\n--- gross vs net (avg R per trade) ---")
    for label, _ in MODES:
        for sym in SYMBOLS:
            r = results[(label, sym)]
            t = r["trades"]
            if t:
                print(f"{label:<8}{sym:<9}gross={r['rr_gross'] / t:+.3f}  "
                      f"net={r['rr_net'] / t:+.3f}  cost={r['cost_r'] / t:+.3f}R/trade")


if __name__ == "__main__":
    main()
