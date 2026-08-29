"""
fibonacci.py â€” standalone Fibonacci analysis engine (v2).

Zero dependencies on other project modules. Only needs NumPy. Works on
raw OHLC(V) arrays. Import and call analyze_fibonacci(), or run standalone.

Covers, end to end:
  1. Swing detection â€” own fractal detector, method="wick" or "close"
  2. Retracement levels + extension levels
  3. Named zones: golden (0.5-0.618), golden_pocket (0.618-0.65),
     sniper (0.382-0.5), beginner (0.382-0.618)
  4. Steepness scoring â€” classifies the anchor leg as steep/moderate/shallow
     and recommends which zone is statistically likely to hold, per the
     "steeper move = shallower retracement" principle
  5. Own EMA calculation (21/50/200) + confluence check against fib levels
  6. Own RSI calculation + basic bullish/bearish divergence detection
  7. Basic candle confirmation at the fib zone (engulfing, pin bar / wick
     rejection, double top/bottom via nearby matching swing prices)
  8. Structure-break confirmation (price breaking the swing immediately
     prior to the current leg â€” used as a trend-continuation trigger)
  9. Invalidation flag â€” retracement breaking past 0.618/1.0 = leg invalid
  10. Extension "measured move" framing (% toward / past the 1.0 target)
  11. Full descriptive report + a single trade_ready bool that folds in
      every confirmation layer (fib zone + MA + candle + structure, RSI
      divergence noted but not required â€” matches how the videos use it
      as a bonus signal, not a hard gate)

Usage:
    from tbb.indicators.fibonacci import analyze_fibonacci
    result = analyze_fibonacci(opens, highs, lows, closes, volumes=None)
    print(result["report"])
"""

import numpy as np

# ---------------------------------------------------------------------------
# Ratio tables
# ---------------------------------------------------------------------------

RETRACEMENT_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.65, 0.786, 1.0]
EXTENSION_RATIOS = [1.0, 1.272, 1.414, 1.618, 2.0, 2.618]

# Named zones â€” (low_ratio, high_ratio). All defined on the 0..1 retracement
# scale; direction-safe ordering is handled in-code.
ZONES = {
    "golden":     (0.5, 0.618),     # classic definition (video 1 / video 3)
    "golden_pocket": (0.618, 0.65), # deeper, narrower, use with caution
    "sniper":     (0.382, 0.5),     # strong/steep-trend continuation zone
    "beginner":   (0.382, 0.618),   # broad "best of both worlds" zone
}


# ---------------------------------------------------------------------------
# 1. Independent swing detection â€” wick or close anchoring
# ---------------------------------------------------------------------------

def find_swings(highs, lows, closes, left=3, right=3, method="wick"):
    """
    Fractal swing detection. method="wick" uses high/low extremes (default,
    matches most-common trader usage). method="close" uses candle closes
    for both swing highs and lows â€” the videos stress picking ONE method
    and staying consistent, so this is a single explicit switch, not a
    per-call mix.
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    if method == "close":
        src_high = closes
        src_low = closes
    else:
        src_high = highs
        src_low = lows

    n = len(src_high)
    swings = []
    for i in range(left, n - right):
        window_h = src_high[i - left:i + right + 1]
        if src_high[i] == window_h.max() and np.argmax(window_h) == left:
            swings.append({"index": i, "price": src_high[i], "type": "high"})

        window_l = src_low[i - left:i + right + 1]
        if src_low[i] == window_l.min() and np.argmin(window_l) == left:
            swings.append({"index": i, "price": src_low[i], "type": "low"})

    swings.sort(key=lambda s: s["index"])
    return swings


def get_last_swing_leg(swings):
    """Most recent swing point + nearest prior opposite-type point = anchor leg."""
    if len(swings) < 2:
        return None
    end = swings[-1]
    start = None
    for s in reversed(swings[:-1]):
        if s["type"] != end["type"]:
            start = s
            break
    if start is None:
        return None
    direction = "up" if end["price"] > start["price"] else "down"
    return {"start": start, "end": end, "direction": direction}


def get_prior_structure_point(swings, leg):
    """
    The swing point immediately before leg['start'] of the SAME type as
    leg['start'] â€” used for structure-break confirmation (price breaking
    this prior point after reversing off a fib zone = trend continuation
    confirmed).
    """
    start_idx = leg["start"]["index"]
    same_type = leg["start"]["type"]
    candidates = [s for s in swings if s["index"] < start_idx and s["type"] == same_type]
    if not candidates:
        return None
    return candidates[-1]


# ---------------------------------------------------------------------------
# 2. Retracement / extension levels
# ---------------------------------------------------------------------------

def calculate_retracements(leg):
    start_price = leg["start"]["price"]
    end_price = leg["end"]["price"]
    span = end_price - start_price
    return {r: end_price - span * r for r in RETRACEMENT_RATIOS}


def calculate_extensions(leg):
    """
    ratio=1.0 is the measured-move objective (100% of the leg projected
    from the retracement low/high). Levels beyond 1.0 represent overshoot
    of that measured move.
    """
    start_price = leg["start"]["price"]
    end_price = leg["end"]["price"]
    span = end_price - start_price
    return {r: end_price + span * r for r in EXTENSION_RATIOS}


def measured_move_progress(current_price, extensions, leg):
    """
    Frames extension distance as % progress toward/past the 1.0 measured
    move, matching how the videos describe extensions (not raw price
    levels, but "how close are we to the objective").
    """
    start = extensions[0.0] if 0.0 in extensions else leg["end"]["price"]
    target = extensions[1.0]
    span = target - start
    if span == 0:
        return 0.0
    progress = (current_price - start) / span * 100
    return round(progress, 1)


# ---------------------------------------------------------------------------
# 3 & 4. Named zones + steepness-based zone recommendation
# ---------------------------------------------------------------------------

def _zone_bounds(retracements, ratio_lo, ratio_hi):
    lo, hi = retracements[ratio_lo], retracements[ratio_hi]
    return (lo, hi) if lo <= hi else (hi, lo)


def get_named_zones(retracements):
    """Returns {zone_name: (price_low, price_high)} for all named zones."""
    return {name: _zone_bounds(retracements, r[0], r[1]) for name, r in ZONES.items()}


def price_in_zone(price, zone_bounds):
    lo, hi = zone_bounds
    return lo <= price <= hi


def classify_steepness(leg, bar_count=None):
    """
    Approximates the leg's "angle" using price span per bar, normalized by
    the leg's own price range so it's scale-independent across assets.
    Steeper/faster moves -> expect shallower retracement (sniper zone,
    0.382-0.5). Shallower/slower moves -> expect deeper retracement
    (golden zone, 0.5-0.618), per video 2's core thesis.

    Returns one of: "steep", "moderate", "shallow", plus the recommended
    zone name and a numeric steepness score (bars-normalized % move per bar).
    """
    start_idx = leg["start"]["index"]
    end_idx = leg["end"]["index"]
    bars = max(end_idx - start_idx, 1)

    start_p = leg["start"]["price"]
    end_p = leg["end"]["price"]
    pct_move = abs(end_p - start_p) / start_p * 100
    pct_per_bar = pct_move / bars

    # Thresholds are heuristic â€” tuned for typical crypto 1h/4h swings.
    # Scale-independent, but if input is a very different timeframe the
    # caller should sanity-check these against their own data.
    if pct_per_bar >= 0.6:
        steepness = "steep"
        recommended_zone = "sniper"
    elif pct_per_bar >= 0.25:
        steepness = "moderate"
        recommended_zone = "beginner"
    else:
        steepness = "shallow"
        recommended_zone = "golden"

    return {
        "steepness": steepness,
        "pct_per_bar": round(pct_per_bar, 4),
        "bars_in_leg": bars,
        "recommended_zone": recommended_zone,
    }


# ---------------------------------------------------------------------------
# 5. Own EMA + MA confluence
# ---------------------------------------------------------------------------

def calculate_ema(closes, period):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * alpha + ema * (1 - alpha)
    return ema


def ma_confluence(current_price, closes, tolerance_pct=0.3, periods=(21, 50, 200)):
    """
    Checks whether any of the given EMAs currently sit near current_price
    â€” used to confirm a fib zone touch is reinforced by a moving average,
    per video 2's core setup (fib + 21/50/200 EMA rejection).
    """
    results = {}
    for p in periods:
        ema_val = calculate_ema(closes, p)
        if ema_val is None:
            results[p] = {"value": None, "touched": False}
            continue
        dist_pct = abs(current_price - ema_val) / current_price * 100
        results[p] = {"value": round(ema_val, 6), "distance_pct": round(dist_pct, 3),
                       "touched": dist_pct <= tolerance_pct}
    any_touched = any(r["touched"] for r in results.values())
    return {"emas": results, "any_touched": any_touched}


# ---------------------------------------------------------------------------
# 6. Own RSI + divergence
# ---------------------------------------------------------------------------

def calculate_rsi(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rsi_values = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi_values.append(100 - (100 / (1 + rs)))
    return np.array(rsi_values)


def detect_divergence(closes, swings, period=14, lookback_swings=2):
    """
    Basic divergence check between the last two same-type swings (both
    lows for bullish divergence, both highs for bearish) â€” price makes a
    lower low while RSI makes a higher low (bullish), or price makes a
    higher high while RSI makes a lower high (bearish).
    Returns {"bullish": bool, "bearish": bool, "detail": str}
    """
    rsi = calculate_rsi(closes, period)
    if rsi is None:
        return {"bullish": False, "bearish": False, "detail": "not enough data for RSI"}

    rsi_offset = len(closes) - len(rsi)  # rsi[i] corresponds to closes[i + rsi_offset + 1]... approx aligned

    lows = [s for s in swings if s["type"] == "low"][-lookback_swings:]
    highs = [s for s in swings if s["type"] == "high"][-lookback_swings:]

    bullish, bearish, detail = False, False, "no divergence detected"

    if len(lows) == 2:
        p1, p2 = lows[0], lows[1]
        idx1 = max(p1["index"] - rsi_offset - 1, 0)
        idx2 = max(p2["index"] - rsi_offset - 1, 0)
        if idx1 < len(rsi) and idx2 < len(rsi):
            price_lower_low = p2["price"] < p1["price"]
            rsi_higher_low = rsi[idx2] > rsi[idx1]
            if price_lower_low and rsi_higher_low:
                bullish = True
                detail = "bullish divergence: price lower low, RSI higher low"

    if len(highs) == 2:
        p1, p2 = highs[0], highs[1]
        idx1 = max(p1["index"] - rsi_offset - 1, 0)
        idx2 = max(p2["index"] - rsi_offset - 1, 0)
        if idx1 < len(rsi) and idx2 < len(rsi):
            price_higher_high = p2["price"] > p1["price"]
            rsi_lower_high = rsi[idx2] < rsi[idx1]
            if price_higher_high and rsi_lower_high:
                bearish = True
                detail = "bearish divergence: price higher high, RSI lower high"

    return {"bullish": bullish, "bearish": bearish, "detail": detail}


# ---------------------------------------------------------------------------
# 7. Candle confirmation at the fib zone
# ---------------------------------------------------------------------------

def check_candle_confirmation(opens, highs, lows, closes, direction, lookback=3):
    """
    Looks at the last `lookback` candles for a bullish/bearish engulfing
    pattern or a rejection wick (pin bar), matching either direction the
    leg needs to continue in. direction: "up" or "down" (leg direction â€”
    an "up" leg pulling back wants a BULLISH confirmation to resume up).
    """
    o, h, l, c = (np.asarray(x, dtype=float) for x in (opens, highs, lows, closes))
    n = len(c)
    if n < 2:
        return {"confirmed": False, "pattern": None}

    want_bullish = direction == "up"

    for i in range(n - 1, max(n - 1 - lookback, 0), -1):
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng == 0:
            continue

        # Engulfing: current body fully engulfs previous body, opposite color
        if i >= 1:
            prev_bullish = c[i - 1] > o[i - 1]
            curr_bullish = c[i] > o[i]
            engulfs = (min(o[i], c[i]) <= min(o[i - 1], c[i - 1]) and
                       max(o[i], c[i]) >= max(o[i - 1], c[i - 1]))
            if engulfs and curr_bullish != prev_bullish:
                if want_bullish and curr_bullish:
                    return {"confirmed": True, "pattern": "bullish_engulfing", "index": i}
                if not want_bullish and not curr_bullish:
                    return {"confirmed": True, "pattern": "bearish_engulfing", "index": i}

        # Pin bar / rejection wick: long wick opposite the reversal direction,
        # small body, wick >= 2x body
        lower_wick = min(o[i], c[i]) - l[i]
        upper_wick = h[i] - max(o[i], c[i])
        if want_bullish and lower_wick >= 2 * body and lower_wick > upper_wick:
            return {"confirmed": True, "pattern": "bullish_pin_bar", "index": i}
        if not want_bullish and upper_wick >= 2 * body and upper_wick > lower_wick:
            return {"confirmed": True, "pattern": "bearish_pin_bar", "index": i}

    return {"confirmed": False, "pattern": None}


# ---------------------------------------------------------------------------
# 8. Structure-break confirmation
# ---------------------------------------------------------------------------

def check_structure_break(closes, leg, swings):
    """
    Confirms trend continuation: after reversing off the fib zone, has
    price broken back above/below the swing point that started the
    CURRENT leg's opposite-direction predecessor? Concretely: for an up
    leg, has price broken above leg['end'] (the swing high) again, or at
    minimum broken above the most recent minor swing high formed during
    the pullback? We use the simplest, most robust version: has the
    latest close broken beyond leg['end']'s price in the leg's direction.
    """
    current_price = closes[-1]
    end_price = leg["end"]["price"]
    if leg["direction"] == "up":
        broken = current_price > end_price
    else:
        broken = current_price < end_price
    return {"broken": broken, "reference_price": end_price}


# ---------------------------------------------------------------------------
# 9. Invalidation check
# ---------------------------------------------------------------------------

def check_invalidation(current_price, retracements, leg):
    """
    A retracement pushing past the 0.618 level and especially past 1.0
    (the full leg origin) is treated as a warning sign / invalidation
    that the leg's trend may be over, per video 3.
    """
    level_618 = retracements[0.618]
    level_100 = retracements[1.0]

    if leg["direction"] == "up":
        broke_618 = current_price < level_618
        broke_100 = current_price < level_100
    else:
        broke_618 = current_price > level_618
        broke_100 = current_price > level_100

    if broke_100:
        status = "invalidated"
    elif broke_618:
        status = "warning"
    else:
        status = "valid"

    return {"status": status, "broke_618": broke_618, "broke_100": broke_100}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_fibonacci(opens, highs, lows, closes, volumes=None,
                       left=3, right=3, method="wick",
                       tolerance_pct=0.15, ma_tolerance_pct=0.3):
    swings = find_swings(highs, lows, closes, left=left, right=right, method=method)
    leg = get_last_swing_leg(swings)

    if leg is None:
        return {
            "leg": None, "retracements": {}, "extensions": {}, "zones": {},
            "steepness": None, "ma_confluence": None, "divergence": None,
            "candle_confirmation": None, "structure_break": None,
            "invalidation": None, "fib_confluence": False, "trade_ready": False,
            "report": "Not enough swing structure found (need >= 2 opposite-type "
                      "swing points). Try more bars or a smaller left/right window.",
        }

    current_price = closes[-1]
    retracements = calculate_retracements(leg)
    extensions = calculate_extensions(leg)
    zones = get_named_zones(retracements)
    steep = classify_steepness(leg)

    # Zone confluence: is price in ANY named zone right now, and specifically
    # in the steepness-recommended zone?
    zone_hits = {name: price_in_zone(current_price, bounds) for name, bounds in zones.items()}
    in_recommended_zone = zone_hits.get(steep["recommended_zone"], False)
    fib_confluence = any(zone_hits.values())

    ma_result = ma_confluence(current_price, closes, tolerance_pct=ma_tolerance_pct)
    divergence = detect_divergence(closes, swings)
    candle = check_candle_confirmation(opens, highs, lows, closes, leg["direction"])
    structure = check_structure_break(closes, leg, swings)
    invalidation = check_invalidation(current_price, retracements, leg)
    mm_progress = measured_move_progress(current_price, extensions, leg)

    # trade_ready folds in the mandatory layers per both videos: fib zone
    # touch + candle confirmation + not invalidated. MA confluence and RSI
    # divergence are bonus confluence, not hard requirements (matches how
    # the videos use them â€” "the more confluences, the more confidence").
    trade_ready = (
        fib_confluence and
        candle["confirmed"] and
        invalidation["status"] != "invalidated"
    )

    report = _build_report(
        leg, retracements, extensions, zones, zone_hits, steep,
        current_price, ma_result, divergence, candle, structure,
        invalidation, mm_progress, trade_ready, in_recommended_zone
    )

    return {
        "leg": leg,
        "retracements": retracements,
        "extensions": extensions,
        "zones": zones,
        "zone_hits": zone_hits,
        "steepness": steep,
        "ma_confluence": ma_result,
        "divergence": divergence,
        "candle_confirmation": candle,
        "structure_break": structure,
        "invalidation": invalidation,
        "measured_move_progress_pct": mm_progress,
        "fib_confluence": fib_confluence,
        "trade_ready": trade_ready,
        "report": report,
    }


def _build_report(leg, retracements, extensions, zones, zone_hits, steep,
                   current_price, ma_result, divergence, candle, structure,
                   invalidation, mm_progress, trade_ready, in_recommended_zone):
    direction = leg["direction"]
    start_p, end_p = leg["start"]["price"], leg["end"]["price"]

    lines = []
    lines.append("FIBONACCI ANALYSIS")
    lines.append("=" * 44)
    lines.append(f"Anchor leg: {direction.upper()}  "
                 f"({leg['start']['type']} {start_p:.5f} -> {leg['end']['type']} {end_p:.5f})")
    lines.append(f"Current price: {current_price:.5f}")
    lines.append(f"Leg steepness: {steep['steepness']} ({steep['pct_per_bar']}%/bar over "
                 f"{steep['bars_in_leg']} bars) -> recommended zone: {steep['recommended_zone']}")
    lines.append("")

    lines.append("Retracement levels:")
    for r in RETRACEMENT_RATIOS:
        marker = "  <-- current" if abs(retracements[r] - current_price) / current_price * 100 <= 0.15 else ""
        lines.append(f"  {r:>6.3f}  {retracements[r]:>12.5f}{marker}")
    lines.append("")

    lines.append("Named zones (price bounds):")
    for name, (lo, hi) in zones.items():
        hit = " <-- PRICE IN ZONE" if zone_hits[name] else ""
        rec = "  [steepness-recommended]" if name == steep["recommended_zone"] else ""
        lines.append(f"  {name:<14} {lo:>12.5f} - {hi:<12.5f}{hit}{rec}")
    lines.append("")

    lines.append("Extension levels (measured-move targets):")
    for r in EXTENSION_RATIOS:
        lines.append(f"  {r:>6.3f}  {extensions[r]:>12.5f}")
    lines.append(f"  Measured-move progress: {mm_progress}% toward/past the 1.0 objective")
    lines.append("")

    lines.append("Confirmation layers:")
    lines.append(f"  MA confluence (21/50/200): {'YES' if ma_result['any_touched'] else 'no'}")
    for p, r in ma_result["emas"].items():
        if r["value"] is not None:
            lines.append(f"    EMA{p}: {r['value']:.5f} ({r['distance_pct']}% away)"
                         f"{'  <-- touched' if r['touched'] else ''}")
    lines.append(f"  RSI divergence: {divergence['detail']}")
    lines.append(f"  Candle confirmation: {'YES â€” ' + candle['pattern'] if candle['confirmed'] else 'none found'}")
    lines.append(f"  Structure break ({'above' if direction == 'up' else 'below'} "
                 f"{structure['reference_price']:.5f}): {'YES' if structure['broken'] else 'not yet'}")
    lines.append(f"  Invalidation status: {invalidation['status'].upper()}"
                 f"{' (broke 0.618)' if invalidation['broke_618'] and invalidation['status']=='warning' else ''}")
    lines.append("")

    lines.append(f"TRADE READY: {'YES' if trade_ready else 'NO'}"
                 f"  (fib zone touch + candle confirmation + not invalidated)")
    if in_recommended_zone:
        lines.append("  Note: price is in the steepness-recommended zone â€” higher-probability setup.")
    lines.append("")

    lines.append("How to read this:")
    if direction == "up":
        lines.append("  Leg is UP. Waiting for a pullback into a named zone, ideally the")
        lines.append("  steepness-recommended one, with a bullish candle confirmation and")
        lines.append("  price later breaking back above the leg's swing high to confirm")
        lines.append("  trend continuation. A break below the 1.0 level invalidates the leg.")
    else:
        lines.append("  Leg is DOWN. Waiting for a relief bounce into a named zone, ideally")
        lines.append("  the steepness-recommended one, with a bearish candle confirmation")
        lines.append("  and price later breaking back below the leg's swing low to confirm")
        lines.append("  trend continuation. A break above the 1.0 level invalidates the leg.")

    return "\n".join(lines)


if __name__ == "__main__":
    import random, math
    random.seed(11)
    n = 220
    opens, highs, lows, closes, vols = [], [], [], [], []
    p = 100.0
    for i in range(n):
        wiggle = math.sin(i / 6) * 0.6  # forces regular local peaks/troughs
        op = p
        if i < 110:
            p += random.uniform(0.1, 0.6) + wiggle
        else:
            p -= random.uniform(0.05, 0.4) + wiggle
        cl = p
        hi = max(op, cl) + random.uniform(0.05, 0.3)
        lo = min(op, cl) - random.uniform(0.05, 0.3)
        opens.append(op); highs.append(hi); lows.append(lo); closes.append(cl)
        vols.append(random.uniform(100, 300))

    result = analyze_fibonacci(opens, highs, lows, closes, volumes=vols, left=2, right=2)
    print(result["report"])
    print()
    print("trade_ready:", result["trade_ready"])
    print("fib_confluence flag for pipeline.py:", result["fib_confluence"])
