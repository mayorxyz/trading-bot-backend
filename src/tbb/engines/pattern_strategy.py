"""
pattern_strategy.py â€” strategy rules that turn chart patterns into trade plans.

Self-contained: NumPy only. Encodes the entry/exit techniques from the chart
pattern course on top of chart_patterns.py output:

    1. Momentum-candle confirmation of a trigger break (mandatory gate)
       â€” body-to-range ratio + how much of the body cleared the level.
    2. Break-and-retest detection â€” did price return to the broken level and
       reject it (previous resistance becoming support, and vice versa)?
    3. First-pullback stop reference â€” tighter invalidation than the raw
       pattern invalidation when a pullback already printed.
    4. Volatility-contraction quality filter measured on the BASE before the
       breakout bars (excludes the expanding breakout candle itself).
    5. Two-step take-profit: TP1 at tp1_rr * risk -> close half, move stop to
       breakeven; remainder targets the pattern's measured move.

analyze_pattern_setup() is the orchestrator the pipeline calls.
"""

import numpy as np

from tbb.indicators.chart_patterns import detect_chart_patterns


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def calculate_atr(highs, lows, closes, period=14):
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return None

    trs = np.zeros(n)
    trs[0] = highs[0] - lows[0]
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    atr = np.zeros(n)
    atr[period - 1] = trs[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


def momentum_candle(opens, highs, lows, closes, direction, level_price,
                    min_body_ratio=0.5, min_clearance_ratio=0.5):
    """
    The mandatory false-breakout gate. A break only counts when (a) the candle
    has a real body (not a doji), (b) it closed in the breakout direction, and
    (c) at least min_clearance_ratio of its body lies beyond the level.
    """
    o, h, l, c = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
    rng = h - l
    if rng <= 0:
        return {"confirmed": False, "reason": "zero range candle"}

    body = abs(c - o)
    if body <= 0:
        return {"confirmed": False, "body_ratio": 0.0, "clearance": 0.0,
                "reason": "zero body candle"}
    body_low, body_high = min(o, c), max(o, c)

    if direction in ("bullish", "up", "LONG"):
        clearance = (body_high - max(level_price, body_low)) / body
        color_ok = c > o
    else:
        clearance = (min(level_price, body_high) - body_low) / body
        color_ok = c < o

    confirmed = (
        (body / rng) >= min_body_ratio and
        color_ok and
        clearance >= min_clearance_ratio
    )
    return {
        "confirmed": bool(confirmed),
        "body_ratio": round(float(body / rng), 3),
        "clearance": round(float(max(clearance, 0.0)), 3),
        "color_ok": bool(color_ok),
    }


def detect_break_and_retest(closes, highs, lows, direction, trigger_level,
                            lookback=30, zone_frac=0.0015):
    """
    State machine over recent bars:
        "none"                 no qualifying break inside `lookback`
        "broke_awaiting"       broke the level, pullback not seen yet
        "retested"             pulled back into the zone and rejected it
                               (close back beyond the level after the touch)
    Returns {stage, break_index, retest_index}.
    """
    n = len(closes)
    start = max(1, n - lookback)
    broke_up = direction in ("bullish", "up", "LONG")
    band = abs(trigger_level) * zone_frac

    break_index = None
    for j in range(n - 1, start - 1, -1):
        prev_close = closes[j - 1]
        if broke_up and prev_close <= trigger_level < closes[j]:
            break_index = j
            break
        if not broke_up and prev_close >= trigger_level > closes[j]:
            break_index = j
            break
    if break_index is None:
        return {"stage": "none", "break_index": None, "retest_index": None}

    for k in range(break_index + 1, n):
        touched = lows[k] <= trigger_level + band if broke_up \
            else highs[k] >= trigger_level - band
        if not touched:
            continue
        rejected = closes[k] > trigger_level if broke_up else closes[k] < trigger_level
        return {
            "stage": "retested" if rejected else "broke_awaiting",
            "break_index": int(break_index),
            "retest_index": int(k),
        }

    return {"stage": "broke_awaiting", "break_index": int(break_index), "retest_index": None}


def first_pullback_stop(direction, break_index, highs, lows, buffer_atr=None,
                        atr_value=None):
    """
    First-pullback technique: once a break happened, the most recent opposing
    swing extreme between the break and now is a much tighter stop anchor than
    the full-pattern invalidation.
    """
    if break_index is None:
        return None
    segment_h = np.asarray(highs[break_index:], dtype=float)
    segment_l = np.asarray(lows[break_index:], dtype=float)
    if len(segment_h) == 0:
        return None
    buffer = (buffer_atr * atr_value) if (buffer_atr is not None and atr_value) else 0.0

    if direction in ("bullish", "up", "LONG"):
        return float(segment_l.min() - buffer)
    return float(segment_h.max() + buffer)


def volatility_contraction_base(highs, lows, closes, atr_period=14,
                                base_lookback=20, compare_lookback=60,
                                exclude_last=3, contraction_threshold=0.8):
    """
    Quality filter measured on the BASE: mean ATR over the consolidation window
    EXCLUDING the last `exclude_last` bars (the expanding breakout candles)
    versus the longer ATR average before it. Fixes the naive variant that
    compares ATR including the breakout bar â€” which almost never confirms.
    """
    atr = calculate_atr(highs, lows, closes, period=atr_period)
    if atr is None:
        return {"confirmed": False, "reason": "insufficient data for ATR"}

    valid = [a for a in atr if a > 0]
    if len(valid) < base_lookback + 5:
        return {"confirmed": False, "reason": "insufficient ATR history"}

    end = len(atr) - exclude_last
    start = end - base_lookback
    cmp_start = max(0, start - compare_lookback)
    if start <= cmp_start or end <= start:
        return {"confirmed": False, "reason": "window too small"}

    base_mean = float(np.mean(atr[start:end]))
    cmp_mean = float(np.mean(atr[cmp_start:start]))
    if base_mean <= 0 or cmp_mean <= 0:
        return {"confirmed": False, "reason": "non-positive ATR windows"}

    ratio = base_mean / cmp_mean
    return {
        "confirmed": bool(ratio <= contraction_threshold),
        "ratio": round(float(ratio), 3),
        "base_atr": round(base_mean, 8),
        "compare_atr": round(cmp_mean, 8),
    }


def build_trade_plan(entry, direction, stop_ref, measured_target=None,
                     tp1_rr=1.5, breakeven_after_tp1=True):
    risk = abs(entry - stop_ref)
    if risk <= 0:
        return None
    long_dir = direction in ("bullish", "up", "LONG")
    tp1 = entry + risk * tp1_rr if long_dir else entry - risk * tp1_rr
    plan = {
        "entry": float(entry),
        "stop": float(stop_ref),
        "risk": float(risk),
        "tp1": float(tp1),
        "tp1_rr": float(tp1_rr),
        "breakeven_after_tp1": bool(breakeven_after_tp1),
        "final_target": float(measured_target) if measured_target is not None else None,
    }
    if measured_target is not None and risk > 0:
        plan["measured_rr"] = round(abs(measured_target - entry) / risk, 2)
    return plan


def _same_side(pattern_direction, trade_direction):
    if pattern_direction is None or trade_direction is None:
        return True  # neutral-shape patterns (e.g. symmetrical triangle) pass through
    p_long = pattern_direction in ("bullish", "up", "LONG")
    t_long = trade_direction in ("bullish", "up", "LONG")
    return p_long == t_long


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def analyze_pattern_setup(opens, highs, lows, closes, volumes=None,
                          trade_direction=None, patterns=None,
                          retest_window=30, tp1_rr=1.5,
                          atr_period=14, stop_buffer_atr=0.25):
    """
    Turns detected chart patterns into an actionable setup verdict.

    Priority order:
        1. Confirmed pattern whose latest close broke the trigger with a
           momentum candle -> trade_ready with a two-step plan.
        2. Confirmed break awaiting retest -> watchlist entry (trade_ready
           False but actionable levels returned).
        3. Forming pattern aligned with bias -> watchlist only.

    Returns dict with keys:
        trade_ready      bool â€” hard gate passed (momentum-confirmed break)
        aligned          bool â€” any pattern agrees with trade_direction
        chosen           the winning pattern dict or None
        stage            "breakout_confirmed" | "awaiting_retest" |
                         "forming_watchlist" | "no_pattern"
        filters          momentum / retest / volatility results
        plan             build_trade_plan output or None
        skip_reason      why nothing fired (when applicable)
        all_patterns     compact list of everything detected
    """
    opens = np.asarray(opens, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    if patterns is None:
        report = detect_chart_patterns(highs, lows, closes)
    else:
        report = patterns
    found = report.get("patterns", [])

    atr_series = calculate_atr(highs, lows, closes, period=atr_period)
    atr_now = float(atr_series[-1]) if atr_series is not None else None

    result = {
        "trade_ready": False,
        "aligned": False,
        "chosen": None,
        "stage": "no_pattern",
        "filters": {},
        "plan": None,
        "skip_reason": None,
        "all_patterns": [
            {"pattern": p["pattern"], "direction": p.get("direction"),
             "status": p["status"], "trigger_level": p["trigger_level"],
             "invalidation": p["invalidation"],
             "measured_target": p.get("measured_target"),
             "bars_formed": p.get("bars_formed")}
            for p in found
        ],
    }
    if not found:
        result["skip_reason"] = "no chart pattern detected"
        return result

    candidates = [p for p in found if _same_side(p.get("direction"), trade_direction)]
    if trade_direction is not None:
        result["aligned"] = len([p for p in candidates if p.get("direction")]) > 0
    if not candidates:
        result["skip_reason"] = "patterns found but none align with trade direction"
        return result

    vol_filter = volatility_contraction_base(highs, lows, closes, atr_period=atr_period)
    result["filters"]["volatility_contraction"] = vol_filter

    def _make_plan(pattern, entry_price):
        stop_anchor = pattern["invalidation"]
        tight = first_pullback_stop(
            pattern["direction"], result["filters"].get("_break_index"),
            highs, lows, buffer_atr=stop_buffer_atr, atr_value=atr_now,
        )
        # Tighter stop wins when it stays on the correct side of entry.
        if tight is not None and entry_price is not None:
            long_dir = pattern["direction"] == "bullish"
            if long_dir and 0 < tight < entry_price:
                stop_anchor = tight
            elif not long_dir and tight > entry_price:
                stop_anchor = tight
        return build_trade_plan(
            entry=entry_price, direction=pattern["direction"],
            stop_ref=stop_anchor, measured_target=pattern.get("measured_target"),
            tp1_rr=tp1_rr,
        )

    for pattern in sorted(candidates, key=lambda p: (p["status"] == "confirmed",
                                                     p.get("touches", 0)), reverse=True):
        mom = momentum_candle(opens, highs, lows, closes,
                              pattern["direction"], pattern["trigger_level"])
        rtest = detect_break_and_retest(closes, highs, lows, pattern["direction"],
                                        pattern["trigger_level"], lookback=retest_window)
        result["filters"]["momentum"] = mom
        result["filters"]["retest"] = rtest
        result["filters"]["_break_index"] = rtest.get("break_index")
        result["chosen"] = pattern

        if pattern["status"] == "confirmed" and mom["confirmed"]:
            plan = _make_plan(pattern, float(closes[-1]))
            result.update({
                "trade_ready": bool(plan is not None),
                "stage": "breakout_confirmed",
                "plan": plan,
            })
            if plan is None:
                result["skip_reason"] = "momentum confirmed but stop/entry degenerate"
            return result

        if rtest["stage"] == "retested":
            result["stage"] = "breakout_confirmed"
            result["skip_reason"] = "break+retest present but latest candle failed momentum check"
            return result

        if rtest["stage"] == "broke_awaiting":
            result["stage"] = "awaiting_retest"
            result["skip_reason"] = "level broke, waiting for retest rejection"
            return result

    if result["stage"] == "no_pattern":
        forming = [p for p in candidates if p["status"] == "forming"]
        if forming:
            result["stage"] = "forming_watchlist"
            best_forming = max(forming, key=lambda p: (p.get("touches", 0),
                                                       p.get("bars_formed", 0)))
            result["chosen"] = best_forming
            result["skip_reason"] = (
                f"{best_forming['pattern']} forming â€” trigger "
                f"{best_forming['trigger_level']}, invalidation "
                f"{best_forming['invalidation']}"
            )
        else:
            result["skip_reason"] = "candidate patterns exist but none broke their trigger yet"

    return result
