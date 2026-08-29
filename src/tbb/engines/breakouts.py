"""
breakouts.py â€” two SEPARATE breakout detectors, per user requirement:
  1. S/R breakout   â€” price closes beyond a known support/resistance level
  2. Consolidation breakout â€” price closes outside a consolidation zone's high/low

Kept as distinct functions/outputs since a level touch and a range exit
are different events (a level can be touched with no consolidation present,
and vice versa).
"""


def detect_sr_breakout(closes, highs, lows, sr_levels, confirm_closes=1, tolerance_pct=0.1):
    """
    Detect breakout beyond a support/resistance level.

    Args:
        closes, highs, lows: full candle arrays (oldest -> newest)
        sr_levels: output from find_sr_levels()
        confirm_closes: consecutive closes required beyond the level to confirm
        tolerance_pct: buffer % beyond level to count as a real break (avoid wick noise)

    Returns:
        List of breakout events:
        [{"index": int, "level_price": float, "direction": "UP"/"DOWN",
          "level_type": str}, ...]
    """
    events = []
    n = len(closes)

    for lvl in sr_levels:
        level_price = lvl["price"]
        buffer = level_price * (tolerance_pct / 100)

        i = confirm_closes
        while i < n:
            # check UP breakout (close above level+buffer for confirm_closes bars)
            up_ok = all(closes[i - k] > level_price + buffer for k in range(confirm_closes))
            down_ok = all(closes[i - k] < level_price - buffer for k in range(confirm_closes))

            prior_close = closes[i - confirm_closes] if i - confirm_closes >= 0 else None

            if up_ok and prior_close is not None and prior_close <= level_price:
                events.append({
                    "index": i,
                    "level_price": level_price,
                    "direction": "UP",
                    "level_type": lvl["type"],
                })
            elif down_ok and prior_close is not None and prior_close >= level_price:
                events.append({
                    "index": i,
                    "level_price": level_price,
                    "direction": "DOWN",
                    "level_type": lvl["type"],
                })
            i += 1

    events.sort(key=lambda e: e["index"])
    return events


def detect_consolidation_breakout(closes, zones, confirm_closes=1, tolerance_pct=0.1):
    """
    Detect breakout out of a consolidation zone's high/low band.

    Args:
        closes: full candle close array
        zones: output from find_consolidation_zones()
        confirm_closes: consecutive closes required beyond band to confirm
        tolerance_pct: buffer % beyond band edge to count as a real break

    Returns:
        List of breakout events:
        [{"index": int, "zone_high": float, "zone_low": float,
          "direction": "UP"/"DOWN"}, ...]
    """
    events = []
    n = len(closes)

    for zone in zones:
        hi, lo = zone["high"], zone["low"]
        buf_hi = hi * (tolerance_pct / 100)
        buf_lo = lo * (tolerance_pct / 100)

        # only look for breakout after the zone ends
        start_check = zone["end_index"] + 1
        i = max(start_check, confirm_closes)

        while i < n:
            up_ok = all(closes[i - k] > hi + buf_hi for k in range(confirm_closes))
            down_ok = all(closes[i - k] < lo - buf_lo for k in range(confirm_closes))

            if up_ok:
                events.append({
                    "index": i, "zone_high": hi, "zone_low": lo, "direction": "UP",
                })
                break  # one breakout per zone is enough
            elif down_ok:
                events.append({
                    "index": i, "zone_high": hi, "zone_low": lo, "direction": "DOWN",
                })
                break
            i += 1

    events.sort(key=lambda e: e["index"])
    return events
