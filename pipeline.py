"""
pipeline.py — wires every module together in the correct order.
Drop this in the same folder as the other files.

analyze_pair() — core structure/entry/SL-TP/confluence/log flow.
Signals fire on weighted evidence, not on every indicator being present: the
only hard requirements are a tradable bias, a qualifying entry, and valid
SL/TP. Everything else (fib zone, chart pattern, retracement, wicks, volume,
structure) contributes weight to a 0-100 confluence score, and a conviction
gate skips the setup when that score lands below min_confluence_score
(default 50 = at least MEDIUM; pass 0 to disable).
analyze_pair_with_bias() — wraps it with phase1-6 top-down bias via bias_bridge.py,
plus execution-timeframe chart-pattern analysis (chart_patterns.py through
pattern_strategy.py) and fib-retracement pullback analysis (retracement.py on
top of fibonacci.py). A momentum-confirmed pattern break adds
chart_pattern_align points to the confluence score; a confirmed fib-zone
pullback entry adds retracement_confirm points and lights the previously
placeholder fib_confluence input. Every result carries "chart_pattern" and
"retracement" summary blocks regardless of outcome.

Both take db_path, which is where a fired signal is logged. Pass db_path=None to
compute a signal without logging it — used by the stateless POST /predict.
"""

import talib
import numpy as np
from datetime import datetime

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
from pattern_detector import get_active_patterns, summarize_bias
from phase6_supplementary import detect_mss, is_news_blackout
from pattern_strategy import analyze_pattern_setup
from retracement import analyze_retracement, compute_fib_score
from fibonacci import analyze_fibonacci
from breakout_engine import analyze_breakout
from elliott_wave import analyze_elliott_wave


def _summarize_chart_pattern(setup):
    """JSON-safe compact view of a pattern_strategy.analyze_pattern_setup result."""
    chosen = setup.get("chosen") or {}
    plan = setup.get("plan") or {}
    return {
        "trade_ready": setup.get("trade_ready", False),
        "aligned": setup.get("aligned", False),
        "stage": setup.get("stage"),
        "pattern": chosen.get("pattern"),
        "pattern_direction": chosen.get("direction"),
        "status": chosen.get("status"),
        "trigger_level": chosen.get("trigger_level"),
        "invalidation": chosen.get("invalidation"),
        "measured_target": chosen.get("measured_target"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "tp1": plan.get("tp1"),
        "final_target": plan.get("final_target"),
        "skip_reason": setup.get("skip_reason"),
        "patterns_found": [p.get("pattern") for p in setup.get("all_patterns", [])],
    }


def _summarize_retracement(setup):
    """JSON-safe compact view of a retracement.analyze_retracement result."""
    plan = setup.get("plan") or {}
    filters = setup.get("filters") or {}
    return {
        "trade_ready": setup.get("trade_ready", False),
        "aligned": setup.get("aligned", False),
        "stage": setup.get("stage"),
        "direction": setup.get("direction"),
        "fib_confluence": setup.get("fib_confluence", False),
        "trend": filters.get("trend"),
        "pullback_depth_pct": filters.get("pullback_depth_pct"),
        "zone_hits": filters.get("zone_hits", []),
        "recommended_zone": filters.get("recommended_zone"),
        "invalidation": filters.get("invalidation"),
        "exhausted_leg": (filters.get("exhaustion") or {}).get("stretched"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "tp1": plan.get("tp1"),
        "final_target": plan.get("final_target"),
        "skip_reason": setup.get("skip_reason"),
    }


def analyze_pair_with_bias(pair, timeframe, df_by_tf,
                            opens, highs, lows, closes, volumes,
                            db_path="signals.db", news_calendar=None,
                            min_confluence_score=50):
    """
    Top-down bias resolution wrapper. 
    Pass news_calendar (list of dicts with 'time' and 'impact') to enforce hard news blackouts.
    """
    bias = resolve_bias(df_by_tf)

    if not bias["tradable"] or bias["direction"] is None:
        return {"skipped": f"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"}

    # ---------------------------------------------------------------------
    # NEW: Market Structure Shift (MSS) Conflict Check
    # ---------------------------------------------------------------------
    no_mss_conflict = True
    if bias.get("structure") and bias["structure"].get("events"):
        mss_events = detect_mss(bias["structure"]["events"], bias["structure"].get("trend", ""))
        if mss_events:
            last_mss = mss_events[-1]["event"]
            conflicting = (
                (bias["direction"] == "LONG" and last_mss == "MSS_bear") or
                (bias["direction"] == "SHORT" and last_mss == "MSS_bull")
            )
            no_mss_conflict = not conflicting

    if not no_mss_conflict:
        return {"skipped": "conflicting Market Structure Shift (MSS) detected"}

    # ---------------------------------------------------------------------
    # NEW: News Blackout Hard Gate
    # ---------------------------------------------------------------------
    if news_calendar is not None:
        blackout = is_news_blackout(news_calendar, datetime.utcnow())
        if blackout["blackout"]:
            return {"skipped": f"news blackout: {blackout['events_today']}"}

    # ---------------------------------------------------------------------
    # Existing Bias & Pattern Alignment Logic
    # ---------------------------------------------------------------------
    exec_tf_df = list(df_by_tf.values())[-1]
    active_patterns = get_active_patterns(exec_tf_df)
    pattern_bias = summarize_bias(active_patterns)

    agreeing = (pattern_bias["bullish_patterns"] if bias["direction"] == "LONG"
                else pattern_bias["bearish_patterns"])
    pattern_name = agreeing[0] if agreeing else None

    # Structure/liquidity confluence inputs
    structure_bos_align = False
    if bias.get("structure") and bias["structure"].get("events"):
        recent_bos = [e for e in bias["structure"]["events"][-5:] if "BOS" in e["event"]]
        if recent_bos:
            last_bos = recent_bos[-1]["event"]
            structure_bos_align = (
                (bias["direction"] == "LONG" and last_bos == "BOS_bull") or
                (bias["direction"] == "SHORT" and last_bos == "BOS_bear")
            )

    liquidity_target = False
    if bias.get("liquidity") is not None and not bias["liquidity"].empty:
        current_price = closes[-1]
        pool = bias["liquidity"][bias["liquidity"]["liquidity_type"].isin(["major", "low_hanging"])]
        if not pool.empty:
            if bias["direction"] == "LONG":
                targets = pool[pool["price"] > current_price]
            else:
                targets = pool[pool["price"] < current_price]
            liquidity_target = not targets.empty

    # ---------------------------------------------------------------------
    # NEW: Chart-pattern setup (chart_patterns.py + pattern_strategy.py)
    # Runs on the execution timeframe arrays. A raised exception is captured
    # into the summary instead of being swallowed, so live/backtest/predict
    # keep working while a pattern bug remains visible in every result.
    # ---------------------------------------------------------------------
    try:
        pattern_setup = analyze_pattern_setup(
            opens, highs, lows, closes, volumes,
            trade_direction=bias["direction"],
        )
        chart_pattern_info = _summarize_chart_pattern(pattern_setup)
    except Exception as e:
        chart_pattern_info = {"trade_ready": False, "aligned": False,
                              "stage": "error", "error": f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------------------
    # NEW: Fibonacci & breakout engine scores (weighted inputs, not gates).
    # Fibonacci runs ONCE here and the same result is shared into the
    # retracement orchestrator (retracement.py on top of fibonacci.py), so
    # every consumer reads one computation. Errors surface in the summary
    # blocks instead of killing live/backtest/predict runs.
    # ---------------------------------------------------------------------
    fib_info = {"score": 0, "trade_ready": False}
    fib_raw = None
    try:
        fib_raw = analyze_fibonacci(opens, highs, lows, closes, volumes=volumes)
        zone_hits = [n for n, v in (fib_raw.get("zone_hits") or {}).items() if v]
        fib_info = {
            "score": compute_fib_score(fib_raw),
            "trade_ready": bool(fib_raw.get("trade_ready")),
            "in_any_zone": len(zone_hits) > 0,
            "zone_hits": zone_hits,
            "invalidation": (fib_raw.get("invalidation") or {}).get("status"),
        }
    except Exception as e:
        fib_raw = None
        fib_info = {"score": 0, "trade_ready": False, "stage": "error",
                    "error": f"{type(e).__name__}: {e}"}

    breakout_info = {"score": 0, "trade_ready": False}
    try:
        brk = analyze_breakout(opens, highs, lows, closes, volumes=volumes)
        breakout_info = {
            "score": int(brk.get("score") or 0),
            "trade_ready": bool(brk.get("trade_ready")),
            "direction": (brk.get("breakout") or {}).get("direction"),
            "shape": ((brk.get("key_levels") or {}).get("shape")),
            "momentum_confirmed": bool((brk.get("momentum") or {}).get("is_momentum")),
            "volume_confirmed": bool((brk.get("volume") or {}).get("confirmed")),
            "entry": (brk.get("entry_stop") or {}).get("entry"),
            "stop": (brk.get("entry_stop") or {}).get("stop"),
            "tp1": (brk.get("take_profit_plan") or {}).get("tp1"),
            "chandelier_stop": brk.get("chandelier_stop"),
        }
    except Exception as e:
        breakout_info = {"score": 0, "trade_ready": False, "stage": "error",
                         "error": f"{type(e).__name__}: {e}"}

    elliott_info = {"score": 0, "trade_ready": False}
    try:
        ew = analyze_elliott_wave(opens, highs, lows, closes, volumes=volumes)
        pos = ew.get("position") or {}
        elliott_info = {
            "score": int(ew.get("elliott_score") or 0),
            "trade_ready": bool(ew.get("trade_ready")),
            "count_valid": bool(ew.get("count_valid")),
            "candidate_found": bool(ew.get("candidate_found")),
            "direction": ew.get("direction"),
            "position_stage": pos.get("stage"),
            "position_direction": pos.get("direction"),
            "trigger_level": pos.get("trigger_level"),
            "invalidation": pos.get("invalidation"),
            "stale": bool(pos.get("stale")),
            "truncated": bool((ew.get("truncation") or {}).get("truncated")),
            "divergence": bool(((ew.get("wave5_divergence") or {}).get("divergence"))),
        }
    except Exception as e:
        elliott_info = {"score": 0, "trade_ready": False, "stage": "error",
                        "error": f"{type(e).__name__}: {e}"}

    try:
        retr_setup = analyze_retracement(
            opens, highs, lows, closes, volumes,
            trade_direction=bias["direction"],
            fib_precomputed=fib_raw,
        )
        retracement_info = _summarize_retracement(retr_setup)
    except Exception as e:
        retracement_info = {"trade_ready": False, "aligned": False,
                            "stage": "error", "error": f"{type(e).__name__}: {e}"}

    return analyze_pair(
        pair=pair, timeframe=timeframe,
        opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes,
        direction=bias["direction"], pattern_name=pattern_name,
        trend_aligned=bias["trend_aligned"], mtf_full_alignment=bias["mtf_full_alignment"],
        structure_bos_align=structure_bos_align, liquidity_target=liquidity_target,
        no_mss_conflict=no_mss_conflict,
        db_path=db_path,
        chart_pattern_info=chart_pattern_info,
        retracement_info=retracement_info,
        fib_info=fib_info,
        breakout_info=breakout_info,
        elliott_info=elliott_info,
        min_confluence_score=min_confluence_score,
    )


def analyze_pair(pair, timeframe, opens, highs, lows, closes, volumes,
                  direction, pattern_name=None, trend_aligned=False,
                  mtf_full_alignment=False, structure_bos_align=False,
                  liquidity_target=False, no_mss_conflict=True, db_path="signals.db",
                  chart_pattern_info=None, retracement_info=None,
                  fib_info=None, breakout_info=None, elliott_info=None,
                  min_confluence_score=50):
    """
    Core execution logic. Computes structure, entry, SL/TP, and confluence.
    """
    swings = get_zigzag_swings(highs, lows, closes)
    sr_levels = find_sr_levels(swings)
    zones = find_consolidation_zones(swings)
    sr_breaks = detect_sr_breakout(closes, highs, lows, sr_levels)
    cons_breaks = detect_consolidation_breakout(closes, zones)
    wick_events = detect_wicks(opens, highs, lows, closes)
    wick_hits = wick_at_level(wick_events, sr_levels, highs, lows)
    vol_spikes = detect_volume_spike(volumes)
    vol_div = detect_volume_divergence(closes, volumes)

    # FIX: Define missing variables referenced in original code
    current_price = closes[-1]
    atr = float(talib.ATR(np.array(highs), np.array(lows), np.array(closes), timeperiod=14)[-1])

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

    # Engine facts when the bias wrapper supplied them; legacy direct callers
    # that pass nothing keep the old zero-contribution behaviour.
    retracement_confirmed = bool(retracement_info and retracement_info.get("trade_ready"))
    fib_score_val = int(fib_info.get("score") or 0) if fib_info else 0
    breakout_score_val = int(breakout_info.get("score") or 0) if breakout_info else 0
    elliott_score_val = int(elliott_info.get("score") or 0) if elliott_info else 0

    score_result = calculate_confluence({
        "pattern_match": pattern_name is not None,
        "trend_align": trend_aligned,
        "mtf_alignment": mtf_full_alignment,
        "sr_level_strength": entry_result["level_touches"],
        "wick_rejection": wick_at_entry,
        "fib_score": fib_score_val,
        "volume_confirm": volume_confirm,
        "structure_bos_align": structure_bos_align,
        "liquidity_target": liquidity_target,
        "no_mss_conflict": no_mss_conflict, # NEW: Added to confluence scoring
        "chart_pattern_align": bool(chart_pattern_info and chart_pattern_info.get("trade_ready")),
        "retracement_confirm": retracement_confirmed,
        "breakout_score": breakout_score_val,
        "elliott_score": elliott_score_val,
    })

    # Conviction gate: weighted evidence decides, not presence of every input.
    # A signal only fires when the gathered confluence is at least MEDIUM
    # (default 50). Pass min_confluence_score=0 to disable.
    if score_result["score"] < min_confluence_score:
        return {
            "skipped": f"low conviction: confluence {score_result['score']}/100 "
                       f"below required {min_confluence_score}",
            "confluence_breakdown": score_result["breakdown"],
            "chart_pattern": chart_pattern_info,
            "retracement": retracement_info,
            "fibonacci": fib_info,
            "breakout_engine": breakout_info,
            "elliott": elliott_info,
        }

    # db_path=None => compute and return WITHOUT logging. 
    # The signal maths above is unchanged either way; this only gates the side effect, 
    # so a stateless preview (POST /predict) cannot pollute the signals.db ledger 
    # that live and backtest runs share.
    if db_path is not None:
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
        "no_mss_conflict": no_mss_conflict,
        "chart_pattern": chart_pattern_info,
        "retracement": retracement_info,
        "fibonacci": fib_info,
        "breakout_engine": breakout_info,
        "elliott": elliott_info,
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