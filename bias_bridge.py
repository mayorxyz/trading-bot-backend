from phase1_primitives import ema_trend_filter
from phase2_signal_engine import detect_imbalances
from phase3_orchestration import per_tf_bias, resolve_topdown_bias, detect_regime, tradable


def resolve_bias(df_by_tf: dict) -> dict:
    for tf, df in df_by_tf.items():
        if 'bias' not in df.columns:
            df_by_tf[tf] = df.join(ema_trend_filter(df)[['bias']])

    bias_by_tf = per_tf_bias(df_by_tf)
    topdown = resolve_topdown_bias(bias_by_tf)

    if not topdown['tradable']:
        return {"direction": None, "trend_aligned": False, "mtf_full_alignment": False,
                "regime": None, "tradable": False, "topdown": topdown}

    lowest_tf_df = list(df_by_tf.values())[-1]
    imb = detect_imbalances(lowest_tf_df)
    regime = detect_regime(lowest_tf_df, imb)

    direction = "LONG" if topdown['bias'] == 'bullish' else "SHORT"
    mtf_full_alignment = topdown['aligned_count'] == len(df_by_tf)

    return {
        "direction": direction,
        "trend_aligned": topdown['aligned_count'] >= 2,
        "mtf_full_alignment": mtf_full_alignment,
        "regime": regime,
        "tradable": tradable(regime) and topdown['tradable'],
        "topdown": topdown,
    }