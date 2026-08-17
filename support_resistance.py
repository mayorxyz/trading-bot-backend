"""
support_resistance.py — clusters zigzag swing points into S/R levels.

Depends on zigzag.py output.
"""

import numpy as np


def find_sr_levels(swings, price_tolerance_pct=0.5, min_touches=2):
    """
    Cluster swing points into S/R levels.

    Args:
        swings: output from get_zigzag_swings()
        price_tolerance_pct: % distance for two swings to count as same level
        min_touches: minimum swings needed to confirm a level

    Returns:
        List of levels, sorted by strength (touches desc):
        [{"price": float, "touches": int, "type": "SUPPORT"/"RESISTANCE"/"BOTH",
          "indices": [int,...]}, ...]
    """
    if not swings:
        return []

    used = [False] * len(swings)
    levels = []

    for i, s in enumerate(swings):
        if used[i]:
            continue
        cluster = [i]
        for j in range(i + 1, len(swings)):
            if used[j]:
                continue
            if abs(swings[j]["price"] - s["price"]) / s["price"] * 100 <= price_tolerance_pct:
                cluster.append(j)

        if len(cluster) >= min_touches:
            for idx in cluster:
                used[idx] = True
            cluster_prices = [swings[idx]["price"] for idx in cluster]
            cluster_types = set(swings[idx]["type"] for idx in cluster)
            level_type = "BOTH" if len(cluster_types) > 1 else (
                "RESISTANCE" if "HIGH" in cluster_types else "SUPPORT"
            )
            levels.append({
                "price": float(np.mean(cluster_prices)),
                "touches": len(cluster),
                "type": level_type,
                "indices": [swings[idx]["index"] for idx in cluster],
            })

    levels.sort(key=lambda x: x["touches"], reverse=True)
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
