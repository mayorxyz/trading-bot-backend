"""
structure_retest.py — break-of-structure retest entries from phase5 events.

The course's core entry: price breaks a structural high/low (BOS), pulls
back into the broken level, and the level rejects (previous resistance
becoming support, and vice versa). Chart-pattern necklines fire rarely;
structure breaks happen constantly, so phase5's event stream becomes an
entry SOURCE here instead of only feeding gates.

Broken-level derivation (no phase5 change required):
    BOS_bear records the NEW low that broke prior support -> broken level is
    the most recent 'swing_low' event BEFORE it. BOS_bull mirrors with
    'swing_high'.

Reuses pattern_strategy's momentum_candle / detect_break_and_retest /
calculate_atr / build_trade_plan — no duplicated logic. Same setup contract
as analyze_pattern_setup: trade_ready / stage / plan / skip_reason.
"""

import numpy as np

from pattern_strategy import (calculate_atr, momentum_candle,
                              detect_break_and_retest, build_trade_plan)


def latest_bos_level(structure_events):
    """
    Most recent BOS + the structural level it broke.
        BOS_bull -> broke a swing HIGH above -> bullish (level now support)
        BOS_bear -> broke a swing LOW below  -> bearish (level now resistance)
    Returns {direction, level, bos_index} or None.
    """
    if not structure_events:
        return None

    for i in range(len(structure_events) - 1, -1, -1):
        ev = structure_events[i]
        event = str(ev.get("event", ""))
        if "BOS" not in event:
            continue
        direction = "bullish" if "bull" in event else "bearish"
        # Bullish BOS breaks the prior swing HIGH (resistance->support);
        # bearish BOS breaks the prior swing LOW (support->resistance).
        wanted = ("swing_high" if direction == "bullish" else "swing_low")
        bos_idx = ev.get("idx")
        for j in range(i - 1, -1, -1):
            prior = structure_events[j]
            if prior.get("event") == wanted:
                price = prior.get("price")
                if price is None:
                    continue
                return {
                    "direction": direction,
                    "level": float(price),
                    "bos_index": bos_idx,
                }
        return None  # most recent BOS has no derivable origin; do not skip ahead
    return None


def analyze_structure_retest(structure_events, opens, highs, lows, closes,
                             trade_direction=None, retest_window=30,
                             atr_period=14, stop_buffer_atr=0.25, tp1_rr=1.5):
    """
    Verdict for trading a retest of the most recently broken structural level.
    Direction contract: bias uses LONG/SHORT, internals use bullish/bearish —
    both normalized here.
    """
    opens = np.asarray(opens, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    result = {
        "trade_ready": False,
        "aligned": False,
        "stage": "no_bos",
        "direction": None,
        "level": None,
        "filters": {},
        "plan": None,
        "skip_reason": None,
    }

    bos = latest_bos_level(structure_events)
    if bos is None:
        result["skip_reason"] = "no derivable break-of-structure level"
        return result

    direction = bos["direction"]
    level = bos["level"]
    result["direction"] = direction
    result["level"] = round(level, 10)

    intended = None
    if trade_direction is not None:
        t = str(trade_direction).lower()
        intended = {"long": "bullish", "short": "bearish"}.get(t)
    aligned = intended is None or intended == direction
    result["aligned"] = bool(aligned)

    atr_series = calculate_atr(highs, lows, closes, period=atr_period)
    atr_now = float(atr_series[-1]) if atr_series is not None else None

    mom = momentum_candle(opens, highs, lows, closes, direction, level)
    rtest = detect_break_and_retest(closes, highs, lows, direction, level,
                                    lookback=retest_window)
    result["filters"] = {
        "momentum": mom,
        "retest": rtest,
        "bos_index": bos.get("bos_index"),
    }

    if not aligned:
        result["stage"] = "misaligned_with_bias"
        result["skip_reason"] = (
            f"BOS retest is {direction} but bias is {trade_direction}"
        )
        return result

    def _plan():
        long_dir = direction == "bullish"
        buffer = (stop_buffer_atr * atr_now) if (stop_buffer_atr and atr_now) else 0.0
        stop_ref = level - buffer if long_dir else level + buffer
        return build_trade_plan(
            entry=float(closes[-1]), direction=direction,
            stop_ref=stop_ref, measured_target=None, tp1_rr=tp1_rr,
        )

    if mom["confirmed"]:
        plan = _plan()
        result.update({
            "trade_ready": plan is not None,
            "stage": "bos_retest_confirmed",
            "plan": plan,
        })
        if plan is None:
            result["skip_reason"] = "momentum confirmed but stop/entry degenerate"
        return result

    if rtest["stage"] == "retested":
        result["stage"] = "bos_retest_confirmed"
        result["skip_reason"] = "retest rejected but latest candle failed momentum gate"
        return result

    if rtest["stage"] == "broke_awaiting":
        result["stage"] = "broke_awaiting_retest"
        result["skip_reason"] = "structure broke, waiting for pullback into the level"
        return result

    result["stage"] = "no_recent_break"
    result["skip_reason"] = f"break of {round(level, 10)} older than {retest_window} bars"
    return result
