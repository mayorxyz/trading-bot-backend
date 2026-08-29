"""
breakout_engine.py â€” standalone breakout detection & trade-management engine.

Port of the user's breakout.py design with the review fixes applied:
  - volatility contraction measured on the BASE (excludes breakout bars)
  - scale-independent flat-line detection for shape labels
  - consistent return keys on every path (chandelier_stop always present)
  - JSON-safe float casting
  - demo data the detector can actually find swings in

Named breakout_engine (not breakout) to avoid import confusion with the
existing breakouts.py S/R-consolidation module used by the pipeline.

Zero dependencies on other project modules. Only needs NumPy.

Pattern-agnostic by design: rather than matching visual shapes, this finds
the underlying key levels â€” a resistance trendline/level above and a support
trendline/level below a consolidation â€” and treats every pattern as the same
concept: consolidation -> key levels -> breakout. Shape names are labels
derived from slopes, reported for readability only.

Covers:
  1. Consolidation / key-level detection + shape classification
  2. Breakout detection â€” candle close beyond a key level
  3. False-breakout filters: momentum candle (body ratio + clearance),
     volume spike, base volatility contraction, EMA trend alignment
  4. Entry at momentum-candle close, stop beyond opposite side of the level
     buffered by ATR
  5. Two-step take-profit: TP1 at tp1_rr * risk (close half -> breakeven),
     remainder trails a chandelier stop
  6. compute_breakout_score(result) -> 0-100 confluence contribution

analyze_breakout() returns a dict whose "trade_ready" flag is gated ONLY by
the momentum candle; volume/volatility/trend are supporting confluence.
"""

import numpy as np


# ---------------------------------------------------------------------------
# 0. Shared low-level helpers
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


def calculate_ema(closes, period):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema_series = np.zeros(len(closes))
    ema_series[:period] = np.nan
    ema = closes[:period].mean()
    ema_series[period - 1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * alpha + ema * (1 - alpha)
        ema_series[i] = ema
    return ema_series


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


def _fit_trendline(points):
    """Simple linear regression over (index, price) points -> (slope, intercept)."""
    if len(points) < 2:
        return None
    xs = np.array([p["index"] for p in points], dtype=float)
    ys = np.array([p["price"] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return {"slope": float(slope), "intercept": float(intercept)}


def _is_flat(line, span_bars, ref_price, flat_pct=0.0015):
    """Scale-independent flatness: total rise over span vs price level."""
    if line is None or ref_price == 0 or span_bars <= 0:
        return False
    return abs(line["slope"]) * span_bars / ref_price <= flat_pct


def find_key_levels(highs, lows, closes, left=2, right=2, lookback=60,
                    min_touches=2, flat_pct=0.0015):
    """
    Resistance trendline through recent swing highs, support trendline through
    recent swing lows within the last `lookback` bars; shape classified from
    the two slopes using a scale-independent flat threshold.
    Returns None if fewer than min_touches swing highs or lows exist.
    """
    n = len(closes)
    start = max(0, n - lookback)
    h_slice = highs[start:]
    l_slice = lows[start:]

    swings = find_fractal_swings(h_slice, l_slice, left=left, right=right)
    for s in swings:
        s["index"] += start

    swing_highs = [s for s in swings if s["type"] == "high"]
    swing_lows = [s for s in swings if s["type"] == "low"]

    if len(swing_highs) < min_touches or len(swing_lows) < min_touches:
        return None

    resistance = _fit_trendline(swing_highs[-(min_touches + 1):])
    support = _fit_trendline(swing_lows[-(min_touches + 1):])
    if resistance is None or support is None:
        return None

    ref_price = float(closes[-1])
    shape = _classify_shape(resistance["slope"], support["slope"],
                            n - start, ref_price, flat_pct)

    return {
        "resistance": resistance,
        "support": support,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "shape": shape,
        "touches": {"resistance": len(swing_highs), "support": len(swing_lows)},
    }


def _classify_shape(res_slope, sup_slope, span_bars, ref_price, flat_pct):
    res_flat = _is_flat({"slope": res_slope}, span_bars, ref_price, flat_pct)
    sup_flat = _is_flat({"slope": sup_slope}, span_bars, ref_price, flat_pct)

    if res_flat and sup_flat:
        return "rectangle"
    if res_flat:
        return "ascending_triangle" if sup_slope > 0 else "descending_base"
    if sup_flat:
        return "descending_triangle" if res_slope < 0 else "ascending_base"

    same_direction = (res_slope > 0) == (sup_slope > 0)
    if same_direction:
        return "flag"
    if res_slope < 0 < sup_slope:
        return "symmetrical_triangle_or_pennant"
    return "expanding_wedge"


def level_price_at(trendline, index):
    return trendline["slope"] * index + trendline["intercept"]


# ---------------------------------------------------------------------------
# 2. Breakout detection
# ---------------------------------------------------------------------------

def detect_breakout(closes, key_levels, current_index):
    """
    Checks whether the latest close has broken above resistance or below
    support at the current bar index.
    """
    if key_levels is None:
        return {"direction": None, "level_price": None}

    res_price = level_price_at(key_levels["resistance"], current_index)
    sup_price = level_price_at(key_levels["support"], current_index)
    current_close = float(closes[-1])

    if current_close > res_price:
        return {"direction": "up", "level_price": float(res_price)}
    if current_close < sup_price:
        return {"direction": "down", "level_price": float(sup_price)}
    return {"direction": None, "level_price": None}


# ---------------------------------------------------------------------------
# 3. False-breakout filters
# ---------------------------------------------------------------------------

def check_momentum_candle(opens, highs, lows, closes, direction, level_price,
                          min_body_ratio=0.5, min_clearance_ratio=0.5):
    """
    Momentum candle gate: real body (not doji), close in breakout direction,
    and at least min_clearance_ratio of the body beyond the level.
    """
    o, h, l, c = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
    rng = h - l
    if rng <= 0:
        return {"is_momentum": False, "reason": "zero range candle",
                "body_ratio": 0.0, "clearance_ratio": 0.0, "color_aligned": False}

    body = abs(c - o)
    body_ratio = body / rng
    body_low, body_high = min(o, c), max(o, c)

    if direction == "up":
        clearance = (body_high - max(level_price, body_low)) / body if body > 0 else 0
        color_ok = c > o
    else:
        clearance = (min(level_price, body_high) - body_low) / body if body > 0 else 0
        color_ok = c < o

    return {
        "is_momentum": bool(body_ratio >= min_body_ratio and color_ok and
                            clearance >= min_clearance_ratio),
        "body_ratio": round(float(body_ratio), 3),
        "clearance_ratio": round(float(max(clearance, 0)), 3),
        "color_aligned": bool(color_ok),
    }


def check_volume_spike(volumes, lookback=20, spike_multiple=1.5):
    volumes = np.asarray(volumes, dtype=float)
    if len(volumes) < lookback + 1:
        return {"confirmed": False, "ratio": None,
                "reason": "not enough volume history"}
    avg_vol = volumes[-lookback - 1:-1].mean()
    current_vol = volumes[-1]
    ratio = current_vol / avg_vol if avg_vol > 0 else 0
    return {
        "confirmed": bool(ratio >= spike_multiple),
        "ratio": round(float(ratio), 2),
        "avg_volume": round(float(avg_vol), 2),
        "current_volume": round(float(current_vol), 2),
    }


def check_volatility_contraction(highs, lows, closes, atr_period=14,
                                 base_lookback=20, compare_lookback=40,
                                 exclude_last=2, contraction_threshold=0.85):
    """
    Base filter: mean ATR over the consolidation EXCLUDING the last
    `exclude_last` bars (the expanding breakout candles) versus the longer
    average before it. Comparing ATR including the breakout bar would almost
    never confirm â€” the breakout itself expands volatility.
    """
    atr = calculate_atr(highs, lows, closes, period=atr_period)
    if atr is None:
        return {"confirmed": False, "ratio": None,
                "reason": "not enough data for ATR"}

    end = len(atr) - exclude_last
    start = end - base_lookback
    cmp_start = max(atr_period, start - compare_lookback)
    if start <= cmp_start or end <= start:
        return {"confirmed": False, "ratio": None,
                "reason": "window too small for contraction check"}

    base_mean = float(np.mean(atr[start:end]))
    cmp_mean = float(np.mean(atr[cmp_start:start]))
    if base_mean <= 0 or cmp_mean <= 0:
        return {"confirmed": False, "ratio": None,
                "reason": "non-positive ATR windows"}

    ratio = base_mean / cmp_mean
    return {
        "confirmed": bool(ratio <= contraction_threshold),
        "ratio": round(ratio, 3),
        "base_atr": round(base_mean, 8),
        "compare_atr": round(cmp_mean, 8),
    }


def check_trend_alignment(closes, direction, period=50):
    """Breakout direction agreeing with the broader EMA trend."""
    ema = calculate_ema(closes, period)
    if ema is None:
        return {"aligned": False, "ema_value": None,
                "reason": "not enough data for EMA"}
    ema_now = float(ema[-1])
    if np.isnan(ema_now):
        return {"aligned": False, "ema_value": None, "reason": "EMA not yet formed"}

    current_price = float(closes[-1])
    aligned = current_price > ema_now if direction == "up" else current_price < ema_now
    return {"aligned": bool(aligned), "ema_value": round(ema_now, 6)}


# ---------------------------------------------------------------------------
# 4. Entry / stop logic
# ---------------------------------------------------------------------------

def calculate_entry_stop(closes, direction, level_price,
                         buffer_atr=None, atr_value=None):
    """
    Entry at the momentum candle's close; stop just beyond the opposite side
    of the key level, buffered by a fraction of ATR when available.
    """
    entry = float(closes[-1])
    buffer = (buffer_atr * atr_value) if (buffer_atr and atr_value) else 0.0
    stop = level_price - buffer if direction == "up" else level_price + buffer
    risk = abs(entry - stop)
    return {"entry": entry, "stop": float(stop), "risk": float(risk)}


# ---------------------------------------------------------------------------
# 5. Two-step take-profit system
# ---------------------------------------------------------------------------

def calculate_take_profit_plan(entry, stop, direction, tp1_rr=1.5):
    risk = abs(entry - stop)
    tp1 = entry + risk * tp1_rr if direction == "up" else entry - risk * tp1_rr
    return {
        "tp1": float(tp1),
        "tp1_rr": float(tp1_rr),
        "breakeven_after_tp1": float(entry),
    }


def chandelier_stop(highs, lows, closes, direction, period=22, atr_multiple=2.0):
    """
    Chandelier exit: highest high over `period` minus 2*ATR (long); lowest low
    plus 2*ATR (short). Caller trails it bar-by-bar.
    """
    atr = calculate_atr(highs, lows, closes, period=14)
    if atr is None:
        return None
    current_atr = float(atr[-1])

    highs_arr = np.asarray(highs[-period:], dtype=float)
    lows_arr = np.asarray(lows[-period:], dtype=float)

    if direction == "up":
        return float(highs_arr.max() - atr_multiple * current_atr)
    return float(lows_arr.min() + atr_multiple * current_atr)


# ---------------------------------------------------------------------------
# Confluence scoring export
# ---------------------------------------------------------------------------

def compute_breakout_score(result):
    """
    0-100 quality score for an analyze_breakout() result:
        momentum candle confirmed            +35   (else +10 * body_ratio)
        clearance depth                      +10 * min(clearance, 1)
        volume spike confirmed               +20   (+5 bonus when ratio >= 2x)
        base volatility contraction          +15
        EMA trend alignment                  +15
    No breakout detected -> 0. trade_ready stays momentum-gated regardless;
    the score feeds weighted confluence only, never hard gates.
    """
    if not result or result.get("breakout", {}).get("direction") is None:
        return 0

    momentum = result.get("momentum") or {}
    score = 35.0 if momentum.get("is_momentum") else 10.0 * (momentum.get("body_ratio") or 0)
    score += 10.0 * min(momentum.get("clearance_ratio") or 0, 1)

    vol = result.get("volume") or {}
    if vol.get("confirmed"):
        score += 20.0
        if (vol.get("ratio") or 0) >= 2.0:
            score += 5.0

    if (result.get("volatility") or {}).get("confirmed"):
        score += 15.0

    if (result.get("trend") or {}).get("aligned"):
        score += 15.0

    return int(round(max(0.0, min(score, 100.0))))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_breakout(opens, highs, lows, closes, volumes=None,
                     swing_left=2, swing_right=2, lookback=60,
                     volume_spike_multiple=1.5, trend_period=50,
                     tp1_rr=1.5, chandelier_period=22,
                     chandelier_atr_multiple=2.0):

    opens = np.asarray(opens, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    current_index = len(closes) - 1

    def _empty(extra=None):
        out = {
            "key_levels": None, "breakout": {"direction": None, "level_price": None},
            "momentum": None, "volume": None, "volatility": None, "trend": None,
            "entry_stop": None, "take_profit_plan": None, "chandelier_stop": None,
            "score": 0, "trade_ready": False,
        }
        if extra:
            out.update(extra)
        return out

    key_levels = find_key_levels(highs, lows, closes, left=swing_left,
                                 right=swing_right, lookback=lookback)
    if key_levels is None:
        return _empty({
            "report": "No consolidation with enough swing touches found "
                      f"(need >= {2} swing highs and lows within the last "
                      f"{lookback} bars).",
        })

    breakout = detect_breakout(closes, key_levels, current_index)

    if breakout["direction"] is None:
        empty = _empty({"key_levels": key_levels, "breakout": breakout})
        empty["report"] = (
            f"No breakout yet â€” price inside key levels "
            f"(shape label: {key_levels['shape']})."
        )
        return empty

    momentum = check_momentum_candle(opens, highs, lows, closes,
                                     breakout["direction"], breakout["level_price"])

    volume = (check_volume_spike(volumes, spike_multiple=volume_spike_multiple)
              if volumes is not None else
              {"confirmed": False, "ratio": None, "reason": "no volume data provided"})

    volatility = check_volatility_contraction(highs, lows, closes)
    trend = check_trend_alignment(closes, breakout["direction"], period=trend_period)

    atr_series = calculate_atr(highs, lows, closes)
    atr_now = float(atr_series[-1]) if atr_series is not None else None

    entry_stop = calculate_entry_stop(closes, breakout["direction"],
                                      breakout["level_price"],
                                      buffer_atr=0.25, atr_value=atr_now)
    tp_plan = calculate_take_profit_plan(entry_stop["entry"], entry_stop["stop"],
                                         breakout["direction"], tp1_rr=tp1_rr)
    chandelier = chandelier_stop(highs, lows, closes, breakout["direction"],
                                 period=chandelier_period,
                                 atr_multiple=chandelier_atr_multiple)

    result = {
        "key_levels": key_levels,
        "breakout": breakout,
        "momentum": momentum,
        "volume": volume,
        "volatility": volatility,
        "trend": trend,
        "entry_stop": entry_stop,
        "take_profit_plan": tp_plan,
        "chandelier_stop": chandelier,
        "score": 0,
        # Momentum candle is the mandatory false-breakout gate; everything
        # else is supporting confluence folded into score, not gates.
        "trade_ready": momentum["is_momentum"],
    }
    result["score"] = compute_breakout_score(result)
    return result


if __name__ == "__main__":
    import random, math
    random.seed(42)
    n = 160
    opens, highs, lows, closes, vols = [], [], [], [], []
    p = 100.0
    for i in range(n):
        op = p
        if i < 120:
            # tight consolidation: small noise the fractal detector can resolve
            p = 100 + math.sin(i / 7) * 1.5 + random.uniform(-0.08, 0.08)
        else:
            # breakout: strong directional push
            p += random.uniform(0.8, 1.6)
        cl = p
        hi = max(op, cl) + random.uniform(0.02, 0.1)
        lo = min(op, cl) - random.uniform(0.02, 0.1)
        opens.append(op); highs.append(hi); lows.append(lo); closes.append(cl)
        vol = random.uniform(95, 105) if i < 120 else random.uniform(280, 420)
        vols.append(vol)

    res = analyze_breakout(opens, highs, lows, closes, volumes=vols, lookback=120)
    print("direction:", res["breakout"]["direction"])
    print("momentum:", res["momentum"])
    print("volume:", res["volume"])
    print("volatility:", res["volatility"])
    print("trend:", res["trend"])
    print("score:", res["score"])
    print("trade_ready:", res["trade_ready"])
