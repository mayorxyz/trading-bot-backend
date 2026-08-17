"""
consolidation.py — detects consolidation (ranging) zones.

Depends on zigzag.py output. Separate concept from S/R levels:
- S/R = price reacting to a specific level, possibly touched far apart in time
- Consolidation = price trapped in a tight band for N consecutive swings
"""

import numpy as np


def find_consolidation_zones(swings, min_swings=4, max_range_pct=3.0):
    """
    Detect zones where price stayed within a tight band for consecutive swings.

    Args:
        swings: output from get_zigzag_swings()
        min_swings: minimum consecutive swings to count as consolidation
        max_range_pct: max (high-low)/low % for the window to count as tight

    Returns:
        List of zones:
        [{"start_index": int, "end_index": int, "high": float, "low": float,
          "swing_count": int}, ...]
    """
    zones = []
    n = len(swings)
    if n < min_swings:
        return zones

    i = 0
    while i <= n - min_swings:
        window = swings[i:i + min_swings]
        prices = [s["price"] for s in window]
        hi, lo = max(prices), min(prices)
        range_pct = (hi - lo) / lo * 100

        if range_pct <= max_range_pct:
            # extend window as far as it stays tight
            j = i + min_swings
            while j < n:
                test_prices = prices + [swings[j]["price"]]
                hi2, lo2 = max(test_prices), min(test_prices)
                if (hi2 - lo2) / lo2 * 100 <= max_range_pct:
                    prices = test_prices
                    hi, lo = hi2, lo2
                    j += 1
                else:
                    break
            zones.append({
                "start_index": window[0]["index"],
                "end_index": swings[j - 1]["index"],
                "high": float(hi),
                "low": float(lo),
                "swing_count": j - i,
            })
            i = j
        else:
            i += 1

    return zones
