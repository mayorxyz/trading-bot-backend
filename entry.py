"""
entry.py — finds the best ENTRY price. Separate from sl_tp.py by design.

Logic: don't enter at current market price. Instead find the nearest
high-strength level (S/R, most-touched) in the trade direction that price
is likely to retest — enter there, since it's the point most likely to be
touched AND react in the expected direction (best historical strength).
"""


def find_best_entry(current_price, direction, sr_levels, max_distance_pct=2.0, min_touches=2):
    """
    direction: "LONG" or "SHORT"
    sr_levels: from support_resistance.find_sr_levels() (sorted by touches desc)

    LONG  -> look for a support level at/below current price (retest on pullback)
    SHORT -> look for a resistance level at/above current price (retest on pullback)

    Returns: {"entry_price": float, "level_touches": int, "level_type": str,
              "distance_pct": float} or None if nothing qualifies
    """
    candidates = []
    for lvl in sr_levels:
        if lvl["touches"] < min_touches:
            continue
        if direction == "LONG" and lvl["price"] <= current_price and lvl["type"] in ("SUPPORT", "BOTH"):
            candidates.append(lvl)
        elif direction == "SHORT" and lvl["price"] >= current_price and lvl["type"] in ("RESISTANCE", "BOTH"):
            candidates.append(lvl)

    if not candidates:
        return None

    # filter to within max distance
    in_range = [
        lvl for lvl in candidates
        if abs(current_price - lvl["price"]) / current_price * 100 <= max_distance_pct
    ]
    if not in_range:
        return None

    # pick the STRONGEST (most touches) level within range, not just nearest —
    # this is the "highest probability of touching + reacting" level
    best = max(in_range, key=lambda l: l["touches"])

    return {
        "entry_price": best["price"],
        "level_touches": best["touches"],
        "level_type": best["type"],
        "distance_pct": round(abs(current_price - best["price"]) / current_price * 100, 3),
    }
