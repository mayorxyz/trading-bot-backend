"""
pipeline.py — wires every module together in the correct order.
Drop this in the same folder as the other files (or same repo root),
alongside your existing multi_pair_scanner_v2.py.

Call analyze_pair() once you have OHLCV arrays for a pair/timeframe.
"""

import talib
import numpy as np

from zigzag import get_zigzag_swings
from support_resistance import find_sr_levels
from consolidation import find_consolidation_zones
from breakouts import detect_sr_breakout, detect_consolidation_breakout
from wicks import detect_wicks, wick_at_level
from entry import find_best_entry
from sl_tp import get_trade_levels
from validity import validate_trade
from confluence import calculate_confluence, confidence_label
from volume import detect_volume_spike, detect_volume_divergence
from signal_store import init_db, log_signal


def analyze_pair(pair, timeframe, opens, highs, lows, closes, volumes,
                  direction, pattern_name=None, trend_aligned=False,
                  mtf_full_alignment=False, db_path="signals.db"):
    """
    Runs the full structure -> entry -> SL/TP -> validity -> confluence -> log
    pipeline for one pair/timeframe snapshot.

    direction: "LONG" or "SHORT" — decided upstream (by your existing
               pattern/trend bias logic in multi_pair_scanner_v2.py)
    pattern_name: candlestick pattern that triggered this, if any
    trend_aligned: bool, from your existing SMA trend check
    mtf_full_alignment: bool, from your existing multi-timeframe check

    Returns: dict with full result, or {"skipped": reason} if no valid signal.
    """
    current_price = closes[-1]
    atr_arr = talib.ATR(np.array(highs), np.array(lows), np.array(closes), timeperiod=14)
    atr = atr_arr[-1]

    # 1. Structure detection
    swings = get_zigzag_swings(highs, lows, closes)
    sr_levels = find_sr_levels(swings)
    zones = find_consolidation_zones(swings)
    sr_breaks = detect_sr_breakout(closes, highs, lows, sr_levels)
    cons_breaks = detect_consolidation_breakout(closes, zones)
    wick_events = detect_wicks(opens, highs, lows, closes)
    wick_hits = wick_at_level(wick_events, sr_levels, highs, lows)
    vol_spikes = detect_volume_spike(volumes)
    vol_div = detect_volume_divergence(closes, volumes)

    # 2. Entry (needs sr_levels)
    entry_result = find_best_entry(current_price, direction, sr_levels)
    if entry_result is None:
        return {"skipped": "no qualifying entry level found"}

    # 3. SL/TP (needs entry_price from step 2, + atr, swings, sr_levels)
    trade = get_trade_levels(
        entry_result["entry_price"], direction, atr, swings, sr_levels
    )
    if trade is None:
        return {"skipped": "sl/tp calculation failed"}

    # 4. Validity check (needs full trade from step 3)
    check = validate_trade(trade, current_price)
    if not check["valid"]:
        return {"skipped": f"invalid trade: {check['reasons']}"}

    # 5. Confluence score (gathers signals from steps 1-4 + upstream inputs)
    last_idx = len(closes) - 1
    wick_at_entry = any(w["index"] >= last_idx - 2 for w in wick_hits)
    volume_confirm = any(v["index"] >= last_idx - 2 for v in vol_spikes)

    score_result = calculate_confluence({
        "pattern_match": pattern_name is not None,
        "trend_align": trend_aligned,
        "mtf_alignment": mtf_full_alignment,
        "sr_level_strength": entry_result["level_touches"],
        "wick_rejection": wick_at_entry,
        "fib_confluence": False,  # wire in from your existing fib check if available
        "volume_confirm": volume_confirm,
    })

    # 6. Log to Signal Store
    init_db(db_path)
    log_signal(
        pair=pair, timeframe=timeframe, direction=direction,
        entry_price=trade["entry"], sl_price=trade["sl"]["sl_price"],
        tp_price=trade["tp"]["tp_price"], rr=trade["tp"]["rr"],
        confluence_score=score_result["score"], valid=True,
        pattern=pattern_name, db_path=db_path,
    )

    return {
        "pair": pair,
        "timeframe": timeframe,
        "direction": direction,
        "entry": trade["entry"],
        "entry_level_touches": entry_result["level_touches"],
        "sl": trade["sl"]["sl_price"],
        "sl_method": trade["sl"]["method"],
        "tp": trade["tp"]["tp_price"],
        "tp_method": trade["tp"]["method"],
        "rr": trade["tp"]["rr"],
        "confluence_score": score_result["score"],
        "confidence": confidence_label(score_result["score"]),
        "confluence_breakdown": score_result["breakdown"],
        "swings_found": len(swings),
        "sr_levels_found": len(sr_levels),
        "consolidation_zones": len(zones),
        "sr_breakouts": len(sr_breaks),
        "consolidation_breakouts": len(cons_breaks),
        "wick_rejections_at_level": len(wick_hits),
        "volume_spikes": len(vol_spikes),
        "volume_divergences": len(vol_div),
    }


if __name__ == "__main__":
    # quick synthetic self-test
    import random
    random.seed(7)
    n = 300
    o, h, l, c, v = [], [], [], [], []
    p = 100.0
    for _ in range(n):
        op = p
        cl = p + random.uniform(-1.2, 1.2)
        hi = max(op, cl) + random.uniform(0, 0.8)
        lo = min(op, cl) - random.uniform(0, 0.8)
        o.append(op); h.append(hi); l.append(lo); c.append(cl)
        v.append(random.uniform(100, 300))
        p = cl

    result = analyze_pair(
        pair="TESTUSDT", timeframe="4h",
        opens=o, highs=h, lows=l, closes=c, volumes=v,
        direction="LONG", pattern_name="hammer",
        trend_aligned=True, mtf_full_alignment=False,
        db_path="/home/claude/test_pipeline.db",
    )
    import json
    print(json.dumps(result, indent=2, default=str))
