"""
support_resistance.py — clusters zigzag swing points into S/R levels.

Depends on zigzag.py output.
"""

import numpy as np


def find_sr_levels(swings, tolerance_pct=0.5):
    if not swings:
        return []

    clusters = []  # each: {"prices": [...], "types": [...]}

    for s in swings:
        price = s["price"]
        # Normalised on ingest: zigzag.get_zigzag_swings emits "HIGH"/"LOW" while
        # phase1_primitives.get_swing_points emits "high"/"low". Comparing against
        # one casing silently counted zero of each, so every cluster fell through
        # to "BOTH" and entry.py's directional filter became a no-op.
        swing_type = str(s["type"]).upper()
        matched = None
        for cl in clusters:
            avg = sum(cl["prices"]) / len(cl["prices"])
            if abs(price - avg) / avg * 100 <= tolerance_pct:
                matched = cl
                break
        if matched:
            matched["prices"].append(price)
            matched["types"].append(swing_type)
        else:
            clusters.append({"prices": [price], "types": [swing_type]})

    levels = []
    for cl in clusters:
        n_high = cl["types"].count("HIGH")
        n_low = cl["types"].count("LOW")
        if n_low > n_high:
            lvl_type = "SUPPORT"
        elif n_high > n_low:
            lvl_type = "RESISTANCE"
        else:
            lvl_type = "BOTH"

        levels.append({
            "price": sum(cl["prices"]) / len(cl["prices"]),
            "touches": len(cl["prices"]),
            "type": lvl_type,
        })

    levels.sort(key=lambda l: l["touches"], reverse=True)
    return levels


def nearest_level(current_price, levels, max_distance_pct=1.0):
    """Return closest S/R level within max_distance_pct, or None."""
    best = None
    best_dist = None
    for lvl in levels:
        dist = abs(current_price - lvl["price"]) / current_price * 100
        if dist <= max_distance_pct and (best_dist is None or dist < best_dist):
            best, best_dist = lvl, dist
    return best
