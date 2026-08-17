"""
Phase 2 — Core signal engine.
1. Imbalance / Fair Value Gap (FVG) detection (3-candle, untested-only)
2. Double Rejection Rule (DR / DRF) — zone hold vs fail
3. Sweep vs Break of liquidity (multi-candle acceptance window)

Depends on phase1_primitives.py (candle_primitives, get_swing_points).
"""

import pandas as pd
import numpy as np
from phase1_primitives import candle_primitives


# ---------- 1. Imbalance / FVG detection ----------

def detect_imbalances(df: pd.DataFrame, min_body_ratio: float = 0.6) -> pd.DataFrame:
    """
    3-candle imbalance: candle2 is expansion, gap between candle1 & candle3.
    Bullish: candle3.low > candle1.high, zone=[c1.high, c3.low]
    Bearish: candle3.high < candle1.low, zone=[c3.high, c1.low]
    Validity: candle2 body_ratio >= min_body_ratio (expansion), candle3 must not
    overlap candle1's extreme (else immediate rebalance -> invalid).

    Returns DataFrame indexed by candle3's index with columns:
    type ('bullish'/'bearish'), zone_low, zone_high, c1_idx, c2_idx, c3_idx, tested (bool, init False)
    """
    prim = candle_primitives(df)
    cols = {c.lower(): c for c in df.columns}
    h, l = df[cols['high']], df[cols['low']]
    idx = df.index

    rows = []
    for i in range(2, len(df)):
        i1, i2, i3 = idx[i - 2], idx[i - 1], idx[i]
        c2_expansion = prim.loc[i2, 'body_ratio'] >= min_body_ratio if not pd.isna(prim.loc[i2, 'body_ratio']) else False
        if not c2_expansion:
            continue

        # Bullish imbalance
        if l.loc[i3] > h.loc[i1]:
            rows.append({
                'index': i3, 'type': 'bullish',
                'zone_low': h.loc[i1], 'zone_high': l.loc[i3],
                'c1_idx': i1, 'c2_idx': i2, 'c3_idx': i3,
                'tested': False,
            })
        # Bearish imbalance
        elif h.loc[i3] < l.loc[i1]:
            rows.append({
                'index': i3, 'type': 'bearish',
                'zone_low': h.loc[i3], 'zone_high': l.loc[i1],
                'c1_idx': i1, 'c2_idx': i2, 'c3_idx': i3,
                'tested': False,
            })

    return pd.DataFrame(rows)


def mark_tested_imbalances(df: pd.DataFrame, imbalances: pd.DataFrame) -> pd.DataFrame:
    """
    Mark each imbalance 'tested' once price first taps the zone after formation.
    Untested-only rule: once tapped, don't reuse. Adds 'tested_at' index.
    """
    if imbalances.empty:
        return imbalances
    cols = {c.lower(): c for c in df.columns}
    h, l = df[cols['high']], df[cols['low']]
    idx_list = list(df.index)

    imbalances = imbalances.copy()
    imbalances['tested_at'] = None

    for row_i, row in imbalances.iterrows():
        start_pos = idx_list.index(row['c3_idx']) + 1
        for pos in range(start_pos, len(idx_list)):
            j = idx_list[pos]
            if l.loc[j] <= row['zone_high'] and h.loc[j] >= row['zone_low']:
                imbalances.at[row_i, 'tested'] = True
                imbalances.at[row_i, 'tested_at'] = j
                break
    return imbalances


# ---------- 2. Double Rejection Rule ----------

def evaluate_double_rejection(df: pd.DataFrame, imbalance_row: pd.Series, max_candles: int = 3) -> dict:
    """
    Evaluate zone hold/fail after tap, per DR/DRF rules:
    - Successful DR: candle1 taps+rejects, candle2 respects candle1 extreme -> holds.
    - DRF: candle2 body-closes beyond candle1's extreme -> failed.
    - SR (tap and go): immediate leave, no lingering -> holds.
    - Immediate failure: candle1 blasts straight through -> failed.
    - >max_candles unresolved -> ignore/drop.

    Returns {'outcome': 'hold'/'fail'/'unresolved', 'resolved_at': idx or None}
    """
    if imbalance_row.get('tested_at') is None:
        return {'outcome': 'untested', 'resolved_at': None}

    idx_list = list(df.index)
    cols = {c.lower(): c for c in df.columns}
    o, h, l, c = df[cols['open']], df[cols['high']], df[cols['low']], df[cols['close']]

    tap_pos = idx_list.index(imbalance_row['tested_at'])
    direction = imbalance_row['type']  # bullish / bearish
    zone_low, zone_high = imbalance_row['zone_low'], imbalance_row['zone_high']

    candle1_idx = idx_list[tap_pos]
    c1_low, c1_high, c1_close = l.loc[candle1_idx], h.loc[candle1_idx], c.loc[candle1_idx]

    # Immediate failure: candle1 blasts through zone and takes opposite side outright
    if direction == 'bullish' and c1_close < zone_low:
        return {'outcome': 'fail', 'resolved_at': candle1_idx}
    if direction == 'bearish' and c1_close > zone_high:
        return {'outcome': 'fail', 'resolved_at': candle1_idx}

    # Look at subsequent candles up to max_candles
    for step in range(1, max_candles + 1):
        pos = tap_pos + step
        if pos >= len(idx_list):
            break
        j = idx_list[pos]
        cj_close = c.loc[j]

        if direction == 'bullish':
            # DRF: body closes beyond candle1's low
            if cj_close < c1_low:
                return {'outcome': 'fail', 'resolved_at': j}
            # Hold: price moves back up cleanly, momentum resumes
            if cj_close > zone_high:
                return {'outcome': 'hold', 'resolved_at': j}
        else:  # bearish
            if cj_close > c1_high:
                return {'outcome': 'fail', 'resolved_at': j}
            if cj_close < zone_low:
                return {'outcome': 'hold', 'resolved_at': j}

    return {'outcome': 'unresolved', 'resolved_at': None}


# ---------- 3. Sweep vs Break of liquidity ----------

def evaluate_sweep_or_break(df: pd.DataFrame, level_price: float, level_idx,
                             direction: str, acceptance_bars: int = 3) -> dict:
    """
    direction: 'above' (level is a high, testing buy-side liquidity) or
               'below' (level is a low, testing sell-side liquidity)
    Judged over acceptance_bars, not a single candle.
    Returns {'outcome': 'sweep'/'break'/'untested', 'resolved_at': idx or None}
    """
    cols = {c.lower(): c for c in df.columns}
    h, l, c = df[cols['high']], df[cols['low']], df[cols['close']]
    idx_list = list(df.index)

    try:
        start_pos = idx_list.index(level_idx) + 1
    except ValueError:
        return {'outcome': 'untested', 'resolved_at': None}

    touched = False
    touch_pos = None
    for pos in range(start_pos, len(idx_list)):
        j = idx_list[pos]
        if direction == 'above' and h.loc[j] >= level_price:
            touched = True
            touch_pos = pos
            break
        if direction == 'below' and l.loc[j] <= level_price:
            touched = True
            touch_pos = pos
            break

    if not touched:
        return {'outcome': 'untested', 'resolved_at': None}

    # Check acceptance over the window following touch
    end_pos = min(touch_pos + acceptance_bars, len(idx_list) - 1)
    window_idx = idx_list[touch_pos:end_pos + 1]
    closes = c.loc[window_idx]

    if direction == 'above':
        accepted = (closes > level_price).sum() >= max(2, len(window_idx) - 1)
    else:
        accepted = (closes < level_price).sum() >= max(2, len(window_idx) - 1)

    outcome = 'break' if accepted else 'sweep'
    return {'outcome': outcome, 'resolved_at': window_idx[-1]}


if __name__ == "__main__":
    np.random.seed(2)
    n = 300
    close = 100 + np.cumsum(np.random.randn(n) * 0.8)
    open_ = close + np.random.randn(n) * 0.3
    high = np.maximum(open_, close) + np.abs(np.random.randn(n) * 0.4)
    low = np.minimum(open_, close) - np.abs(np.random.randn(n) * 0.4)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})

    imb = detect_imbalances(df)
    print(f"Imbalances detected: {len(imb)}")
    imb_tested = mark_tested_imbalances(df, imb)
    print(f"Tested: {imb_tested['tested'].sum()} / {len(imb_tested)}")

    if imb_tested['tested'].any():
        row = imb_tested[imb_tested['tested']].iloc[0]
        result = evaluate_double_rejection(df, row)
        print(f"Sample DR evaluation on first tested imbalance: {result}")

        sweep_result = evaluate_sweep_or_break(
            df, level_price=row['zone_high'], level_idx=row['c3_idx'], direction='above'
        )
        print(f"Sample sweep/break evaluation: {sweep_result}")
