"""
Phase 3 — Regime filters + orchestration + entry model.
1. Regime filters (no-imbalance chop, 2-failure consolidation)
2. Top-down bias (multi-TF alignment, HTF wins conflicts)
3. VSSR entry model (POI tap -> lower-TF imbalance confirmation)
4. Stop-loss / take-profit rules

Depends on phase1_primitives.py, phase2_signal_engine.py.
"""

import pandas as pd
import numpy as np
from phase2_signal_engine import detect_imbalances, mark_tested_imbalances, evaluate_double_rejection


# ---------- 1. Regime filters ----------

def detect_regime(df: pd.DataFrame, imbalances: pd.DataFrame, lookback: int = 50) -> str:
    """
    'chop'          — 0 imbalances in lookback window
    'consolidation' — 2+ consecutive failed imbalances (opposing directions)
    'trending'      — otherwise
    """
    if imbalances.empty:
        return 'chop'

    recent = imbalances[imbalances['c3_idx'].isin(df.index[-lookback:])]
    if recent.empty:
        return 'chop'

    tested = mark_tested_imbalances(df, recent)
    tested = tested[tested['tested']]
    if tested.empty:
        return 'trending'  # imbalances exist but untested yet — not chop

    outcomes = []
    for _, row in tested.iterrows():
        res = evaluate_double_rejection(df, row)
        outcomes.append((row['type'], res['outcome']))

    # find 2 consecutive fails in opposing directions
    fails = [o for o in outcomes if o[1] == 'fail']
    if len(fails) >= 2:
        last_two = fails[-2:]
        if last_two[0][0] != last_two[1][0]:
            return 'consolidation'

    return 'trending'


def tradable(regime: str) -> bool:
    return regime == 'trending'


# ---------- 2. Top-down bias ----------

TF_LADDER = ['1W', '1D', '4H', '1H', '15M', '5M', '3M', '1M']


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """df must have a DatetimeIndex and columns open/high/low/close(/volume)."""
    cols = {c.lower(): c for c in df.columns}
    agg = {
        cols['open']: 'first', cols['high']: 'max',
        cols['low']: 'min', cols['close']: 'last',
    }
    if 'volume' in cols:
        agg[cols['volume']] = 'sum'
    return df.resample(rule).agg(agg).dropna()


RESAMPLE_MAP = {'1W': '1W', '1D': '1D', '4H': '4h', '1H': '1h', '15M': '15min', '5M': '5min', '3M': '3min', '1M': '1min'}


def per_tf_bias(df_by_tf: dict) -> dict:
    """
    df_by_tf: {'1D': df, '4H': df, ...} each with phase1 EMA bias already computed
    (expects an 'bias' column from ema_trend_filter, or computes it if missing).
    Returns {'1D': 'bullish', '4H': 'bearish', ...}
    """
    from phase1_primitives import ema_trend_filter
    result = {}
    for tf, df in df_by_tf.items():
        if 'bias' in df.columns:
            result[tf] = df['bias'].iloc[-1]
        else:
            ema = ema_trend_filter(df)
            result[tf] = ema['bias'].iloc[-1]
    return result


def resolve_topdown_bias(bias_by_tf: dict, tf_order: list = None) -> dict:
    """
    HTF wins conflicts. Alignment rule: want >=2 TFs aligned before trading.
    tf_order: highest-to-lowest, defaults to TF_LADDER filtered to keys present.
    Returns {'bias': overall bias, 'aligned_count': int, 'tradable': bool}
    """
    if tf_order is None:
        tf_order = [tf for tf in TF_LADDER if tf in bias_by_tf]

    biases = [bias_by_tf[tf] for tf in tf_order]
    htf_bias = biases[0] if biases else 'consolidation'

    aligned_count = sum(1 for b in biases if b == htf_bias and b != 'consolidation')

    return {
        'bias': htf_bias,
        'aligned_count': aligned_count,
        'tradable': aligned_count >= 2 and htf_bias != 'consolidation',
        'per_tf': dict(zip(tf_order, biases)),
    }


# ---------- 3. VSSR entry model ----------

VSSR_MAP = {'1D': '1H', '4H': '15M', '1H': '5M', '15M': '3M'}
OPPOSING_CHECK_MAP = {'4H': '1H', '1H': '15M', '1D': '4H'}


def vssr_entry_tf(poi_tf: str) -> str:
    if poi_tf not in VSSR_MAP:
        raise ValueError(f"No VSSR mapping for POI timeframe {poi_tf}")
    return VSSR_MAP[poi_tf]


def find_vssr_entry(entry_tf_df: pd.DataFrame, poi_direction: str, poi_tap_idx,
                     liquidity_target_price: float = None, liquidity_broken_check: pd.Series = None) -> dict:
    """
    After POI tap, look for a NEW imbalance forming in poi_direction on entry_tf_df
    (data after poi_tap_idx). Entry filter: skip if target liquidity already taken
    (checked via liquidity_broken_check, a boolean series aligned to entry_tf_df.index).

    Returns {'entry_found': bool, 'entry_idx': idx or None, 'imbalance_row': dict or None, 'skipped_reason': str or None}
    """
    idx_list = list(entry_tf_df.index)
    try:
        start_pos = idx_list.index(poi_tap_idx)
    except ValueError:
        start_pos = 0

    sub_df = entry_tf_df.iloc[start_pos:]
    imb = detect_imbalances(sub_df)
    if imb.empty:
        return {'entry_found': False, 'entry_idx': None, 'imbalance_row': None, 'skipped_reason': 'no_imbalance_formed'}

    matching = imb[imb['type'] == poi_direction]
    if matching.empty:
        return {'entry_found': False, 'entry_idx': None, 'imbalance_row': None, 'skipped_reason': 'no_matching_direction'}

    first = matching.iloc[0]

    # Entry filter: don't enter if liquidity already taken before this imbalance formed
    if liquidity_broken_check is not None:
        pre_entry = liquidity_broken_check.loc[:first['c3_idx']]
        if pre_entry.any():
            return {'entry_found': False, 'entry_idx': None, 'imbalance_row': None, 'skipped_reason': 'liquidity_already_taken'}

    return {'entry_found': True, 'entry_idx': first['c3_idx'], 'imbalance_row': first.to_dict(), 'skipped_reason': None}


def check_opposing_imbalance(poi_tf: str, one_tf_below_df: pd.DataFrame, poi_direction: str,
                              price_range: tuple) -> dict:
    """
    Check for an untested opposing imbalance between current price and POI, one TF below POI.
    price_range: (low, high) bracket to check within.
    Returns {'opposing_found': bool, 'zone': dict or None}
    """
    opposing_dir = 'bearish' if poi_direction == 'bullish' else 'bullish'
    imb = detect_imbalances(one_tf_below_df)
    if imb.empty:
        return {'opposing_found': False, 'zone': None}

    matching = imb[imb['type'] == opposing_dir]
    lo, hi = price_range
    within = matching[(matching['zone_low'] >= lo) & (matching['zone_high'] <= hi)]
    if within.empty:
        return {'opposing_found': False, 'zone': None}

    return {'opposing_found': True, 'zone': within.iloc[0].to_dict()}


# ---------- 4. Stop-loss / Take-profit ----------

def compute_stop_loss(entry_tf_df: pd.DataFrame, tap_start_idx, tap_end_idx, direction: str,
                       buffer_pct: float = 0.0005) -> float:
    """
    Stop at the absolute extreme of the entry-TF swing during the POI tap/VSSR window.
    direction: 'bullish' (long) -> stop below lowest low; 'bearish' (short) -> stop above highest high.
    """
    cols = {c.lower(): c for c in entry_tf_df.columns}
    window = entry_tf_df.loc[tap_start_idx:tap_end_idx]
    if direction == 'bullish':
        extreme = window[cols['low']].min()
        return extreme * (1 - buffer_pct)
    else:
        extreme = window[cols['high']].max()
        return extreme * (1 + buffer_pct)


def compute_take_profit(entry_price: float, stop_price: float, direction: str,
                         rr: float = 1.5, liquidity_target: float = None) -> dict:
    """
    Two methods: fixed RR (default 1.5) or nearest liquidity (low-hanging fruit).
    liquidity_target overrides fixed RR if provided.
    """
    risk = abs(entry_price - stop_price)
    if direction == 'bullish':
        fixed_tp = entry_price + risk * rr
    else:
        fixed_tp = entry_price - risk * rr

    target = liquidity_target if liquidity_target is not None else fixed_tp
    actual_rr = abs(target - entry_price) / risk if risk > 0 else 0

    return {'take_profit': target, 'fixed_rr_tp': fixed_tp, 'actual_rr': actual_rr, 'risk_amount': risk}


def position_size(account_balance: float, risk_pct: float, entry_price: float, stop_price: float) -> float:
    """size = (balance * risk%) / stop_distance"""
    risk_amount = account_balance * risk_pct
    stop_distance = abs(entry_price - stop_price)
    if stop_distance == 0:
        return 0.0
    return risk_amount / stop_distance


if __name__ == "__main__":
    np.random.seed(3)
    n = 300
    idx = pd.date_range('2025-01-01', periods=n, freq='1h')
    close = 100 + np.cumsum(np.random.randn(n) * 0.8)
    open_ = close + np.random.randn(n) * 0.3
    high = np.maximum(open_, close) + np.abs(np.random.randn(n) * 0.4)
    low = np.minimum(open_, close) - np.abs(np.random.randn(n) * 0.4)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)

    imb = detect_imbalances(df)
    regime = detect_regime(df, imb)
    print(f"Regime: {regime}, tradable: {tradable(regime)}")

    # top-down bias mock
    bias_by_tf = {'1D': 'bullish', '4H': 'bullish', '1H': 'bearish'}
    topdown = resolve_topdown_bias(bias_by_tf)
    print(f"Top-down bias: {topdown}")

    # VSSR entry test
    if not imb.empty:
        poi = imb.iloc[0]
        entry_tf = vssr_entry_tf('1H') if '1H' in VSSR_MAP else '15M'
        vssr = find_vssr_entry(df, poi['type'], poi['c3_idx'])
        print(f"VSSR entry search: {vssr['entry_found']}, reason: {vssr['skipped_reason']}")

        sl = compute_stop_loss(df, poi['c1_idx'], poi['c3_idx'], poi['type'])
        entry_price = df['close'].loc[poi['c3_idx']]
        tp = compute_take_profit(entry_price, sl, poi['type'])
        size = position_size(10000, 0.01, entry_price, sl)
        print(f"Entry: {entry_price:.2f}, SL: {sl:.2f}, TP: {tp['take_profit']:.2f}, size: {size:.4f}")
