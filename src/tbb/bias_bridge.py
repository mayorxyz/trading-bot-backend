"""
bias_bridge.py â€” adapts Phase 1-3 (+5) outputs into the pipeline's
tradable-direction/bias result.

Build #8: every heavy component is memoized per immutable-frame fingerprint
(last bar + row count, via memokv). In a backtest the 1D/4H slices only
advance every few execution steps, so their components are computed once and
reused â€” while remaining byte-identical to uncached runs, because each cached
value is a pure function of the frame it was keyed on.

Cached values are shared and therefore READ-ONLY. Callers must not mutate
returned dicts/DataFrames.
"""

from tbb import memokv
from tbb.analysis.primitives import ema_trend_filter
from tbb.analysis.imbalances import detect_imbalances
from tbb.analysis.regime import per_tf_bias, resolve_topdown_bias, detect_regime, tradable
from tbb.analysis.structure_liquidity import classify_structure, classify_liquidity

_MEMO_V = 8  # bump if any component's semantics change


def _cached_ema_bias(df):
    """Full ema_trend_filter output for this exact frame (read-only)."""
    key = ("ema", _MEMO_V, memokv.frame_key(df))
    out = memokv.get(key)
    if out is None:
        out = ema_trend_filter(df)
        memokv.put(key, out)
    return out


def _cached_imbalances(df):
    key = ("imb", _MEMO_V, memokv.frame_key(df))
    out = memokv.get(key)
    if out is None:
        out = detect_imbalances(df)
        memokv.put(key, out)
    return out


def _cached_regime(df, imb):
    key = ("regime", _MEMO_V, memokv.frame_key(df), len(imb) if imb is not None else 0)
    out = memokv.get(key)
    if out is None:
        out = detect_regime(df, imb)
        memokv.put(key, out)
    return out


def _cached_structure(df):
    key = ("struct", _MEMO_V, memokv.frame_key(df))
    out = memokv.get(key)
    if out is None:
        out = classify_structure(df)
        memokv.put(key, out)
    return out


def _cached_liquidity(df, current_price):
    key = ("liq", _MEMO_V, memokv.frame_key(df), round(float(current_price), 10))
    out = memokv.get(key)
    if out is None:
        out = classify_liquidity(df, current_price=current_price)
        memokv.put(key, out)
    return out


def resolve_bias(df_by_tf: dict) -> dict:
    for tf, df in df_by_tf.items():
        if 'bias' not in df.columns:
            df_by_tf[tf] = df.join(_cached_ema_bias(df)[['bias']])

    bias_by_tf = per_tf_bias(df_by_tf)
    topdown = resolve_topdown_bias(bias_by_tf)

    if not topdown['tradable']:
        return {"direction": None, "trend_aligned": False, "mtf_full_alignment": False,
                "regime": None, "tradable": False, "topdown": topdown,
                "structure": None, "liquidity": None}

    lowest_tf_df = list(df_by_tf.values())[-1]
    imb = _cached_imbalances(lowest_tf_df)
    regime = _cached_regime(lowest_tf_df, imb)

    direction = "LONG" if topdown['bias'] == 'bullish' else "SHORT"
    mtf_full_alignment = topdown['aligned_count'] == len(df_by_tf)

    # Structure/liquidity on execution timeframe
    structure = _cached_structure(lowest_tf_df)
    current_price = lowest_tf_df['close'].iloc[-1] if 'close' in lowest_tf_df.columns else lowest_tf_df['Close'].iloc[-1]
    liquidity = _cached_liquidity(lowest_tf_df, current_price)

    return {
        "direction": direction,
        "trend_aligned": topdown['aligned_count'] >= 2,
        "mtf_full_alignment": mtf_full_alignment,
        "regime": regime,
        "tradable": tradable(regime) and topdown['tradable'],
        "topdown": topdown,
        "structure": structure,
        "liquidity": liquidity,
    }
