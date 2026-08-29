"""
Phase 5 Ã¢â‚¬â€ Structure classification + liquidity typing + state machine.
1. Uptrend/downtrend/consolidation via HH/HL vs LL/LH (HL confirmed only after prior HH broken)
2. Break of Structure (BOS) Ã¢â‚¬â€ body-close preferred
3. Trend Change (TC) Ã¢â‚¬â€ needs confirming BOS; fake-TC filter
4. Liquidity types: low-hanging fruit / major (isolated) / reaction areas
5. Origin-of-move target
6. Market mechanism state machine: pullback -> expansion -> take-liquidity

Depends on primitives.get_swing_points.
"""

import pandas as pd
import numpy as np
from tbb.analysis.primitives import get_swing_points


# ---------- 1+2+3. Structure: trend, BOS, TC, fake-TC filter ----------

def classify_structure(df: pd.DataFrame, side_bars: int = 2) -> dict:
    """
    Walks swing points in order, tracking HH/HL (up) or LL/LH (down) sequences.
    Rule: a Higher Low is only confirmed once the prior Higher High is broken (and symmetric for LH).
    Returns dict with:
      - events: list of {idx, price, event} where event in
        ['swing_high','swing_low','BOS_bull','BOS_bear','TC_bull','TC_bear','fake_TC']
      - trend: current trend ('uptrend'/'downtrend'/'consolidation') as of last event
      - structural_points: last confirmed HH/HL or LL/LH
    """
    cols = {c.lower(): c for c in df.columns}
    close = df[cols['close']]
    swings = get_swing_points(df, side_bars)
    if swings.empty:
        return {'events': [], 'trend': 'consolidation', 'structural_points': {}}

    events = []
    trend = 'consolidation'
    last_high = None       # most recent confirmed swing high (price, idx)
    last_low = None        # most recent confirmed swing low (price, idx)
    last_confirmed_hh = None
    last_confirmed_hl = None
    last_confirmed_ll = None
    last_confirmed_lh = None
    pending_tc = None      # 'bull' or 'bear' TC awaiting BOS confirmation
    tc_origin_price = None

    for _, sw in swings.iterrows():
        idx, price, typ = sw['index'], sw['price'], sw['type']
        events.append({'idx': idx, 'price': price, 'event': f'swing_{typ}'})

        # Check BOS against most recent opposite-type structural point
        if typ == 'high':
            if last_confirmed_hh is not None and price > last_confirmed_hh[0]:
                events.append({'idx': idx, 'price': price, 'event': 'BOS_bull'})
                if trend != 'uptrend':
                    trend = 'uptrend'
                last_confirmed_hh = (price, idx)
                if pending_tc == 'bull':
                    pending_tc, tc_origin_price = None, None  # confirmed reversal to up
            elif last_confirmed_hh is None:
                last_confirmed_hh = (price, idx)

            # TC check: bearish TC = breaking below last confirmed HL while in uptrend
            if trend == 'uptrend' and last_confirmed_hl is not None:
                pass  # TC triggered on close breaks, handled below via close series

            last_high = (price, idx)

        elif typ == 'low':
            if last_confirmed_ll is not None and price < last_confirmed_ll[0]:
                events.append({'idx': idx, 'price': price, 'event': 'BOS_bear'})
                if trend != 'downtrend':
                    trend = 'downtrend'
                last_confirmed_ll = (price, idx)
                if pending_tc == 'bear':
                    pending_tc, tc_origin_price = None, None
            elif last_confirmed_ll is None:
                last_confirmed_ll = (price, idx)

            last_low = (price, idx)

        # HL confirmation: only once prior HH is broken (i.e., a BOS_bull happened after this low)
        if typ == 'low' and last_confirmed_hh is not None and last_low is not None:
            if events and events[-1]['event'] == 'BOS_bull':
                last_confirmed_hl = last_low
        if typ == 'high' and last_confirmed_ll is not None and last_high is not None:
            if events and events[-1]['event'] == 'BOS_bear':
                last_confirmed_lh = last_high

    # TC detection via close breaking last confirmed HL (uptrend) / LH (downtrend)
    tc_events = []
    if last_confirmed_hl is not None:
        hl_price, hl_idx = last_confirmed_hl
        after = close.loc[hl_idx:]
        breaks = after[after < hl_price]
        if not breaks.empty:
            tc_idx = breaks.index[0]
            tc_events.append({'idx': tc_idx, 'price': breaks.iloc[0], 'event': 'TC_bear'})
    if last_confirmed_lh is not None:
        lh_price, lh_idx = last_confirmed_lh
        after = close.loc[lh_idx:]
        breaks = after[after > lh_price]
        if not breaks.empty:
            tc_idx = breaks.index[0]
            tc_events.append({'idx': tc_idx, 'price': breaks.iloc[0], 'event': 'TC_bull'})

    events.extend(tc_events)
    # Position lookup built ONCE Ã¢â‚¬â€ the previous lambda called
    # list(df.index).index(...) per event, rescanning all timestamps every
    # time (O(events x bars): ~5M iterations on 6 months of 1H history).
    _pos_of = {ts: i for i, ts in enumerate(df.index)}
    events.sort(key=lambda e: _pos_of.get(e['idx'], 0))

    return {
        'events': events,
        'trend': trend,
        'structural_points': {
            'last_confirmed_hh': last_confirmed_hh,
            'last_confirmed_hl': last_confirmed_hl,
            'last_confirmed_ll': last_confirmed_ll,
            'last_confirmed_lh': last_confirmed_lh,
        },
    }


def check_fake_tc(df: pd.DataFrame, tc_event: dict, structure: dict, max_bars: int = 20) -> bool:
    """
    A TC is 'fake' if price fails to make the confirming new extreme (no BOS)
    within max_bars and instead breaks back beyond the TC origin.
    Returns True if fake.
    """
    cols = {c.lower(): c for c in df.columns}
    close = df[cols['close']]
    idx_list = list(df.index)
    try:
        start_pos = idx_list.index(tc_event['idx'])
    except ValueError:
        return False

    window = idx_list[start_pos:start_pos + max_bars]
    subsequent_bos = [e for e in structure['events']
                       if e['event'] in ('BOS_bull', 'BOS_bear') and e['idx'] in window]
    if subsequent_bos:
        return False  # confirmed by BOS -> real

    # No confirming BOS within window -> check if price reverted back beyond TC origin
    origin_price = tc_event['price']
    later_closes = close.loc[window]
    if tc_event['event'] == 'TC_bear':
        reverted = (later_closes > origin_price).any()
    else:
        reverted = (later_closes < origin_price).any()
    return reverted


# ---------- 4. Liquidity types ----------

def classify_liquidity(df: pd.DataFrame, side_bars: int = 2, current_price: float = None,
                        isolation_bars: int = 30) -> pd.DataFrame:
    """
    low_hanging: nearest swing (high or low) to current_price
    major: isolated swings Ã¢â‚¬â€ not touched (price didn't revisit) for >= isolation_bars
    reaction: prior-trend swings that remain after a TC (approximation: swings older than
               the most recent trend-change point)
    Returns swing points DataFrame with an added 'liquidity_type' column.
    """
    cols = {c.lower(): c for c in df.columns}
    h, l = df[cols['high']], df[cols['low']]
    swings = get_swing_points(df, side_bars)
    if swings.empty:
        return swings

    if current_price is None:
        current_price = df[cols['close']].iloc[-1]

    idx_list = list(df.index)
    liquidity_types = []
    for _, sw in swings.iterrows():
        pos = idx_list.index(sw['index'])
        after = idx_list[pos + 1:]
        touched = False
        for j in after[:isolation_bars]:
            if sw['type'] == 'high' and h.loc[j] >= sw['price']:
                touched = True
                break
            if sw['type'] == 'low' and l.loc[j] <= sw['price']:
                touched = True
                break
        liquidity_types.append('major' if not touched else 'reaction')

    swings = swings.copy()
    swings['liquidity_type'] = liquidity_types

    # nearest swing overall -> reclassify as low_hanging
    swings['dist'] = (swings['price'] - current_price).abs()
    nearest_idx = swings['dist'].idxmin()
    swings.loc[nearest_idx, 'liquidity_type'] = 'low_hanging'
    swings = swings.drop(columns='dist')
    return swings


# ---------- 5. Origin of move ----------

def find_origin_of_move(imbalance_row: dict, structure: dict) -> dict:
    """
    Origin = swing low of a bullish impulse / swing high of a bearish impulse
    that precedes the imbalance's candle2 (the expansion candle).
    Approximated via nearest prior opposite-type structural point before c1_idx.
    """
    direction = imbalance_row['type']
    points = structure['structural_points']
    if direction == 'bullish':
        origin = points.get('last_confirmed_hl') or points.get('last_confirmed_ll')
    else:
        origin = points.get('last_confirmed_lh') or points.get('last_confirmed_hh')

    if origin is None:
        return {'origin_found': False, 'price': None, 'idx': None}
    return {'origin_found': True, 'price': origin[0], 'idx': origin[1]}


# ---------- 6. Market mechanism state machine ----------

def determine_market_phase(df: pd.DataFrame, imbalances: pd.DataFrame, liquidity_points: pd.DataFrame,
                            current_price: float = None) -> str:
    """
    Returns one of: 'offering_fair_value' (pullback into imbalance),
                    'expansion' (retested, pushing to liquidity Ã¢â‚¬â€ best entry phase),
                    'taking_liquidity' (making new swing high/low)
    Heuristic: check most recent untested imbalance vs most recent tested-and-held one
    vs whether price is at a fresh extreme.
    """
    cols = {c.lower(): c for c in df.columns}
    if current_price is None:
        current_price = df[cols['close']].iloc[-1]

    if imbalances.empty:
        return 'offering_fair_value'  # no imbalance context -> default to waiting

    from tbb.analysis.imbalances import mark_tested_imbalances
    tested = mark_tested_imbalances(df, imbalances)
    untested = tested[~tested['tested']]

    if not untested.empty:
        last_untested = untested.iloc[-1]
        zone_mid = (last_untested['zone_low'] + last_untested['zone_high']) / 2
        moving_toward = ((last_untested['type'] == 'bullish' and current_price > zone_mid) or
                          (last_untested['type'] == 'bearish' and current_price < zone_mid))
        if moving_toward:
            return 'offering_fair_value'

    # if price at/near a fresh liquidity extreme -> taking_liquidity
    if not liquidity_points.empty:
        low_hanging = liquidity_points[liquidity_points['liquidity_type'] == 'low_hanging']
        if not low_hanging.empty:
            lh_price = low_hanging.iloc[0]['price']
            if abs(current_price - lh_price) / lh_price < 0.002:
                return 'taking_liquidity'

    return 'expansion'


if __name__ == "__main__":
    np.random.seed(4)
    n = 300
    idx = pd.date_range('2025-01-01', periods=n, freq='1h')
    close = 100 + np.cumsum(np.random.randn(n) * 0.8)
    open_ = close + np.random.randn(n) * 0.3
    high = np.maximum(open_, close) + np.abs(np.random.randn(n) * 0.4)
    low = np.minimum(open_, close) - np.abs(np.random.randn(n) * 0.4)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)

    structure = classify_structure(df)
    print(f"Trend: {structure['trend']}")
    print(f"Event count: {len(structure['events'])}")
    bos_events = [e for e in structure['events'] if 'BOS' in e['event']]
    tc_events = [e for e in structure['events'] if 'TC' in e['event']]
    print(f"BOS events: {len(bos_events)}, TC events: {len(tc_events)}")

    if tc_events:
        fake = check_fake_tc(df, tc_events[0], structure)
        print(f"First TC fake?: {fake}")

    liq = classify_liquidity(df)
    print(f"\nLiquidity types: {liq['liquidity_type'].value_counts().to_dict()}")

    from tbb.analysis.imbalances import detect_imbalances
    imb = detect_imbalances(df)
    if not imb.empty:
        origin = find_origin_of_move(imb.iloc[0].to_dict(), structure)
        print(f"\nOrigin of move (first imbalance): {origin}")

    phase = determine_market_phase(df, imb, liq)
    print(f"\nCurrent market phase: {phase}")
