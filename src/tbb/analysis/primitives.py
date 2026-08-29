"""
Phase 1 â€” Core primitives for structural trading system.
1. Candle OHLC/body/wick primitives
2. Fractal swing high/low detection (3-bar & 5-bar)
3. 50 EMA trend filter with slope + break detection

Input: pd.DataFrame with columns open, high, low, close (case-insensitive).
"""

import pandas as pd
import numpy as np


# ---------- 1. Candle primitives ----------

def candle_primitives(df: pd.DataFrame) -> pd.DataFrame:
    cols = {c.lower(): c for c in df.columns}
    o, h, l, c = df[cols['open']], df[cols['high']], df[cols['low']], df[cols['close']]

    out = pd.DataFrame(index=df.index)
    out['body'] = (c - o).abs()
    out['upper_wick'] = h - pd.concat([o, c], axis=1).max(axis=1)
    out['lower_wick'] = pd.concat([o, c], axis=1).min(axis=1) - l
    out['total_range'] = h - l
    out['is_bullish'] = c > o
    out['is_bearish'] = c < o
    combined_wick = out['upper_wick'] + out['lower_wick']
    out['is_strong_body'] = out['body'] > combined_wick  # momentum candle
    out['body_ratio'] = out['body'] / out['total_range'].replace(0, np.nan)
    out['is_doji'] = out['body_ratio'] <= 0.1
    return out


# ---------- 2. Fractal swing detection ----------

def fractal_swings(df: pd.DataFrame, side_bars: int = 2) -> pd.DataFrame:
    """
    side_bars=1 -> 3-bar fractal, side_bars=2 -> 5-bar fractal (default).
    Confirmed only after side_bars complete on the right -> result lags by side_bars.
    Returns DataFrame with swing_high, swing_low boolean columns aligned to df.index.
    """
    cols = {c.lower(): c for c in df.columns}
    h, l = df[cols['high']].values, df[cols['low']].values
    n = len(df)
    swing_high = np.zeros(n, dtype=bool)
    swing_low = np.zeros(n, dtype=bool)

    for i in range(side_bars, n - side_bars):
        window_h = h[i - side_bars:i + side_bars + 1]
        window_l = l[i - side_bars:i + side_bars + 1]
        if h[i] == window_h.max() and np.sum(window_h == h[i]) == 1:
            swing_high[i] = True
        if l[i] == window_l.min() and np.sum(window_l == l[i]) == 1:
            swing_low[i] = True

    return pd.DataFrame({'swing_high': swing_high, 'swing_low': swing_low}, index=df.index)


def get_swing_points(df: pd.DataFrame, side_bars: int = 2) -> pd.DataFrame:
    """Compact list of confirmed swing points: index, price, type."""
    sw = fractal_swings(df, side_bars)
    cols = {c.lower(): c for c in df.columns}
    h, l = df[cols['high']], df[cols['low']]

    points = []
    for idx in df.index[sw['swing_high']]:
        points.append({'index': idx, 'price': h.loc[idx], 'type': 'high'})
    for idx in df.index[sw['swing_low']]:
        points.append({'index': idx, 'price': l.loc[idx], 'type': 'low'})
    if not points:
        return pd.DataFrame(columns=['index', 'price', 'type'])
    return pd.DataFrame(points).sort_values('index').reset_index(drop=True)


# ---------- 3. 50 EMA trend filter ----------

def ema_trend_filter(df: pd.DataFrame, length: int = 50, slope_lookback: int = 3,
                      flat_epsilon: float = None) -> pd.DataFrame:
    """
    Returns DataFrame: ema, slope ('up'/'down'/'flat'), bias ('bullish'/'bearish'/'consolidation'),
    trend_change (bool â€” 2 consecutive closes flipping side of EMA).
    """
    cols = {c.lower(): c for c in df.columns}
    c = df[cols['close']]
    ema = c.ewm(span=length, adjust=False).mean()

    if flat_epsilon is None:
        flat_epsilon = ema.std() * 0.01  # adaptive default

    ema_shift = ema.shift(slope_lookback)
    diff = ema - ema_shift
    slope = pd.Series(np.where(diff > flat_epsilon, 'up',
                       np.where(diff < -flat_epsilon, 'down', 'flat')), index=df.index)

    above = c > ema
    bias = pd.Series('consolidation', index=df.index)
    bias[(above) & (slope == 'up')] = 'bullish'
    bias[(~above) & (slope == 'down')] = 'bearish'

    # trend change: 2 consecutive candles closing on the other side of EMA vs prior bias
    side = np.where(above, 1, -1)
    side_series = pd.Series(side, index=df.index)
    flipped_2bar = (side_series == side_series.shift(1)) & (side_series != side_series.shift(2))
    trend_change = flipped_2bar.fillna(False)

    return pd.DataFrame({
        'ema': ema,
        'slope': slope,
        'bias': bias,
        'above_ema': above,
        'trend_change': trend_change,
    }, index=df.index)


# ---------- Combined convenience ----------

def build_phase1_features(df: pd.DataFrame, side_bars: int = 2, ema_length: int = 50) -> pd.DataFrame:
    """Merge candle primitives + fractal swings + EMA bias into one feature frame."""
    prim = candle_primitives(df)
    sw = fractal_swings(df, side_bars)
    ema = ema_trend_filter(df, ema_length)
    return pd.concat([df, prim, sw, ema], axis=1)


if __name__ == "__main__":
    np.random.seed(1)
    n = 300
    close = 100 + np.cumsum(np.random.randn(n) * 0.8)
    open_ = close + np.random.randn(n) * 0.3
    high = np.maximum(open_, close) + np.abs(np.random.randn(n) * 0.4)
    low = np.minimum(open_, close) - np.abs(np.random.randn(n) * 0.4)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})

    feats = build_phase1_features(df)
    print(feats.tail(10)[['close', 'is_bullish', 'body_ratio', 'swing_high', 'swing_low', 'ema', 'slope', 'bias', 'trend_change']])
    print(f"\nTotal swing highs: {feats['swing_high'].sum()}, swing lows: {feats['swing_low'].sum()}")
    print(f"Trend changes flagged: {feats['trend_change'].sum()}")
