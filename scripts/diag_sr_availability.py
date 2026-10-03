"""
diag_sr_availability.py — Diagnostic script to measure S/R entry availability across parameter grids.
"""

import argparse
import os
import numpy as np
import pandas as pd

from tbb.indicators.zigzag import get_zigzag_swings
from tbb.indicators.support_resistance import find_sr_levels
from tbb.engines.entry import find_best_entry
from tbb.backtesting.backtest import load_data


def parse_args():
    parser = argparse.ArgumentParser(description="Measure S/R entry availability.")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
    parser.add_argument("--tf", type=str, default="1H", help="Execution timeframe")
    parser.add_argument("--bars", type=int, default=2000, help="Number of recent bars to evaluate")
    parser.add_argument("--step", type=int, default=4, help="Step between evaluation bars")
    return parser.parse_args()


def run_diagnostics():
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    tf = args.tf
    max_eval_bars = args.bars
    step = args.step

    windows = [200, 400, 700]
    tolerances = [0.5, 1.0]
    distances = [2.0, 3.0]

    configs = []
    for w in windows:
        for tol in tolerances:
            for dist in distances:
                configs.append({"window": w, "tolerance": tol, "distance": dist})

    symbol_results = {}
    overall_stats = {cfg_idx: {"long": 0, "short": 0, "either": 0, "total": 0} for cfg_idx in range(len(configs))}
    window_swings_all = {w: [] for w in windows}

    for symbol in symbols:
        try:
            _, _, df_exec = load_data(symbol, tf)
        except Exception as e:
            print(f"Error loading data for {symbol} ({tf}): {e}")
            continue

        if df_exec.empty:
            print(f"No data found for {symbol} ({tf})")
            continue

        n_total = len(df_exec)
        start_idx = max(0, n_total - max_eval_bars)
        eval_indices = list(range(start_idx, n_total, step))
        n_eval = len(eval_indices)

        if n_eval == 0:
            print(f"No evaluation bars for {symbol}")
            continue

        sym_stats = {cfg_idx: {"long": 0, "short": 0, "either": 0, "total": 0} for cfg_idx in range(len(configs))}

        for i in eval_indices:
            history_slice = df_exec.iloc[: i + 1]

            window_data = {}
            for w in windows:
                sub_df = history_slice.tail(w)
                highs = sub_df["high"].values
                lows = sub_df["low"].values
                closes = sub_df["close"].values
                swings = get_zigzag_swings(highs, lows, closes)
                n_swings = len(swings)
                window_data[w] = {"swings": swings, "closes": closes}
                window_swings_all[w].append(n_swings)

            for cfg_idx, cfg in enumerate(configs):
                w = cfg["window"]
                tol = cfg["tolerance"]
                dist = cfg["distance"]

                w_data = window_data[w]
                swings = w_data["swings"]
                closes = w_data["closes"]
                current_price = closes[-1]

                sr_levels = find_sr_levels(swings, tolerance_pct=tol)

                res_long = find_best_entry(current_price, "LONG", sr_levels, max_distance_pct=dist, min_touches=2)
                res_short = find_best_entry(current_price, "SHORT", sr_levels, max_distance_pct=dist, min_touches=2)

                has_long = res_long is not None
                has_short = res_short is not None
                has_either = has_long or has_short

                sym_stats[cfg_idx]["total"] += 1
                overall_stats[cfg_idx]["total"] += 1

                if has_long:
                    sym_stats[cfg_idx]["long"] += 1
                    overall_stats[cfg_idx]["long"] += 1
                if has_short:
                    sym_stats[cfg_idx]["short"] += 1
                    overall_stats[cfg_idx]["short"] += 1
                if has_either:
                    sym_stats[cfg_idx]["either"] += 1
                    overall_stats[cfg_idx]["either"] += 1

        symbol_results[symbol] = sym_stats

    # Output report
    print("=== S/R ENTRY AVAILABILITY DIAGNOSTIC ===")
    print(f"Timeframe: {tf} | Bars Evaluated: {max_eval_bars} | Step: {step}\n")

    print("Median Zigzag Swings per Window (Overall):")
    for w in windows:
        vals = window_swings_all[w]
        med = np.median(vals) if vals else 0
        print(f"  Window {w}: {med:.1f}")
    print()

    for symbol, sym_stats in symbol_results.items():
        print(f"--- Symbol: {symbol} ---")
        print(f"{'Window':<8} {'Tol(%)':<8} {'MaxDist(%)':<12} {'% LONG':<10} {'% SHORT':<10} {'% Either':<10}")
        print("-" * 62)
        for cfg_idx, cfg in enumerate(configs):
            stats = sym_stats[cfg_idx]
            tot = stats["total"]
            if tot == 0:
                p_long, p_short, p_either = 0.0, 0.0, 0.0
            else:
                p_long = (stats["long"] / tot) * 100
                p_short = (stats["short"] / tot) * 100
                p_either = (stats["either"] / tot) * 100
            print(f"{cfg['window']:<8} {cfg['tolerance']:<8} {cfg['distance']:<12} {p_long:<10.1f} {p_short:<10.1f} {p_either:<10.1f}")
        print()

    print("--- Overall (All Symbols) ---")
    print(f"{'Window':<8} {'Tol(%)':<8} {'MaxDist(%)':<12} {'% LONG':<10} {'% SHORT':<10} {'% Either':<10}")
    print("-" * 62)
    for cfg_idx, cfg in enumerate(configs):
        stats = overall_stats[cfg_idx]
        tot = stats["total"]
        if tot == 0:
            p_long, p_short, p_either = 0.0, 0.0, 0.0
        else:
            p_long = (stats["long"] / tot) * 100
            p_short = (stats["short"] / tot) * 100
            p_either = (stats["either"] / tot) * 100
        print(f"{cfg['window']:<8} {cfg['tolerance']:<8} {cfg['distance']:<12} {p_long:<10.1f} {p_short:<10.1f} {p_either:<10.1f}")


if __name__ == "__main__":
    run_diagnostics()
