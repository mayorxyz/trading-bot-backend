"""
retracement.py — pullback detection & entry timing inside an established move.

Self-contained orchestrator built ON TOP of fibonacci.py (which supplies the
leg, retracement zones, candle confirmation and invalidation). This module
adds the market-structure layer the fib engine does not have:

    1. Swing-sequence labelling  — every swing point tagged HH / HL / LH / LL
    2. Trend state from sequence — "up" needs recent HH+HL, "down" LH+LL
    3. Pullback depth tracker    — % of the last impulse leg already retraced
    4. Exhaustion heuristic      — stretched impulse legs tend to retrace
                                   (consecutive directional bars + ATR
                                   extension vs the 20-bar mean close)
    5. Trade plan                — entry at confirmation close, stop beyond
                                   the leg origin (structural invalidation),
                                   TP1 at fixed R, final target = leg extreme

Direction contract: fibonacci.py uses "up"/"down"; the pipeline passes
bias directions "LONG"/"SHORT". Both are normalized internally.

analyze_retracement() returns a JSON-safe dict:
    trade_ready   hard gate passed (aligned + in zone + candle confirm)
    aligned       leg direction agrees with requested trade direction
    stage         no_structure | trend_conflict | invalidated |
                  retracement_triggered | in_zone_awaiting_trigger |
                  deep_pullback_caution | approaching_zone | impulse_intact
    filters       {trend, labels, exhaustion, pullback_depth_pct,
                   zone_hits, invalidation}
    plan          {entry, stop, risk, tp1, tp1_rr, final_target, measured_rr}
"""

import numpy as np

from fibonacci import analyze_fibonacci, find_swings


# ---------------------------------------------------------------------------
# Structure layer
# ---------------------------------------------------------------------------

def label_swing_sequence(swings):
    """
    Tags each swing point relative to its same-type predecessor:
        highs -> HH (higher high) / LH (lower high) / EQH (equal)
        lows  -> HL (higher low)  / LL (lower low)  / EQL (equal)
    """
    labeled = []
    prev_high = None
    prev_low = None
    for s in swings:
        item = {"index": int(s["index"]), "price": float(s["price"]),
                "type": s["type"], "label": None}
        if s["type"] == "high":
            if prev_high is not None:
                if item["price"] > prev_high:
                    item["label"] = "HH"
                elif item["price"] < prev_high:
                    item["label"] = "LH"
                else:
                    item["label"] = "EQH"
            prev_high = item["price"]
        else:
            if prev_low is not None:
                if item["price"] > prev_low:
                    item["label"] = "HL"
                elif item["price"] < prev_low:
                    item["label"] = "LL"
                else:
                    item["label"] = "EQL"
            prev_low = item["price"]
        labeled.append(item)
    return labeled


def detect_trend_from_sequence(labeled_swings, window=6, min_score=2):
    """
    Net structure score over the last `window` labelled points:
        HH/LL count as continuation of their side; HL confirms up-structure,
        LL confirms down-structure; LH/LH mirror it.
    Returns "up", "down", or None when the sequence has no dominant side.
    """
    recent = [x for x in labeled_swings[-window:] if x.get("label")]
    if len(recent) < 2:
        return None

    score = 0
    for x in recent:
        lbl = x["label"]
        if lbl in ("HH", "HL"):
            score += 1
        elif lbl in ("LH", "LL"):
            score -= 1

    if score >= min_score:
        return "up"
    if score <= -min_score:
        return "down"
    return None


def pullback_depth(highs, lows, leg):
    """
    Fraction of leg['start']->leg['end'] already given back since leg['end']:
        up leg   -> (end_price - lowest_low_since_end)   / size
        down leg -> (highest_high_since_end - end_price) / size
    """
    end_idx = int(leg["end"]["index"])
    start_p = float(leg["start"]["price"])
    end_p = float(leg["end"]["price"])
    size = abs(end_p - start_p)
    if size <= 0:
        return {"depth_pct": None, "extreme_since_end": None}

    if leg["direction"] == "up":
        seg = np.asarray(lows[end_idx:], dtype=float)
        extreme = float(seg.min()) if len(seg) else end_p
        depth = (end_p - extreme) / size
    else:
        seg = np.asarray(highs[end_idx:], dtype=float)
        extreme = float(seg.max()) if len(seg) else end_p
        depth = (extreme - end_p) / size

    return {
        "depth_pct": round(float(max(depth, 0.0)), 3),
        "extreme_since_end": round(extreme, 10),
    }


def exhaustion_check(opens, closes, atr_value=None,
                     max_consecutive=5, extension_atr_mult=3.0, sma_window=20):
    """
    Stretched-leg heuristic: N consecutive same-colour bars into the move,
    and/or close extended far from its SMA(sma_window) measured in ATRs.
    A stretched impulse is NOT a blocker — it raises the odds the pullback
    being measured will actually happen.
    """
    o = np.asarray(opens, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(c)

    consecutive = 0
    if n >= 1:
        last_dir = c[-1] - o[-1]
        for i in range(n - 1, -1, -1):
            d = c[i] - o[i]
            if last_dir > 0 and d > 0:
                consecutive += 1
            elif last_dir < 0 and d < 0:
                consecutive += 1
            else:
                break

    extension_atr = None
    if atr_value and atr_value > 0 and n >= sma_window:
        sma = float(np.mean(c[-sma_window:]))
        extension_atr = round(float(abs(c[-1] - sma) / atr_value), 2)

    stretched = consecutive >= max_consecutive or (
        extension_atr is not None and extension_atr >= extension_atr_mult
    )
    return {
        "stretched": bool(stretched),
        "consecutive_bars": int(consecutive),
        "extension_atr": extension_atr,
    }


def build_retracement_plan(entry, direction, stop_ref, final_target=None,
                           tp1_rr=1.5):
    risk = abs(entry - stop_ref)
    if risk <= 0:
        return None
    long_dir = direction == "up"
    tp1 = entry + risk * tp1_rr if long_dir else entry - risk * tp1_rr
    plan = {
        "entry": round(float(entry), 10),
        "stop": round(float(stop_ref), 10),
        "risk": round(float(risk), 10),
        "tp1": round(float(tp1), 10),
        "tp1_rr": float(tp1_rr),
        "final_target": round(float(final_target), 10) if final_target is not None else None,
    }
    if final_target is not None:
        plan["measured_rr"] = round(abs(final_target - entry) / risk, 2)
    return plan


def compute_fib_score(fib_result):
    """
    0-100 quality score for one analyze_fibonacci() result:
        price inside any named retracement zone       +40
        inside the steepness-recommended zone         +15
        confirming candle at the zone                 +25
        21/50/200 EMA touching current price          +10
        RSI divergence agreeing with the leg side     +10
        invalidation warning                          -15 (invalidated -> 0)
    No usable leg -> 0. This feeds weighted confluence only; never a gate.
    """
    if not fib_result or fib_result.get("leg") is None:
        return 0

    score = 0.0
    zone_hits = {name: bool(v) for name, v in (fib_result.get("zone_hits") or {}).items()}
    hit_names = [name for name, hit in zone_hits.items() if hit]
    if hit_names:
        score += 40.0
        recommended = (fib_result.get("steepness") or {}).get("recommended_zone")
        if recommended in hit_names:
            score += 15.0

    if (fib_result.get("candle_confirmation") or {}).get("confirmed"):
        score += 25.0

    if (fib_result.get("ma_confluence") or {}).get("any_touched"):
        score += 10.0

    divergence = fib_result.get("divergence") or {}
    direction = (fib_result.get("leg") or {}).get("direction")
    if ((direction == "up" and divergence.get("bullish")) or
            (direction == "down" and divergence.get("bearish"))):
        score += 10.0

    status = (fib_result.get("invalidation") or {}).get("status")
    if status == "invalidated":
        return 0
    if status == "warning":
        score -= 15.0

    return int(round(max(0.0, min(score, 100.0))))


def _normalize_direction(direction):
    """Accepts LONG/SHORT/bullish/bearish/up/down -> 'up'/'down'/None."""
    if direction is None:
        return None
    d = str(direction).lower()
    if d in ("long", "bullish", "up"):
        return "up"
    if d in ("short", "bearish", "down"):
        return "down"
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def analyze_retracement(opens, highs, lows, closes, volumes=None,
                        trade_direction=None, left=3, right=3, method="wick",
                        atr_period=14, stop_buffer_atr=0.25, tp1_rr=1.5,
                        trend_window=6, min_trend_score=2,
                        fib_precomputed=None):
    """
    fib_precomputed: pass an analyze_fibonacci() result the caller already
    computed (e.g. a pipeline that shares one fib run across several consumers)
    to avoid recomputing it here.
    """
    opens = np.asarray(opens, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    result = {
        "trade_ready": False,
        "aligned": False,
        "stage": "no_structure",
        "direction": None,
        "filters": {},
        "plan": None,
        "skip_reason": None,
        "fib_confluence": False,
    }

    swings = find_swings(highs, lows, closes, left=left, right=right, method=method)
    labeled = label_swing_sequence(swings)
    trend = detect_trend_from_sequence(labeled, window=trend_window,
                                       min_score=min_trend_score)
    result["filters"]["trend"] = trend
    result["filters"]["recent_labels"] = [x["label"] for x in labeled[-6:]]

    fib = fib_precomputed if fib_precomputed is not None else \
        analyze_fibonacci(opens, highs, lows, closes, volumes=volumes,
                          left=left, right=right, method=method)
    leg = fib.get("leg")
    if leg is None:
        result["stage"] = "no_structure"
        result["skip_reason"] = "fib engine found no usable swing leg"
        return result

    direction = leg["direction"]
    result["direction"] = direction
    intended = _normalize_direction(trade_direction)

    aligned = True
    if intended is not None and intended != direction:
        aligned = False
    trend_conflict = trend is not None and trend != direction
    result["aligned"] = bool(aligned and not trend_conflict)

    atr_series = _atr(highs, lows, closes, period=atr_period)
    atr_now = float(atr_series[-1]) if atr_series is not None else None

    exhaustion = exhaustion_check(opens, closes, atr_value=atr_now)
    depth = pullback_depth(highs, lows, leg)
    invalidation = fib.get("invalidation") or {}
    zone_hits = {k: bool(v) for k, v in (fib.get("zone_hits") or {}).items()}
    steep = fib.get("steepness") or {}

    result["filters"].update({
        "exhaustion": exhaustion,
        "pullback_depth_pct": depth["depth_pct"],
        "extreme_since_end": depth["extreme_since_end"],
        "zone_hits": [name for name, hit in zone_hits.items() if hit],
        "recommended_zone": steep.get("recommended_zone"),
        "invalidation": invalidation.get("status"),
        "candle_confirmation": (fib.get("candle_confirmation") or {}).get("pattern"),
    })
    result["fib_confluence"] = bool(fib.get("fib_confluence")) and result["aligned"]

    if not aligned:
        result["stage"] = "trend_conflict" if trend_conflict else "misaligned_with_bias"
        result["skip_reason"] = (
            f"leg direction {direction} conflicts with "
            f"{'trend' if trend_conflict else 'requested bias'}"
        )
        return result

    entry_price = float(closes[-1])
    start_p = float(leg["start"]["price"])
    end_p = float(leg["end"]["price"])
    buffer = (stop_buffer_atr * atr_now) if (stop_buffer_atr and atr_now) else 0.0
    stop_ref = start_p - buffer if direction == "up" else start_p + buffer

    def _ready_plan():
        plan = build_retracement_plan(
            entry=entry_price, direction=direction, stop_ref=stop_ref,
            final_target=end_p, tp1_rr=tp1_rr,
        )
        result["trade_ready"] = plan is not None
        result["stage"] = "retracement_triggered"
        result["plan"] = plan
        if plan is None:
            result["skip_reason"] = "entry/stop degenerate (no risk distance)"
        return result

    if invalidation.get("status") == "invalidated":
        result["stage"] = "invalidated"
        result["skip_reason"] = "pullback broke full leg origin (1.0 retracement)"
        return result

    gate_ok = (
        bool(fib.get("trade_ready")) and
        invalidation.get("status") != "warning" and
        not trend_conflict
    )
    if gate_ok:
        return _ready_plan()

    if any(zone_hits.values()):
        result["stage"] = "in_zone_awaiting_trigger"
        result["skip_reason"] = "price in fib zone but no confirming candle yet"
        return result

    d = depth["depth_pct"]
    if d is not None and d >= 0.786:
        result["stage"] = "deep_pullback_caution"
        result["skip_reason"] = f"retraced {d:.0%} of the leg — near structural break"
    elif d is not None and d >= 0.236:
        result["stage"] = "approaching_zone"
        result["skip_reason"] = f"pullback underway ({d:.0%} of leg retraced)"
    else:
        result["stage"] = "impulse_intact"
        result["skip_reason"] = "impulse intact — waiting for a pullback to develop"

    return result


def _atr(highs, lows, closes, period=14):
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


if __name__ == "__main__":
    import random
    random.seed(11)
    n = 200
    o, h, l, c = [], [], [], []
    p = 100.0
    for i in range(n):
        op = p
        drift = 0.6 if i < 120 else (-0.9 if i % 17 == 0 else 0.55)
        cl = op + drift + random.uniform(-0.35, 0.35)
        hi = max(op, cl) + random.uniform(0.05, 0.25)
        lo = min(op, cl) - random.uniform(0.05, 0.25)
        o.append(op); h.append(hi); l.append(lo); c.append(cl)
        p = cl

    import json
    out = analyze_retracement(o, h, l, c, trade_direction="LONG")
    print(json.dumps(out, indent=2, default=str))
