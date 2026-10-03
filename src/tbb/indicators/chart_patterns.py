"""
chart_patterns.py â€” classic chart-pattern detection from swing topology.

Self-contained: NumPy only, no project imports, works on raw OHLC arrays.
Same philosophy as breakout.py: patterns are identified from swing-point
relationships and fitted levels, never visual template matching. Shape names
are labels for reporting; every pattern exposes its actionable parts:

    trigger_level    price whose break confirms the trade
    invalidation     price that voids the pattern (stop-loss anchor)
    measured_target  classical measured-move projection (may be None)

Detected families:
    Reversal      double_top, double_bottom, triple_top, triple_bottom,
                  head_shoulders, inverse_head_shoulders
    Continuation  ascending_triangle, descending_triangle,
                  symmetrical_triangle, bull_flag, bear_flag,
                  bull_pennant, bear_pennant, cup_handle, inverted_cup_handle

Pattern dict contract:
    {
        "pattern":         str   â€” name from the list above
        "family":          "reversal" | "continuation"
        "direction":       "bullish" | "bearish" | None   (trade implication)
        "status":          "forming" | "confirmed"
        "trigger_level":   float â€” breakout entry reference
        "invalidation":    float â€” stop anchor
        "measured_target": float | None
        "completed_index": int   â€” bar index where status was decided
        "bars_formed":     int   â€” formation length (80-bar rule input)
        "touches":         int   â€” key-level touch count
        "points":          list of defining swings [{"index","price","type"}]
    }
detect_chart_patterns() runs every detector over the last `lookback` bars and
returns a report dict with all matches sorted most-recent first.
"""

import numpy as np


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def find_fractal_swings(highs, lows, left=3, right=3):
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    n = len(highs)
    swings = []
    for i in range(left, n - right):
        window_h = highs[i - left:i + right + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == left:
            swings.append({"index": int(i), "price": float(highs[i]), "type": "high"})
        window_l = lows[i - left:i + right + 1]
        if lows[i] == window_l.min() and np.argmin(window_l) == left:
            swings.append({"index": int(i), "price": float(lows[i]), "type": "low"})
    swings.sort(key=lambda s: s["index"])
    return swings


def _fit_line(points):
    if len(points) < 2:
        return None
    xs = np.array([p["index"] for p in points], dtype=float)
    ys = np.array([p["price"] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return {"slope": float(slope), "intercept": float(intercept)}


def _line_at(line, index):
    return line["slope"] * index + line["intercept"]


def _within_band(prices, tol_pct):
    """True when max-min of the prices is within tol_pct of their average."""
    prices = list(prices)
    if len(prices) < 2:
        return False
    avg = sum(abs(p) for p in prices) / len(prices)
    if avg == 0:
        return False
    return (max(prices) - min(prices)) / avg <= tol_pct


def _is_flat(line, span_bars, ref_price, flat_pct):
    """Scale-independent flatness: total rise over span vs price level."""
    if line is None or ref_price == 0 or span_bars <= 0:
        return False
    return abs(line["slope"]) * span_bars / ref_price <= flat_pct


# ---------------------------------------------------------------------------
# Reversal: double / triple tops and bottoms
# ---------------------------------------------------------------------------

def _detect_multi_top(swings, closes, side, count, lookback_start,
                      tol_pct=0.012, min_depth_pct=0.005):
    """
    side="top"  -> double/triple top  (bearish, trigger below neckline)
    side="bottom" -> double/triple bottom (bullish, trigger above neckline)
    count=2 or 3 equal extremes; neckline through intervening opposite swings.
    """
    ext_type = "high" if side == "top" else "low"
    opp_type = "low" if side == "top" else "high"
    extremes = [s for s in swings if s["type"] == ext_type and s["index"] >= lookback_start]
    opposites = [s for s in swings if s["type"] == opp_type]

    best = None
    for take in range(count, len(extremes) + 1):
        group = extremes[-take:]
        if len(group) < count or not _within_band([p["price"] for p in group], tol_pct):
            continue
        inner = [o for o in opposites if group[0]["index"] < o["index"] < group[-1]["index"]]
        if len(inner) < count - 1:
            continue
        if side == "top":
            neckline = min(o["price"] for o in inner)
            level_price = sum(p["price"] for p in group) / len(group)
            depth_ok = (level_price - neckline) / neckline >= min_depth_pct
            height = level_price - neckline
            confirmed = any(c < neckline for c in closes[group[-1]["index"] + 1:])
        else:
            neckline = max(o["price"] for o in inner)
            level_price = sum(p["price"] for p in group) / len(group)
            depth_ok = (neckline - level_price) / level_price >= min_depth_pct
            height = neckline - level_price
            confirmed = any(c > neckline for c in closes[group[-1]["index"] + 1:])
        if not depth_ok:
            continue
        candidate = {
            "pattern": f"{'double' if count == 2 else 'triple'}_{side}",
            "family": "reversal",
            "direction": "bearish" if side == "top" else "bullish",
            "status": "confirmed" if confirmed else "forming",
            "trigger_level": round(float(neckline), 10),
            "invalidation": round(float(max(p["price"] for p in group)) if side == "top"
                                  else min(p["price"] for p in group), 10),
            "measured_target": round(float(neckline - height if side == "top"
                                           else neckline + height), 10),
            "completed_index": group[-1]["index"],
            "bars_formed": group[-1]["index"] - group[0]["index"],
            "touches": len(group),
            "points": [{k: v for k, v in p.items()} for p in group],
        }
        best = candidate  # loop runs widest group last; keep largest touch count
    return best


# ---------------------------------------------------------------------------
# Reversal: head & shoulders (+ inverse)
# ---------------------------------------------------------------------------

def _detect_head_shoulders(swings, closes, side, lookback_start,
                           head_margin_pct=0.005, shoulder_tol_pct=0.02):
    """
    side="top"    -> head & shoulders (bearish): middle high above both
                     shoulders, neckline through the two intervening lows.
    side="bottom" -> inverse H&S (bullish), mirrored.
    Neckline may slope; trigger/invalidation are evaluated at the latest bar.
    """
    ext_type = "high" if side == "top" else "low"
    opp_type = "low" if side == "top" else "high"
    exts = [s for s in swings if s["type"] == ext_type and s["index"] >= lookback_start]
    opps = [s for s in swings if s["type"] == opp_type]

    for i in range(len(exts) - 3, -1, -1):
        l_sh, head, r_sh = exts[i], exts[i + 1], exts[i + 2]
        t1 = [o for o in opps if l_sh["index"] < o["index"] < head["index"]]
        t2 = [o for o in opps if head["index"] < o["index"] < r_sh["index"]]
        if not t1 or not t2:
            continue
        p1, p2 = t1[-1], t2[-1]
        avg_sh = (abs(l_sh["price"]) + abs(r_sh["price"])) / 2

        if side == "top":
            head_ok = (head["price"] >= l_sh["price"] * (1 + head_margin_pct) and
                       head["price"] >= r_sh["price"] * (1 + head_margin_pct))
            shoulders_ok = _within_band([l_sh["price"], r_sh["price"]], shoulder_tol_pct)
        else:
            head_ok = (head["price"] <= l_sh["price"] * (1 - head_margin_pct) and
                       head["price"] <= r_sh["price"] * (1 - head_margin_pct))
            shoulders_ok = _within_band([l_sh["price"], r_sh["price"]], shoulder_tol_pct)
        if not (head_ok and shoulders_ok) or avg_sh == 0:
            continue

        neck = _fit_line([{"index": p1["index"], "price": p1["price"]},
                          {"index": p2["index"], "price": p2["price"]}])
        if neck is None:
            continue
        last_idx = int(len(closes) - 1)
        neck_now = _line_at(neck, last_idx)
        neck_at_head = _line_at(neck, head["index"])
        height = abs(head["price"] - neck_at_head)

        if side == "top":
            confirmed = bool(closes[last_idx] < neck_now)
            target = neck_now - height
            invalidation = max(l_sh["price"], r_sh["price"])
        else:
            confirmed = bool(closes[last_idx] > neck_now)
            target = neck_now + height
            invalidation = min(l_sh["price"], r_sh["price"])

        return {
            "pattern": "head_shoulders" if side == "top" else "inverse_head_shoulders",
            "family": "reversal",
            "direction": "bearish" if side == "top" else "bullish",
            "status": "confirmed" if confirmed else "forming",
            "trigger_level": round(float(neck_now), 10),
            "invalidation": round(float(invalidation), 10),
            "measured_target": round(float(target), 10),
            "completed_index": r_sh["index"],
            "bars_formed": r_sh["index"] - l_sh["index"],
            "touches": 3,
            "points": [l_sh, head, r_sh],
        }
    return None


# ---------------------------------------------------------------------------
# Continuation: triangles
# ---------------------------------------------------------------------------

def _detect_triangle(swings, closes, lookback_start, flat_pct=0.002,
                     min_touches=2):
    highs_seq = [s for s in swings if s["type"] == "high" and s["index"] >= lookback_start]
    lows_seq = [s for s in swings if s["type"] == "low" and s["index"] >= lookback_start]
    if len(highs_seq) < min_touches or len(lows_seq) < min_touches:
        return None

    res_line = _fit_line(highs_seq[-min_touches - 1:] if len(highs_seq) > min_touches else highs_seq)
    sup_line = _fit_line(lows_seq[-min_touches - 1:] if len(lows_seq) > min_touches else lows_seq)
    if res_line is None or sup_line is None:
        return None

    start_idx = min(highs_seq[0]["index"], lows_seq[0]["index"])
    span = max(1, int(len(closes) - 1 - start_idx))
    ref_price = float(closes[-1])
    res_flat = _is_flat(res_line, span, ref_price, flat_pct)
    sup_flat = _is_flat(sup_line, span, ref_price, flat_pct)

    name, direction = None, None
    if res_flat and not sup_flat and sup_line["slope"] > 0:
        name, direction = "ascending_triangle", "bullish"
    elif sup_flat and not res_flat and res_line["slope"] < 0:
        name, direction = "descending_triangle", "bearish"
    elif not res_flat and not sup_flat:
        converging = res_line["slope"] < 0 < sup_line["slope"]
        diverging = res_line["slope"] > 0 > sup_line["slope"]
        if converging:
            name, direction = "symmetrical_triangle", None
        elif diverging:
            name, direction = "expanding_triangle", None
    if name is None:
        return None

    last_idx = int(len(closes) - 1)
    res_now = _line_at(res_line, last_idx)
    sup_now = _line_at(sup_line, last_idx)
    height = max(res_now - sup_now, 0)
    if direction == "bullish":
        trigger, invalidation = res_now, sup_now
        confirmed = bool(closes[last_idx] > res_now)
        target = res_now + height
    elif direction == "bearish":
        trigger, invalidation = sup_now, res_now
        confirmed = bool(closes[last_idx] < sup_now)
        target = sup_now - height
    else:
        up_break = bool(closes[last_idx] > res_now)
        down_break = bool(closes[last_idx] < sup_now)
        confirmed = up_break or down_break
        if up_break:
            direction, trigger, invalidation, target = "bullish", res_now, sup_now, res_now + height
        elif down_break:
            direction, trigger, invalidation, target = "bearish", sup_now, res_now, sup_now - height
        else:
            trigger, invalidation, target = res_now, sup_now, None

    return {
        "pattern": name,
        "family": "continuation",
        "direction": direction,
        "status": "confirmed" if confirmed else "forming",
        "trigger_level": round(float(trigger), 10),
        "invalidation": round(float(invalidation), 10),
        "measured_target": round(float(target), 10) if target is not None else None,
        "completed_index": last_idx,
        "bars_formed": last_idx - start_idx,
        "touches": len(highs_seq) + len(lows_seq),
        "points": highs_seq + lows_seq,
        # The course's 80-candle rule: long ranges that break act as reversals.
        "reversal_capable": (last_idx - start_idx) >= 80,
    }


# ---------------------------------------------------------------------------
# Continuation: flags and pennants (pole + small consolidation)
# ---------------------------------------------------------------------------

def _find_pole(highs, lows, closes, pole_lookback, pole_min_move_pct):
    """Strong directional impulse ending at the most recent extreme."""
    n = len(closes)
    if n < pole_lookback + 2:
        return None
    window_hi = int(np.argmax(np.asarray(highs[-pole_lookback:], dtype=float))) + n - pole_lookback
    window_lo = int(np.argmin(np.asarray(lows[-pole_lookback:], dtype=float))) + n - pole_lookback
    base_idx = max(0, min(window_hi, window_lo) - 1)

    up_move = (highs[window_hi] - lows[base_idx]) / lows[base_idx] if lows[base_idx] > 0 else 0
    down_move = (highs[base_idx] - lows[window_lo]) / highs[base_idx] if highs[base_idx] > 0 else 0

    if up_move >= down_move and up_move >= pole_min_move_pct and window_lo < window_hi:
        return {"direction": "bullish", "start_index": base_idx, "end_index": window_hi,
                "move_size": float(up_move)}
    if down_move >= pole_min_move_pct and window_hi < window_lo:
        return {"direction": "bearish", "start_index": base_idx, "end_index": window_lo,
                "move_size": float(down_move)}
    return None


def _detect_flag_or_pennant(swings, highs, lows, closes,
                            pole_lookback=40, pole_min_move_pct=0.03,
                            flag_max_retrace=0.6):
    pole = _find_pole(highs, lows, closes, pole_lookback, pole_min_move_pct)
    if pole is None:
        return None

    after_highs = [s for s in swings if s["type"] == "high" and s["index"] > pole["end_index"]]
    after_lows = [s for s in swings if s["type"] == "low" and s["index"] > pole["end_index"]]
    if len(after_highs) < 2 or len(after_lows) < 2:
        return None

    res_line = _fit_line(after_highs)
    sup_line = _fit_line(after_lows)
    if res_line is None or sup_line is None:
        return None

    last_idx = int(len(closes) - 1)
    res_now = _line_at(res_line, last_idx)
    sup_now = _line_at(sup_line, last_idx)
    pole_size = abs((highs[pole["end_index"]] if pole["direction"] == "bullish"
                     else lows[pole["end_index"]]) -
                    (lows[pole["start_index"]] if pole["direction"] == "bullish"
                     else highs[pole["start_index"]]))
    channel_width = max(abs(res_now - sup_now), 1e-12)

    if pole["direction"] == "bullish":
        deepest_low = min(s["price"] for s in after_lows)
        retrace = (highs[pole["end_index"]] - deepest_low) / pole_size if pole_size > 0 else 1
        parallel = res_line["slope"] < 0 and sup_line["slope"] < 0
        converging = res_line["slope"] < 0 < sup_line["slope"]
        trigger, invalidation = res_now, sup_now
        confirmed = bool(closes[last_idx] > res_now)
        target = res_now + pole_size
    else:
        highest_rally = max(s["price"] for s in after_highs)
        retrace = (highest_rally - lows[pole["end_index"]]) / pole_size if pole_size > 0 else 1
        parallel = res_line["slope"] > 0 and sup_line["slope"] > 0
        converging = sup_line["slope"] > 0 > res_line["slope"]
        trigger, invalidation = sup_now, res_now
        confirmed = bool(closes[last_idx] < sup_now)
        target = sup_now - pole_size

    if retrace > flag_max_retrace or channel_width > pole_size:
        return None

    if parallel:
        name = "bull_flag" if pole["direction"] == "bullish" else "bear_flag"
    elif converging:
        name = "bull_pennant" if pole["direction"] == "bullish" else "bear_pennant"
    else:
        return None

    return {
        "pattern": name,
        "family": "continuation",
        "direction": pole["direction"],
        "status": "confirmed" if confirmed else "forming",
        "trigger_level": round(float(trigger), 10),
        "invalidation": round(float(invalidation), 10),
        "measured_target": round(float(target), 10),
        "completed_index": last_idx,
        "bars_formed": last_idx - pole["end_index"],
        "touches": len(after_highs) + len(after_lows),
        "points": after_highs + after_lows,
        "pole": {k: pole[k] for k in ("direction", "start_index", "end_index", "move_size")},
    }


# ---------------------------------------------------------------------------
# Continuation: cup & handle (+ inverted)
# ---------------------------------------------------------------------------

def _detect_cup(swings, closes, side, lookback_start,
                min_depth_pct=0.08, rim_tol_pct=0.02, handle_depth_frac=0.33):
    """
    Approximation via swing topology: deep rounded base between two rims at
    similar price, then a shallow handle pullback near the right rim.
    Rounded-bottom curvature itself is NOT fitted â€” this stays honest about
    being a coarse filter compared to the other detectors.
    """
    highs_seq = [s for s in swings if s["type"] == "high" and s["index"] >= lookback_start]
    lows_seq = [s for s in swings if s["type"] == "low" and s["index"] >= lookback_start]
    if len(highs_seq) < 2 or len(lows_seq) < 1:
        return None

    base = (min if side == "cup" else max)(lows_seq, key=lambda s: s["price"])
    left_rims = [s for s in highs_seq if s["index"] < base["index"]]
    right_rims = [s for s in highs_seq if s["index"] > base["index"]]
    if not left_rims or not right_rims:
        return None

    left_rim, right_rim = left_rims[-1], right_rims[-1]
    if right_rim["index"] <= base["index"]:
        return None
    rim_avg = (left_rim["price"] + right_rim["price"]) / 2
    if not _within_band([left_rim["price"], right_rim["price"]], rim_tol_pct):
        return None

    depth = (rim_avg - base["price"]) / rim_avg if side == "cup" \
        else (base["price"] - rim_avg) / rim_avg
    if depth < min_depth_pct:
        return None

    handle_zone = rim_avg - handle_depth_frac * abs(rim_avg - base["price"]) if side == "cup" \
        else rim_avg + handle_depth_frac * abs(base["price"] - rim_avg)
    handles = ([s for s in lows_seq if base["index"] < s["index"] < right_rim["index"]]
               if side == "cup" else
               [s for s in highs_seq if base["index"] < s["index"] < right_rim["index"]])
    handle_ok = True
    handle_point = None
    if handles:
        if side == "cup":
            handle_point = max(handles, key=lambda s: s["index"])
            handle_ok = handle_point["price"] >= handle_zone
        else:
            handle_point = max(handles, key=lambda s: s["index"])
            handle_ok = handle_point["price"] <= handle_zone
    if not handle_ok:
        return None

    last_idx = int(len(closes) - 1)
    trigger = right_rim["price"]
    if side == "cup":
        confirmed = bool(closes[last_idx] > trigger)
        invalidation = base["price"]
        target = trigger + abs(rim_avg - base["price"])
    else:
        confirmed = bool(closes[last_idx] < trigger)
        invalidation = base["price"]
        target = trigger - abs(base["price"] - rim_avg)

    return {
        "pattern": "cup_handle" if side == "cup" else "inverted_cup_handle",
        "family": "continuation",
        "direction": "bullish" if side == "cup" else "bearish",
        "status": "confirmed" if confirmed else "forming",
        "trigger_level": round(float(trigger), 10),
        "invalidation": round(float(invalidation), 10),
        "measured_target": round(float(target), 10),
        "completed_index": right_rim["index"],
        "bars_formed": right_rim["index"] - left_rim["index"],
        "touches": 2,
        "points": [p for p in (left_rim, base, right_rim, handle_point) if p],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_chart_patterns(highs, lows, closes,
                          swing_left=3, swing_right=3, lookback=120,
                          multi_tol_pct=0.012, min_depth_pct=0.005,
                          head_margin_pct=0.005, shoulder_tol_pct=0.02,
                          triangle_flat_pct=0.002, pole_lookback=40,
                          pole_min_move_pct=0.03, cup_min_depth_pct=0.08):
    """
    Runs every detector over the last `lookback` bars.

    Returns:
        {
            "swings_found": int,
            "lookback_bars": int,
            "patterns": [pattern dicts, most recent completion first],
            "best": highest-touch pattern or None,
            "summary": short human-readable string,
        }
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    lookback_start = max(0, n - lookback)

    swings = find_fractal_swings(highs, lows, left=swing_left, right=swing_right)
    if n < swing_left + swing_right + 5 or len(swings) < 4:
        return {"swings_found": len(swings), "lookback_bars": lookback,
                "patterns": [], "best": None,
                "summary": "not enough bars/swings for chart-pattern detection"}

    found = []
    for count in (3, 2):
        m = _detect_multi_top(swings, closes, "top", count, lookback_start,
                              multi_tol_pct, min_depth_pct)
        if m and not any(p["pattern"] == m["pattern"] for p in found):
            found.append(m)
        b = _detect_multi_top(swings, closes, "bottom", count, lookback_start,
                              multi_tol_pct, min_depth_pct)
        if b and not any(p["pattern"] == b["pattern"] for p in found):
            found.append(b)

    hs = _detect_head_shoulders(swings, closes, "top", lookback_start,
                                head_margin_pct, shoulder_tol_pct)
    if hs:
        found.append(hs)
    ihs = _detect_head_shoulders(swings, closes, "bottom", lookback_start,
                                 head_margin_pct, shoulder_tol_pct)
    if ihs:
        found.append(ihs)

    tri = _detect_triangle(swings, closes, lookback_start, triangle_flat_pct)
    if tri:
        found.append(tri)

    fp = _detect_flag_or_pennant(swings, highs, lows, closes,
                                 pole_lookback, pole_min_move_pct)
    if fp:
        found.append(fp)

    for side in ("cup", "inverted"):
        c = _detect_cup(swings, closes, side, lookback_start, cup_min_depth_pct)
        if c:
            found.append(c)

    found.sort(key=lambda p: (p["completed_index"], p.get("touches", 0)), reverse=True)
    deduped = []
    seen_types = set()
    for p in found:
        if p["pattern"] in seen_types:
            continue
        seen_types.add(p["pattern"])
        deduped.append(p)

    best = None
    if deduped:
        best = max(deduped, key=lambda p: (p.get("touches", 0),
                                           1 if p["status"] == "confirmed" else 0))
    names = ", ".join(f"{p['pattern']}({p['status']})" for p in deduped[:4]) or "none"
    return {
        "swings_found": len(swings),
        "lookback_bars": lookback,
        "patterns": deduped,
        "best": best,
        "summary": names,
    }
