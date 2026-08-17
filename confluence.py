"""
confluence.py — combines all detected signals into one score (0-100).
Higher = more independent signals agree = higher confidence.
"""

WEIGHTS = {
    "pattern_match": 20,      # candlestick pattern detected in trade direction
    "trend_align": 20,        # SMA trend agrees with direction
    "mtf_alignment": 20,      # multi-timeframe FULL alignment
    "sr_level_strength": 15,  # entry level touches (scaled)
    "wick_rejection": 10,     # rejection wick at entry level
    "fib_confluence": 10,     # entry near fib 50/61.8
    "volume_confirm": 5,      # volume spike on signal candle
}


def calculate_confluence(signals: dict) -> dict:
    """
    signals: dict of booleans/values, keys matching WEIGHTS:
        {
          "pattern_match": bool,
          "trend_align": bool,
          "mtf_alignment": bool,
          "sr_level_strength": int (touch count, capped at 6 for scaling),
          "wick_rejection": bool,
          "fib_confluence": bool,
          "volume_confirm": bool,
        }
    Returns: {"score": int (0-100), "breakdown": {...}}
    """
    score = 0
    breakdown = {}

    for key, weight in WEIGHTS.items():
        val = signals.get(key, False)
        if key == "sr_level_strength":
            points = min(val, 6) / 6 * weight if val else 0
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
