"""
confluence.py — combines all detected signals into one score (0-100).
Higher = more independent signals agree = higher confidence.
"""

WEIGHTS = {
    "pattern_match": 9,
    "trend_align": 9,
    "mtf_alignment": 11,
    "sr_level_strength": 7,
    "wick_rejection": 8,
    "fib_score": 6,                # 0-100 quality from fibonacci.py (scaled)
    "volume_confirm": 5,
    "structure_bos_align": 11,
    "liquidity_target": 5,
    "no_mss_conflict": 5,
    "chart_pattern_align": 6,      # chart-pattern setup (chart_patterns.py)
    "retracement_confirm": 5,      # fib-zone pullback entry (retracement.py)
    "breakout_score": 7,           # 0-100 quality from breakout_engine.py (scaled)
    "elliott_score": 6,            # 0-100 quality from elliott_wave.py (scaled)
}

# Keys scored proportionally to a 0-100 value instead of all-or-nothing.
SCALED_KEYS = ("fib_score", "breakout_score", "elliott_score")


def calculate_confluence(signals: dict) -> dict:
    """
    signals: dict of booleans/values, keys matching WEIGHTS:
        {
          "pattern_match": bool,
          "trend_align": bool,
          "mtf_alignment": bool,
          "sr_level_strength": int (touch count, capped at 6 for scaling),
          "wick_rejection": bool,
          "fib_score": int 0-100 (fibonacci.py quality, scaled),
          "volume_confirm": bool,
          "chart_pattern_align": bool,
          "retracement_confirm": bool,
          "breakout_score": int 0-100 (breakout_engine.py quality, scaled),
          "elliott_score": int 0-100 (elliott_wave.py quality, scaled),
        }
    Scaled keys contribute weight * (value / 100); booleans contribute the
    full weight when truthy. Returns: {"score": int (0-100), "breakdown": {...}}
    """
    score = 0
    breakdown = {}

    for key, weight in WEIGHTS.items():
        val = signals.get(key, False)
        if key == "sr_level_strength":
            points = min(val, 6) / 6 * weight if val else 0
        elif key in SCALED_KEYS:
            try:
                frac = max(0.0, min(float(val), 100.0)) / 100.0
            except (TypeError, ValueError):
                frac = 0.0
            points = weight * frac
        else:
            points = weight if val else 0
        score += points
        breakdown[key] = round(points, 1)

    return {"score": round(score), "breakdown": breakdown}


def confidence_label(score: int) -> str:
    if score >= 75:
        return "HIGH"
    elif score >= 50:
        return "MEDIUM"
    else:
        return "LOW"
