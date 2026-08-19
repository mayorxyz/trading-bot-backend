"""
sl_tp.py — SL/TP calculator.

SL: uses the WIDER of (ATR-based, swing-based) — avoids getting stopped out
    by normal noise when swing is too close; ATR acts as a noise floor.
TP: uses next S/R level if it meets min R:R, else falls back to fixed R:R.
"""

def calculate_sl(entry_price, direction, atr, swings, atr_mult=1.5, lookback=5):
    """
    direction: "LONG" or "SHORT"
    swings: recent swing list (from zigzag), oldest->newest
    Returns: {"sl_price": float, "method": str, "distance": float}
    """
    atr_distance = atr * atr_mult
    atr_sl = entry_price - atr_distance if direction == "LONG" else entry_price + atr_distance

    # Nearest relevant swing low (LONG) / high (SHORT) in lookback window.
    # The swing must also sit on the correct side of entry — a LONG's stop
    # belongs BELOW entry, a SHORT's ABOVE. entry_price comes from
    # find_best_entry (an S/R level, not the last close), so a recent swing can
    # easily land on the wrong side; using it would invert the stop and produce
    # a "loss" with positive PnL.
    relevant = [s for s in swings[-lookback:]
                if (s["type"] == "LOW" and direction == "LONG" and s["price"] < entry_price)
                or (s["type"] == "HIGH" and direction == "SHORT" and s["price"] > entry_price)]

    if relevant:
        swing_price = relevant[-1]["price"]
        swing_distance = abs(entry_price - swing_price)
    else:
        swing_price = None
        swing_distance = 0

    # pick WIDER (safer) of the two
    if swing_price is not None and swing_distance > atr_distance:
        return {"sl_price": swing_price, "method": "SWING", "distance": swing_distance}
    else:
        return {"sl_price": atr_sl, "method": "ATR", "distance": atr_distance}


def calculate_tp(entry_price, direction, sl_price, sr_levels, min_rr=2.0, fallback_rr=2.0):
    """
    Returns: {"tp_price": float, "method": str, "rr": float}
    """
    risk = abs(entry_price - sl_price)
    if risk <= 0:
        return None

    candidates = [
        lvl for lvl in sr_levels
        if (direction == "LONG" and lvl["price"] > entry_price)
        or (direction == "SHORT" and lvl["price"] < entry_price)
    ]
    candidates.sort(key=lambda l: abs(l["price"] - entry_price))

    for lvl in candidates:
        reward = abs(lvl["price"] - entry_price)
        rr = reward / risk
        if rr >= min_rr:
            return {"tp_price": lvl["price"], "method": "SR_LEVEL", "rr": round(rr, 2)}

    # fallback: fixed R:R
    fallback_price = (entry_price + risk * fallback_rr if direction == "LONG"
                       else entry_price - risk * fallback_rr)
    return {"tp_price": fallback_price, "method": "FIXED_RR", "rr": fallback_rr}


def get_trade_levels(entry_price, direction, atr, swings, sr_levels,
                      atr_mult=1.5, min_rr=2.0, fallback_rr=2.0):
    """One call: returns combined SL + TP result, or None if invalid."""
    sl = calculate_sl(entry_price, direction, atr, swings, atr_mult)
    tp = calculate_tp(entry_price, direction, sl["sl_price"], sr_levels, min_rr, fallback_rr)
    if tp is None:
        return None
    return {"entry": entry_price, "direction": direction, "sl": sl, "tp": tp}
