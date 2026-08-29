"""
elliott_wave.py Ã¢â‚¬â€ standalone Elliott Wave Theory (EWT) rule validator,
position-in-count locator + scorer.

Design intent (per course transcript + explicit direction from user):
  This is NOT a real-time auto wave-counter. Auto-counting repaints
  constantly and produces false signals Ã¢â‚¬â€ the course itself warns two
  traders can get two different valid counts on the same chart.

  Instead this is a RULE VALIDATOR: it takes the most recent 6 swing
  points (a candidate 0-1-2-3-4-5 motive structure), checks them against
  Elliott's 5 unbreakable rules, and produces a confluence SCORE from
  the classic Fibonacci ratio relationships (retracement + extension +
  equality + alternation + divergence). It never hard-gates a signal on
  its own opinion of the "count" Ã¢â‚¬â€ only the 5 actual EWT rules are hard
  gates, because those are structural facts, not interpretation.

  detect_wave_position() adds what a pure validator cannot say: WHERE in
  a developing count price currently sits. It walks the trailing swing
  run and finds the deepest legal prefix (0..5), mapping each to an
  actionable stage Ã¢â‚¬â€ wave2 holding (wave-3 loading zone, best entry),
  wave3 extended (await wave-4 pullback), wave4 done (wave-5 trigger),
  impulse complete (reversal watch) Ã¢â‚¬â€ with trigger/invalidation levels.

  Also included: MACD divergence alongside RSI for wave-5 exhaustion,
  a recency guard against stale counts, a light A-B-C correction
  recognizer for context, and JSON-safe output casting.

  Standalone-ish: NumPy plus fibonacci.py shared utilities only
  (find_swings / calculate_rsi / calculate_ema) Ã¢â‚¬â€ same shared-utility
  pattern as pattern_strategy and retracement. No pipeline imports;
  all Elliott logic lives here. Works on any OHLC arrays.

Usage:
    from tbb.engines.elliott_wave import analyze_elliott_wave
    result = analyze_elliott_wave(opens, highs, lows, closes)
    print(result["report"])
"""

import numpy as np

from tbb.indicators.fibonacci import (find_swings, calculate_rsi,
                                       calculate_ema)  # shared swing/RSI utility

# ---------------------------------------------------------------------------
# Tolerances for Fibonacci ratio confluence checks (real markets are never
# textbook-perfect, so each ratio check uses a tolerance band, not a point).
# ---------------------------------------------------------------------------

WAVE2_RETRACE_BAND = (0.45, 0.68)      # ideal 0.5-0.618
WAVE4_RETRACE_BAND = (0.30, 0.45)      # ideal 0.382
WAVE3_EXTENSION_BAND = (1.45, 1.85)    # ideal 1.618 of wave 1
WAVE5_EQUALITY_TOL_PCT = 25            # wave5 within +/-25% of wave1 length = "equal"
WAVE5_EXT_BAND = (0.50, 0.75)          # ideal 0.618 of wave1->wave3 span


# ---------------------------------------------------------------------------
# 1. Candidate wave extraction Ã¢â‚¬â€ last 6 alternating swing points
# ---------------------------------------------------------------------------

def get_candidate_motive_wave(swings):
    """
    Pulls the last 6 swing points that strictly alternate type (high/low),
    interpreted as points 0 (start of wave1), 1, 2, 3, 4, 5.

    Returns None if fewer than 6 alternating points are available, or the
    most recent 6 points don't strictly alternate (i.e. structure isn't
    a clean impulsive candidate at all Ã¢â‚¬â€ most common case, and expected;
    this engine should return "no valid candidate" far more often than
    it returns a valid one, by design).
    """
    if len(swings) < 6:
        return None

    last6 = swings[-6:]
    for i in range(1, 6):
        if last6[i]["type"] == last6[i - 1]["type"]:
            return None  # not alternating -> no clean candidate

    p0, p1, p2, p3, p4, p5 = last6
    direction = "up" if p5["price"] > p0["price"] else "down"

    return {
        "points": {"p0": p0, "p1": p1, "p2": p2, "p3": p3, "p4": p4, "p5": p5},
        "direction": direction,
    }


def _wave_len(a, b):
    """Absolute price length between two swing points."""
    return abs(b["price"] - a["price"])


# ---------------------------------------------------------------------------
# 2. The 5 unbreakable rules Ã¢â‚¬â€ direction-aware, hard gate
# ---------------------------------------------------------------------------

def check_five_rules(candidate):
    """
    Direction-aware checks for all 5 Elliott rules. Returns
    {"valid": bool, "broken": [str, ...]}.

    Up-trend candidate: p0=low, p1=high, p2=low, p3=high, p4=low, p5=high
    Down-trend candidate: mirror image (p0=high, p1=low, ...)
    """
    pts = candidate["points"]
    p0, p1, p2, p3, p4, p5 = (pts["p0"], pts["p1"], pts["p2"],
                               pts["p3"], pts["p4"], pts["p5"])
    up = candidate["direction"] == "up"
    broken = []

    wave1_len = _wave_len(p0, p1)
    wave3_len = _wave_len(p2, p3)
    wave5_len = _wave_len(p4, p5)

    # Rule 1: wave 2 cannot retrace past the start of wave 1 (p0)
    if up:
        if p2["price"] < p0["price"]:
            broken.append("Rule 1: wave 2 retraced past wave 1 start")
    else:
        if p2["price"] > p0["price"]:
            broken.append("Rule 1: wave 2 retraced past wave 1 start")

    # Rule 2: wave 3 can never be the shortest of waves 1, 3, 5
    if wave3_len < wave1_len and wave3_len < wave5_len:
        broken.append("Rule 2: wave 3 is the shortest of 1/3/5")

    # Rule 3: wave 3 must close past the end of wave 1 (p1)
    if up:
        if p3["price"] <= p1["price"]:
            broken.append("Rule 3: wave 3 did not close past wave 1 end")
    else:
        if p3["price"] >= p1["price"]:
            broken.append("Rule 3: wave 3 did not close past wave 1 end")

    # Rule 4: wave 4 cannot overlap the price area (p0-p1 range) of wave 1
    w1_lo, w1_hi = (p0["price"], p1["price"]) if up else (p1["price"], p0["price"])
    if w1_lo <= p4["price"] <= w1_hi:
        broken.append("Rule 4: wave 4 overlapped wave 1's price area")

    # Rule 5 (universal): a valid motive wave needs exactly 5 alternating
    # subwaves ending on the same side (impulse). Structurally this is
    # already enforced by get_candidate_motive_wave() requiring 6 strictly
    # alternating points, but we re-verify direction consistency here:
    # p1, p3, p5 must all be on the impulse side; p2, p4 on the pullback side.
    impulse_type = "high" if up else "low"
    pullback_type = "low" if up else "high"
    if not (p1["type"] == p3["type"] == p5["type"] == impulse_type and
            p2["type"] == p4["type"] == pullback_type):
        broken.append("Rule 5: does not resolve into 5 alternating subwaves")

    return {"valid": len(broken) == 0, "broken": broken}


# ---------------------------------------------------------------------------
# 3. Truncation check (wave 5 fails to pass wave 3 high/low)
# ---------------------------------------------------------------------------

def check_truncation(candidate):
    pts = candidate["points"]
    p3, p5 = pts["p3"], pts["p5"]
    up = candidate["direction"] == "up"
    truncated = (p5["price"] <= p3["price"]) if up else (p5["price"] >= p3["price"])
    return {"truncated": truncated}


# ---------------------------------------------------------------------------
# 4. Fibonacci ratio confluence Ã¢â‚¬â€ retracements, extension, equality
# ---------------------------------------------------------------------------

def score_fib_ratios(candidate):
    """
    Scores how closely the candidate's waves match the classic Elliott+Fib
    relationships. Each check contributes points if within tolerance band;
    none of these are hard gates Ã¢â‚¬â€ they are confluence, same philosophy
    as every other engine in this system.

    Points (sum to 70; remaining 30 come from alternation + divergence):
      wave2 retrace 0.5-0.618 of wave1      -> 20
      wave4 retrace ~0.382 of wave3          -> 20
      wave3 extension ~1.618 of wave1        -> 15
      wave5 ~= wave1 (equality) OR 0.618 ext -> 15
    """
    pts = candidate["points"]
    p0, p1, p2, p3, p4, p5 = (pts["p0"], pts["p1"], pts["p2"],
                               pts["p3"], pts["p4"], pts["p5"])

    wave1_len = _wave_len(p0, p1)
    wave3_len = _wave_len(p2, p3)
    wave5_len = _wave_len(p4, p5)
    w1_to_w3_span = _wave_len(p0, p3)

    detail = {}
    score = 0.0

    # Wave 2 retracement of wave 1
    w2_depth = _wave_len(p1, p2) / wave1_len if wave1_len else 0
    w2_ok = WAVE2_RETRACE_BAND[0] <= w2_depth <= WAVE2_RETRACE_BAND[1]
    detail["wave2_retrace_pct"] = round(w2_depth, 3)
    detail["wave2_in_band"] = w2_ok
    if w2_ok:
        score += 20

    # Wave 4 retracement of wave 3
    w4_depth = _wave_len(p3, p4) / wave3_len if wave3_len else 0
    w4_ok = WAVE4_RETRACE_BAND[0] <= w4_depth <= WAVE4_RETRACE_BAND[1]
    detail["wave4_retrace_pct"] = round(w4_depth, 3)
    detail["wave4_in_band"] = w4_ok
    if w4_ok:
        score += 20

    # Wave 3 extension of wave 1
    w3_ratio = wave3_len / wave1_len if wave1_len else 0
    w3_ok = WAVE3_EXTENSION_BAND[0] <= w3_ratio <= WAVE3_EXTENSION_BAND[1]
    detail["wave3_extension_ratio"] = round(w3_ratio, 3)
    detail["wave3_in_band"] = w3_ok
    if w3_ok:
        score += 15

    # Wave 5: equality with wave 1, OR 0.618 of the wave1->wave3 span
    equality_diff_pct = (abs(wave5_len - wave1_len) / wave1_len * 100
                         if wave1_len else 999)
    w5_equal = equality_diff_pct <= WAVE5_EQUALITY_TOL_PCT
    w5_ext_ratio = wave5_len / w1_to_w3_span if w1_to_w3_span else 0
    w5_ext_ok = WAVE5_EXT_BAND[0] <= w5_ext_ratio <= WAVE5_EXT_BAND[1]
    detail["wave5_vs_wave1_diff_pct"] = round(equality_diff_pct, 1)
    detail["wave5_equal_to_wave1"] = w5_equal
    detail["wave5_extension_ratio"] = round(w5_ext_ratio, 3)
    detail["wave5_ext_in_band"] = w5_ext_ok
    if w5_equal or w5_ext_ok:
        score += 15

    return {"score": score, "detail": detail}


# ---------------------------------------------------------------------------
# 5. Alternation Ã¢â‚¬â€ wave 2 and wave 4 should differ in character
# ---------------------------------------------------------------------------

def score_alternation(candidate):
    """
    Alternation principle: if wave 2 is sharp (steep, few bars, deep %),
    wave 4 tends to be sideways (shallow, more bars) and vice versa.
    Approximated with bars-per-%-retrace "sharpness" on each pullback.
    Contributes up to 15 points.
    """
    pts = candidate["points"]
    p1, p2, p3, p4 = pts["p1"], pts["p2"], pts["p3"], pts["p4"]

    w2_bars = max(p2["index"] - p1["index"], 1)
    w4_bars = max(p4["index"] - p3["index"], 1)
    w2_pct = _wave_len(p1, p2) / p1["price"] * 100 if p1["price"] else 0
    w4_pct = _wave_len(p3, p4) / p3["price"] * 100 if p3["price"] else 0

    w2_sharpness = w2_pct / w2_bars
    w4_sharpness = w4_pct / w4_bars

    # "Alternate" = one notably sharper than the other (ratio >= 1.4x either way)
    ratio = max(w2_sharpness, w4_sharpness) / max(min(w2_sharpness, w4_sharpness), 1e-9)
    alternates = ratio >= 1.4

    return {
        "score": 15 if alternates else 0,
        "wave2_sharpness": round(w2_sharpness, 4),
        "wave4_sharpness": round(w4_sharpness, 4),
        "alternates": alternates,
    }


# ---------------------------------------------------------------------------
# 6. Wave 5 divergence (RSI + MACD) Ã¢â‚¬â€ exhaustion / reversal warning
# ---------------------------------------------------------------------------

def _ema_series(closes, period):
    """EMA as a full series (NaN-headed) Ã¢â‚¬â€ fibonacci.calculate_ema returns a scalar."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    out = np.full(len(closes), np.nan)
    ema = float(np.mean(closes[:period]))
    out[period - 1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * alpha + ema * (1 - alpha)
        out[i] = ema
    return out


def _macd(closes, fast=12, slow=26, signal=9):
    """Standard MACD line + signal EMA; NaN-headed series like calculate_ema."""
    closes = np.asarray(closes, dtype=float)
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    if ema_fast is None or ema_slow is None:
        return None

    macd_line = ema_fast - ema_slow
    valid_idx = [i for i in range(len(macd_line)) if not np.isnan(macd_line[i])]
    if len(valid_idx) < signal:
        return None

    alpha = 2 / (signal + 1)
    first = valid_idx[0]
    sig = float(np.mean(macd_line[first:first + signal]))
    sig_series = np.full(len(closes), np.nan)
    sig_start = first + signal - 1
    sig_series[sig_start] = sig
    for i in range(sig_start + 1, len(closes)):
        sig = macd_line[i] * alpha + sig * (1 - alpha)
        sig_series[i] = sig
    return {"macd": macd_line, "signal": sig_series}


def score_wave5_divergence(candidate, closes):
    """
    Wave-5 exhaustion check: price makes a new extreme beyond wave 3 while
    momentum does not confirm. RSI is primary, MACD line is a second vote;
    either one flagging counts as divergence. Bonus confluence, up to 15
    points Ã¢â‚¬â€ matters most for exit/reversal timing, not entry.
    """
    pts = candidate["points"]
    p3, p5 = pts["p3"], pts["p5"]
    up = candidate["direction"] == "up"

    price_new_extreme = (p5["price"] > p3["price"]) if up else (p5["price"] < p3["price"])
    rsi_div = False
    macd_div = False
    detail = "no wave 5 divergence"

    rsi = calculate_rsi(closes)
    if rsi is None or len(rsi) < 2:
        return {"score": 0, "divergence": False, "rsi_divergence": False,
                "macd_divergence": False, "detail": "insufficient data for RSI"}

    offset = len(closes) - len(rsi)
    idx3 = max(p3["index"] - offset - 1, 0)
    idx5 = max(p5["index"] - offset - 1, 0)

    if idx3 < len(rsi) and idx5 < len(rsi):
        rsi_confirms = (rsi[idx5] > rsi[idx3]) if up else (rsi[idx5] < rsi[idx3])
        rsi_div = bool(price_new_extreme and not rsi_confirms)

    macd = _macd(closes)
    if macd is not None:
        m = macd["macd"]
        v3, v5 = m[idx3], m[idx5]
        if not (np.isnan(v3) or np.isnan(v5)):
            macd_confirms = (v5 > v3) if up else (v5 < v3)
            macd_div = bool(price_new_extreme and not macd_confirms)

    divergence = rsi_div or macd_div
    if divergence:
        detail = ("wave-5 exhaustion divergence ("
                  + ", ".join(filter(None, [
                      "RSI" if rsi_div else "",
                      "MACD" if macd_div else ""])) + ")")
    return {
        "score": 15 if divergence else 0,
        "divergence": divergence,
        "rsi_divergence": rsi_div,
        "macd_divergence": macd_div,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 7. Position-in-count Ã¢â‚¬â€ where in a DEVELOPING count does price sit now?
# ---------------------------------------------------------------------------

def _trailing_alternating(swings, max_points=6):
    """Longest strictly-alternating suffix of the swing list (<= max_points)."""
    run = []
    for s in reversed(swings):
        if run and s["type"] == run[0]["type"]:
            break
        run.insert(0, s)
        if len(run) >= max_points:
            break
    return run


def detect_wave_position(swings, closes):
    """
    Finds the DEEPEST prefix 0..k of the trailing alternating swings that
    maps legally onto Elliott's p0..p5 template (either direction), applying
    every hard rule that prefix makes checkable:
        k>=2 -> rule 1      k>=3 -> rule 3
        k>=4 -> rule 4      k==5 -> all five rules (via check_five_rules)

    The stage is then refined by the CURRENT close so callers learn whether
    the next wave already triggered. Stage ladder (up-count shown; down mirrors):

        k=1  wave1_developing
        k=2  close <= p1: wave2_holding_awaiting_w3_break   <- best entry zone
             close >  p1: wave3_triggered
        k=3  wave3_extended_awaiting_w4_pullback            (watch ~0.382 of W3)
        k=4  close <= p3: wave4_done_awaiting_w5_break
             close >  p3: wave5_running
        k=5  impulse_complete_reversal_watch

    Every stage carries trigger_level / invalidation / targets where they exist.
    """
    n = len(closes)
    close_now = float(closes[-1])
    alts = _trailing_alternating(swings, max_points=6)

    def _mirror(x, up):
        return x if up else -x

    for direction in ("up", "down"):
        impulse_type = "high" if direction == "up" else "low"
        for k in (5, 4, 3, 2, 1):
            seg = alts[-(k + 1):]
            if len(seg) < k + 1:
                continue
            # parity: odd positions must be impulse-side points
            parity_ok = all((seg[i]["type"] == impulse_type) == (i % 2 == 1)
                            for i in range(k + 1))
            if not parity_ok:
                continue

            pts = {f"p{i}": seg[i] for i in range(k + 1)}
            up = direction == "up"

            if k >= 2 and _mirror(pts["p2"]["price"] - pts["p0"]["price"], up) <= 0:
                continue                                    # rule 1 broken
            if k >= 3 and _mirror(pts["p3"]["price"] - pts["p1"]["price"], up) <= 0:
                continue                                    # rule 3 broken
            if k >= 4:
                w1_lo = min(pts["p0"]["price"], pts["p1"]["price"])
                w1_hi = max(pts["p0"]["price"], pts["p1"]["price"])
                if w1_lo <= pts["p4"]["price"] <= w1_hi:
                    continue                                # rule 4 broken
            if k == 5:
                rules = check_five_rules({"points": pts, "direction": direction})
                if not rules["valid"]:
                    continue

            out = {
                "found": True,
                "k": k,
                "direction": direction,
                "points": {key: {"index": int(p["index"]), "price": float(p["price"]),
                                 "type": p["type"]} for key, p in pts.items()},
                "stage": None,
                "trigger_level": None,
                "invalidation": None,
                "targets": {},
                "bars_since_last_swing": n - 1 - int(seg[-1]["index"]),
                "stale": False,
            }
            w1_len = abs(pts["p1"]["price"] - pts["p0"]["price"])

            if k == 1:
                out["stage"] = "wave1_developing"
                out["invalidation"] = float(pts["p0"]["price"])
            elif k == 2:
                trigger = float(pts["p1"]["price"])
                out.update({
                    "trigger_level": trigger,
                    "invalidation": float(pts["p0"]["price"]),
                    "targets": {"wave3_ext_1618": round(
                        pts["p2"]["price"] + _mirror(w1_len * 1.618, up), 10)},
                })
                broke = close_now > trigger if up else close_now < trigger
                out["stage"] = "wave3_triggered" if broke \
                    else "wave2_holding_awaiting_w3_break"
            elif k == 3:
                w3_len = abs(pts["p3"]["price"] - pts["p2"]["price"])
                zone = pts["p3"]["price"] - _mirror(w3_len * 0.382, up)
                out.update({
                    "stage": "wave3_extended_awaiting_w4_pullback",
                    "targets": {"wave4_zone_0382": round(zone, 10)},
                })
            elif k == 4:
                trigger = float(pts["p3"]["price"])
                span_w1_w3 = abs(pts["p3"]["price"] - pts["p0"]["price"])
                out.update({
                    "trigger_level": trigger,
                    "invalidation": float(pts["p4"]["price"]),
                    "targets": {
                        "wave5_equality": round(
                            pts["p4"]["price"] + _mirror(w1_len, up), 10),
                        "wave5_ext_0618": round(
                            pts["p4"]["price"] + _mirror(span_w1_w3 * 0.618, up), 10),
                    },
                })
                broke = close_now > trigger if up else close_now < trigger
                out["stage"] = "wave5_running" if broke \
                    else "wave4_done_awaiting_w5_break"
            else:
                out["stage"] = "impulse_complete_reversal_watch"

            return out

    return {"found": False, "k": 0, "direction": None, "points": {},
            "stage": "no_alternating_structure", "trigger_level": None,
            "invalidation": None, "targets": {},
            "bars_since_last_swing": None, "stale": False}


# ---------------------------------------------------------------------------
# 8. Light A-B-C correction recognizer (context only Ã¢â‚¬â€ no scoring weight)
# ---------------------------------------------------------------------------

def analyze_correction(swings):
    """
    Recognizes a simple zigzag A-B-C in the last three alternating swings:
    C extends beyond A, B failed before the pre-correction origin.
    Heuristic context for 'is the current move corrective?', reported only.
    """
    none_result = {"found": False, "direction": None,
                   "c_over_a_ratio": None, "detail": "no A-B-C structure"}
    if len(swings) < 4:
        return none_result

    origin, a, b, c = swings[-4], swings[-3], swings[-2], swings[-1]
    types = (origin["type"], a["type"], b["type"], c["type"])

    if c["type"] == "low" and types[1:] == ("low", "high", "low"):
        down = True
    elif c["type"] == "high" and types[1:] == ("high", "low", "high"):
        down = False
    else:
        return none_result

    if down:
        c_beyond_a = c["price"] < a["price"]
        b_below_origin = b["price"] < origin["price"]
    else:
        c_beyond_a = c["price"] > a["price"]
        b_below_origin = b["price"] > origin["price"]
    if not (c_beyond_a and b_below_origin):
        return none_result

    ab = abs(b["price"] - a["price"])
    ac_ext = abs(c["price"] - a["price"])
    ratio = round(ac_ext / ab, 2) if ab > 0 else None

    return {
        "found": True,
        "direction": "down" if down else "up",
        "c_over_a_ratio": ratio,
        "detail": f"{'down' if down else 'up'} correction: "
                  f"C extends {'%.2f' % ratio if ratio is not None else '?'}x beyond A",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_elliott_wave(opens, highs, lows, closes, volumes=None,
                         left=3, right=3, method="wick",
                         recency_max_bars=150):
    swings = find_swings(highs, lows, closes, left=left, right=right, method=method)
    candidate = get_candidate_motive_wave(swings)
    position = detect_wave_position(swings, closes)
    correction = analyze_correction(swings)

    def _sanitize(points):
        return {key: {"index": int(p["index"]), "price": float(p["price"]),
                      "type": p["type"]} for key, p in points.items()}

    if candidate is None:
        stale = bool(position.get("bars_since_last_swing") is not None and
                     recency_max_bars and
                     position["bars_since_last_swing"] > recency_max_bars)
        return {
            "candidate_found": False,
            "count_valid": False,
            "elliott_score": 0,
            "trade_ready": False,
            "direction": None,
            "points": {},
            "rules": {"valid": False, "broken": []},
            "truncation": {"truncated": False},
            "fib_ratios": {"score": 0, "detail": {}},
            "alternation": {"score": 0, "alternates": False},
            "wave5_divergence": {"score": 0, "divergence": False,
                                 "rsi_divergence": False, "macd_divergence": False},
            "position": {**position, "stale": stale},
            "correction": correction,
            "report": "No clean alternating 6-point candidate found in the "
                      "last swings Ã¢â‚¬â€ no valid completed Elliott count right "
                      "now. This is expected most of the time.",
        }

    rules = check_five_rules(candidate)
    truncation = check_truncation(candidate)
    fib = score_fib_ratios(candidate)
    alternation = score_alternation(candidate)
    divergence = score_wave5_divergence(candidate, closes)

    # Hard gate: only the 5 actual EWT rules gate validity. Truncation and
    # divergence are warnings/scoring inputs, not invalidation Ã¢â‚¬â€ a
    # truncated wave 5 is still a real (if weak) wave 5, per the course.
    count_valid = rules["valid"]

    raw_score = fib["score"] + alternation["score"] + divergence["score"]
    elliott_score = round(raw_score, 1) if count_valid else 0

    bars_since_p5 = len(closes) - 1 - int(candidate["points"]["p5"]["index"])
    stale = bool(recency_max_bars and bars_since_p5 > recency_max_bars)

    # trade_ready: legal count + meaningful fib confluence + not stale.
    trade_ready = count_valid and elliott_score >= 50 and not stale

    report = _build_report(candidate, rules, truncation, fib, alternation,
                           divergence, elliott_score, count_valid, trade_ready,
                           position, correction, stale, bars_since_p5)

    return {
        "candidate_found": True,
        "direction": candidate["direction"],
        "points": _sanitize(candidate["points"]),
        "rules": rules,
        "count_valid": count_valid,
        "truncation": truncation,
        "fib_ratios": fib,
        "alternation": alternation,
        "wave5_divergence": divergence,
        "elliott_score": elliott_score,
        "trade_ready": trade_ready,
        "stale": stale,
        "bars_since_p5": bars_since_p5,
        "position": position,
        "correction": correction,
        "report": report,
    }


def _build_report(candidate, rules, truncation, fib, alternation, divergence,
                   score, count_valid, trade_ready,
                   position=None, correction=None, stale=False, bars_since_p5=0):
    pts = candidate["points"]
    direction = candidate["direction"]
    lines = []
    lines.append("ELLIOTT WAVE RULE VALIDATOR")
    lines.append("=" * 44)
    lines.append(f"Candidate direction: {direction.upper()}")
    lines.append(f"Points: 0={pts['p0']['price']:.5f} 1={pts['p1']['price']:.5f} "
                 f"2={pts['p2']['price']:.5f} 3={pts['p3']['price']:.5f} "
                 f"4={pts['p4']['price']:.5f} 5={pts['p5']['price']:.5f}")
    lines.append(f"Bars since wave-5 point: {bars_since_p5}"
                 f"{'  (STALE Ã¢â‚¬â€ beyond recency guard)' if stale else ''}")
    lines.append("")

    if position:
        pos = position
        lines.append(f"POSITION IN COUNT: {pos.get('stage')} "
                     f"(depth k={pos.get('k')}, {pos.get('direction')}-count)")
        if pos.get("trigger_level") is not None:
            lines.append(f"  Next-wave trigger: {pos['trigger_level']:.5f}")
        if pos.get("invalidation") is not None:
            lines.append(f"  Invalidation: {pos['invalidation']:.5f}")
        for tname, tval in (pos.get("targets") or {}).items():
            lines.append(f"  Target {tname}: {tval}")
        lines.append("")

    if correction and correction.get("found"):
        lines.append(f"Correction context: {correction['detail']}")

    lines.append("")

    lines.append(f"5 Unbreakable Rules: {'PASS' if rules['valid'] else 'FAIL'}")
    if rules["broken"]:
        for b in rules["broken"]:
            lines.append(f"  - {b}")
    lines.append("")

    if count_valid:
        lines.append("Fibonacci ratio confluence:")
        for k, v in fib["detail"].items():
            lines.append(f"  {k}: {v}")
        lines.append(f"  -> fib score: {fib['score']}/70")
        lines.append("")

        lines.append(f"Alternation: {'YES' if alternation['alternates'] else 'no'} "
                     f"(wave2 sharpness {alternation['wave2_sharpness']} vs "
                     f"wave4 sharpness {alternation['wave4_sharpness']})")
        lines.append(f"  -> alternation score: {alternation['score']}/15")
        lines.append("")

        lines.append(f"Wave 5 divergence: {divergence['detail']}")
        lines.append(f"  -> divergence score: {divergence['score']}/15")
        lines.append("")

        lines.append(f"Truncation warning: {'YES - wave 5 failed to exceed wave 3' if truncation['truncated'] else 'no'}")
        lines.append("")

    lines.append(f"ELLIOTT SCORE: {score}/100")
    lines.append(f"TRADE READY: {'YES' if trade_ready else 'NO'} "
                 f"(valid count + score >= 50 + not stale)")
    lines.append("")
    lines.append("Note: this validator does NOT auto-count waves in real time.")
    lines.append("It only validates whether the LAST 6 swing points form a")
    lines.append("structurally legal Elliott impulse, then scores Fibonacci")
    lines.append("confluence on top. Subjective Ã¢â‚¬â€ use as context, not a solo signal.")

    return "\n".join(lines)


if __name__ == "__main__":
    import random, math
    random.seed(3)
    # Construct a rough impulsive 1-2-3-4-5 shape (6 anchor points), plus
    # a short lead-in and tail so the first and last fractals confirm.
    targets = [100, 92, 108, 103, 122, 114, 126, 124]
    seg = 40
    opens, highs, lows, closes = [], [], [], []
    p = targets[0]
    for s in range(len(targets) - 1):
        for i in range(seg):
            t = i / seg
            p = targets[s] + (targets[s + 1] - targets[s]) * t
            wiggle = math.sin(i / 4) * 0.15
            op = p
            cl = p + wiggle
            hi = max(op, cl) + abs(random.uniform(0.05, 0.2))
            lo = min(op, cl) - abs(random.uniform(0.05, 0.2))
            opens.append(op); highs.append(hi); lows.append(lo); closes.append(cl)

    result = analyze_elliott_wave(opens, highs, lows, closes, left=3, right=3)
    print(result["report"])
    print()
    print("elliott_score:", result["elliott_score"], "| trade_ready:", result["trade_ready"])
