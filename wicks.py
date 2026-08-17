"""
wicks.py — detects significant wicks (rejection candles).

A "wick" here = the shadow (upper or lower) of a candle. Long wicks vs body
often signal rejection at a price level (stop-hunt, exhaustion).
"""

import numpy as np


def detect_wicks(opens, highs, lows, closes, min_wick_body_ratio=2.0, min_wick_range_pct=0.4):
    """
    Flag candles with a significant upper and/or lower wick.

    Args:
        opens, highs, lows, closes: full candle arrays (oldest -> newest)
        min_wick_body_ratio: wick must be >= this many times the candle body
                              (body near 0 -> ratio auto-treated as large)
        min_wick_range_pct: wick must be >= this % of candle's total range,
                             as a secondary filter for doji-like tiny candles

    Returns:
        List of wick events:
        [{"index": int, "side": "UPPER"/"LOWER", "wick_size": float,
          "body_size": float, "ratio": float}, ...]
    """
    opens = np.asarray(opens, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    events = []
    n = len(closes)

    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        body = abs(c - o)
        candle_range = h - l
        if candle_range <= 0:
            continue

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        for side, wick in (("UPPER", upper_wick), ("LOWER", lower_wick)):
            if wick <= 0:
                continue
            wick_pct_of_range = wick / candle_range * 100
            ratio = wick / body if body > 0 else float("inf")

            if ratio >= min_wick_body_ratio and wick_pct_of_range >= min_wick_range_pct:
                events.append({
                    "index": i,
                    "side": side,
                    "wick_size": float(wick),
                    "body_size": float(body),
                    "ratio": float(ratio) if body > 0 else None,
                })

    return events


def wick_at_level(wick_events, sr_levels, highs, lows, tolerance_pct=0.3):
    """
    Cross-reference wick events against S/R levels — flags rejection wicks
    that occurred right at a known level (higher-confidence signal).

    Args:
        wick_events: output from detect_wicks()
        sr_levels: output from support_resistance.find_sr_levels()
        highs, lows: full candle arrays (for wick tip price)
        tolerance_pct: % distance to count wick tip as "at" the level

    Returns:
        List of wick events enriched with matched level info (subset of input
        that actually touched a level):
        [{...wick fields..., "level_price": float, "level_type": str}, ...]
    """
    matches = []
    for w in wick_events:
        tip_price = highs[w["index"]] if w["side"] == "UPPER" else lows[w["index"]]
        for lvl in sr_levels:
            dist_pct = abs(tip_price - lvl["price"]) / lvl["price"] * 100
            if dist_pct <= tolerance_pct:
                matches.append({**w, "level_price": lvl["price"], "level_type": lvl["type"]})
                break
    return matches
