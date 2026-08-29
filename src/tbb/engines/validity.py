"""
validity.py â€” filters out bad trade setups before they count as a signal.
"""

def validate_trade(trade, current_price, min_rr=2.0, max_entry_distance_pct=2.0,
                    max_sl_distance_pct=5.0, min_sl_distance_pct=0.1):
    """
    trade: output of sl_tp.get_trade_levels()
    Returns: {"valid": bool, "reasons": [str,...]}
    """
    reasons = []
    entry = trade["entry"]
    sl = trade["sl"]["sl_price"]
    tp = trade["tp"]["tp_price"]
    rr = trade["tp"]["rr"]
    direction = trade["direction"]

    # Directional sanity: SL and TP must be on the correct side of entry.
    # Checked explicitly because every distance below uses abs(), which cannot
    # distinguish a valid stop from an inverted one.
    if direction == "LONG":
        if sl >= entry:
            reasons.append(f"inverted SL: {sl} not below LONG entry {entry}")
        if tp <= entry:
            reasons.append(f"inverted TP: {tp} not above LONG entry {entry}")
    else:
        if sl <= entry:
            reasons.append(f"inverted SL: {sl} not above SHORT entry {entry}")
        if tp >= entry:
            reasons.append(f"inverted TP: {tp} not below SHORT entry {entry}")

    entry_dist_pct = abs(current_price - entry) / current_price * 100
    sl_dist_pct = abs(entry - sl) / entry * 100

    if rr < min_rr:
        reasons.append(f"R:R {rr} below minimum {min_rr}")
    if entry_dist_pct > max_entry_distance_pct:
        reasons.append(f"entry {entry_dist_pct:.2f}% away, exceeds {max_entry_distance_pct}%")
    if sl_dist_pct > max_sl_distance_pct:
        reasons.append(f"SL {sl_dist_pct:.2f}% away, too wide (>{max_sl_distance_pct}%)")
    if sl_dist_pct < min_sl_distance_pct:
        reasons.append(f"SL {sl_dist_pct:.2f}% away, too tight (<{min_sl_distance_pct}%)")

    return {"valid": len(reasons) == 0, "reasons": reasons}
