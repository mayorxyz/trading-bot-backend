"""
pipeline.py — wires every module together in the correct order.
Drop this in the same folder as the other files.

analyze_pair() — core structure/entry/SL-TP/confluence/log flow.
analyze_pair_with_bias() — wraps it with phase1-6 top-down bias via bias_bridge.py.
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
from bias_bridge import resolve_bias


def get_active_patterns(df):
    """Return a lightweight pattern summary from a dataframe-like object.

    The upstream bias wrapper expects a dict keyed by pattern name with a
    directional signal. If no pattern detector is available, return an empty
    mapping so the caller can handle the neutral case gracefully.
    """
    return {}


def summarize_bias(active_patterns):
    """Convert pattern mapping into a directional summary.

    Supported values can be plain booleans, numeric scores, or strings like
    'bullish'/'bearish'. The result matches the expectations of
    analyze_pair_with_bias():
      - net_bias: 'bullish' | 'bearish' | 'neutral'
      - bullish_patterns: list of bullish pattern names
      - bearish_patterns: list of bearish pattern names
    """
    if not active_patterns:
        return {
            "net_bias": "neutral",
            "bullish_patterns": [],
            "bearish_patterns": [],
        }

    bullish_patterns = []
    bearish_patterns = []

    for name, value in active_patterns.items():
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"bullish", "long", "buy", "up"}:
                bullish_patterns.append(name)
            elif normalized in {"bearish", "short", "sell", "down"}:
                bearish_patterns.append(name)
            continue

        if isinstance(value, (int, float)):
            if value > 0:
                bullish_patterns.append(name)
            elif value < 0:
                bearish_patterns.append(name)
            continue

        if bool(value):
            bullish_patterns.append(name)

    if len(bullish_patterns) > len(bearish_patterns):
        net_bias = "bullish"
    elif len(bearish_patterns) > len(bullish_patterns):
        net_bias = "bearish"
    else:
        net_bias = "neutral"

    return {
        "net_bias": net_bias,
        "bullish_patterns": bullish_patterns,
        "bearish_patterns": bearish_patterns,
    }


def analyze_pair_with_bias(pair, timeframe, df_by_tf,
                            opens, highs, lows, closes, volumes,
                            db_path="signals.db"):
    bias = resolve_bias(df_by_tf)

    if not bias["tradable"] or bias["direction"] is None:
        return {"skipped": f"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"}

    exec_tf_df = list(df_by_tf.values())[-1]
    active_patterns = get_active_patterns(exec_tf_df)
    pattern_bias = summarize_bias(active_patterns)
    print(f"DEBUG bias={bias['direction']} exec_tf_rows={len(exec_tf_df)} last_close={exec_tf_df['close'].iloc[-1]}")
    
    direction_bias = "bullish" if bias["direction"] == "LONG" else "bearish"
    ...
    if pattern_bias["net_bias"] != direction_bias:
        return {"skipped": f"no confirming pattern - pattern_bias={pattern_bias['net_bias']}, "
                            f"direction={bias['direction']}, active_patterns={active_patterns}"}

    pattern_name = (pattern_bias["bullish_patterns"] if bias["direction"] == "LONG"
                     else pattern_bias["bearish_patterns"])
    pattern_name = pattern_name[0] if pattern_name else None

    return analyze_pair(
        pair=pair, timeframe=timeframe,
        opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes,
        direction=bias["direction"], pattern_name=pattern_name,
        trend_aligned=bias["trend_aligned"], mtf_full_alignment=bias["mtf_full_alignment"],
        db_path=db_path,
    )



def analyze_pair(pair, timeframe, opens, highs, lows, closes, volumes,
                  direction, pattern_name=None, trend_aligned=False,
                  mtf_full_alignment=False, db_path="signals.db"):
    current_price = closes[-1]
    atr_arr = talib.ATR(np.array(highs), np.array(lows), np.array(closes), timeperiod=14)
    atr = atr_arr[-1]

    swings = get_zigzag_swings(highs, lows, closes)
    sr_levels = find_sr_levels(swings)
    zones = find_consolidation_zones(swings)
    sr_breaks = detect_sr_breakout(closes, highs, lows, sr_levels)
    cons_breaks = detect_consolidation_breakout(closes, zones)
    wick_events = detect_wicks(opens, highs, lows, closes)
    wick_hits = wick_at_level(wick_events, sr_levels, highs, lows)
    vol_spikes = detect_volume_spike(volumes)
    vol_div = detect_volume_divergence(closes, volumes)

    entry_result = find_best_entry(current_price, direction, sr_levels)
    if entry_result is None:
        return {"skipped": "no qualifying entry level found"}

    trade = get_trade_levels(
        entry_result["entry_price"], direction, atr, swings, sr_levels
    )
    if trade is None:
        return {"skipped": "sl/tp calculation failed"}

    check = validate_trade(trade, current_price)
    if not check["valid"]:
        return {"skipped": f"invalid trade: {check['reasons']}"}

    last_idx = len(closes) - 1
    wick_at_entry = any(w["index"] >= last_idx - 2 for w in wick_hits)
    volume_confirm = any(v["index"] >= last_idx - 2 for v in vol_spikes)

    score_result = calculate_confluence({
        "pattern_match": pattern_name is not None,
        "trend_align": trend_aligned,
        "mtf_alignment": mtf_full_alignment,
        "sr_level_strength": entry_result["level_touches"],
        "wick_rejection": wick_at_entry,
        "fib_confluence": False,
        "volume_confirm": volume_confirm,
    })

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
        db_path="/tmp/test_pipeline.db",
    )
    import json

    print(json.dumps(result, indent=2, default=str))