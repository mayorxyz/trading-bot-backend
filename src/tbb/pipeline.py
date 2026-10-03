"""
pipeline.py Ã¢â‚¬â€ wires every module together in the correct order.
Drop this in the same folder as the other files.

analyze_pair() Ã¢â‚¬â€ core structure/entry/SL-TP/confluence/log flow.
Signals fire on weighted evidence, not on every indicator being present: the
only hard requirements are a tradable bias, a qualifying entry, and valid
SL/TP. Everything else (fib zone, chart pattern, retracement, wicks, volume,
structure) contributes weight to a 0-100 confluence score, and a conviction
gate skips the setup when that score lands below min_confluence_score
(default 50 = at least MEDIUM; pass 0 to disable).
analyze_pair_with_bias() Ã¢â‚¬â€ wraps it with phase1-6 top-down bias via bias_bridge.py,
plus execution-timeframe chart-pattern analysis (chart_patterns.py through
pattern_strategy.py) and fib-retracement pullback analysis (retracement.py on
top of fibonacci.py). A momentum-confirmed pattern break adds
chart_pattern_align points to the confluence score; a confirmed fib-zone
pullback entry adds retracement_confirm points and lights the previously
placeholder fib_confluence input. Every result carries "chart_pattern" and
"retracement" summary blocks regardless of outcome.

Both take db_path, which is where a fired signal is logged. Pass db_path=None to
compute a signal without logging it Ã¢â‚¬â€ used by the stateless POST /predict.
"""

import os
import threading
from collections import OrderedDict

import talib
import numpy as np
from datetime import datetime

from tbb import config as paths
from tbb.indicators.zigzag import get_zigzag_swings
from tbb.indicators.support_resistance import find_sr_levels
from tbb.engines.consolidation import find_consolidation_zones
from tbb.engines.breakouts import detect_sr_breakout, detect_consolidation_breakout
from tbb.indicators.wicks import detect_wicks, wick_at_level
from tbb.engines.entry import find_best_entry
from tbb.engines.sl_tp import get_trade_levels
from tbb.engines.validity import validate_trade
from tbb.engines.confluence import (calculate_confluence, confidence_label,
                        WEIGHTS, SCALED_KEYS)
from tbb.indicators.volume import detect_volume_spike, detect_volume_divergence
from tbb.storage.signal_store import init_db, log_signal
from tbb.bias_bridge import resolve_bias
from tbb.indicators.pattern_detector import get_active_patterns, summarize_bias
from tbb.analysis.supplementary import detect_mss, is_news_blackout, session_quality
from tbb.engines.pattern_strategy import analyze_pattern_setup
from tbb.engines.retracement import analyze_retracement, compute_fib_score
from tbb.indicators.fibonacci import analyze_fibonacci
from tbb.engines.breakout_engine import analyze_breakout
from tbb.engines.elliott_wave import analyze_elliott_wave
from tbb.engines.structure_retest import analyze_structure_retest
from tbb.engines import entry_generators as entry_gen


# ---------------------------------------------------------------------------
# Window memoization (Build #6): bars are immutable once closed, so every
# artifact computed from a fixed window is a pure function of that window.
# Keys fingerprint frames by (last-bar ISO, row count) plus edge prices; the
# same bar therefore pays its compute once per process, and repeat /predict
# calls within a bar are cache hits. Backtests slide to a new last bar each
# step, so they naturally miss Ã¢â‚¬â€ they benefit only from the gates-first
# reorder below.
#
# Correctness contract: cached values are READ-ONLY. Nothing downstream may
# mutate a returned dict/DataFrame in place. If you change what an engine
# computes, also bump PIPELINE_MEMO_VERSION so stale processes restart clean.
# ---------------------------------------------------------------------------
PIPELINE_MEMO_VERSION = 1
_MEMO_MAX = int(os.environ.get("PIPELINE_MEMO_ENTRIES", 512))
_memo = OrderedDict()
_memo_lock = threading.Lock()


def _memo_get(key):
    with _memo_lock:
        if key in _memo:
            _memo.move_to_end(key)
            return _memo[key]
    return None


def _memo_put(key, value):
    with _memo_lock:
        _memo[key] = value
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)


def _frame_key(df):
    """Fingerprint one OHLCV frame: immutable once its last bar closes."""
    if df is None or not len(df):
        return ("empty", 0)
    return (str(df.index[-1]), int(len(df)))


def _arrays_key(opens, highs, lows, closes):
    """Fingerprint an execution window without hashing full arrays."""
    return (int(len(closes)),
            round(float(closes[0]), 10), round(float(closes[-1]), 10),
            round(float(highs.max()), 10), round(float(lows.min()), 10))


def _frames_bias_key(df_by_tf):
    return tuple(sorted((tf,) + _frame_key(df)
                        for tf, df in df_by_tf.items()))


# --- Build #9: bound-pruned engine evaluation -------------------------------
# Confluence is a weighted sum with known per-input maximums, so after each
# engine we can compute an EXACT upper bound on the final score:
#     earned(actual, evaluated inputs) + WEIGHTS(unevaluated inputs)
# If that bound is below the conviction threshold, the verdict is already
# decided no matter what the remaining engines would find Ã¢â‚¬â€ so they are
# skipped (labelled "pruned_verdict_decided") instead of computed. Fired
# signals are never pruned: a bound < threshold can never produce a fire.

def _prune_enabled() -> bool:
    return os.environ.get("PIPELINE_ENGINE_PRUNE", "1") != "0"


def _earned_points(key, val):
    """Mirror of confluence.calculate_confluence's per-input arithmetic."""
    w = WEIGHTS[key]
    if key == "sr_level_strength":
        return min(val, 6) / 6 * w if val else 0.0
    if key in SCALED_KEYS:
        try:
            frac = max(0.0, min(float(val), 100.0)) / 100.0
        except (TypeError, ValueError):
            frac = 0.0
        return w * frac
    return w if val else 0.0


def _summarize_chart_pattern(setup):
    """JSON-safe compact view of a pattern_strategy.analyze_pattern_setup result."""
    chosen = setup.get("chosen") or {}
    plan = setup.get("plan") or {}
    return {
        "trade_ready": setup.get("trade_ready", False),
        "aligned": setup.get("aligned", False),
        "stage": setup.get("stage"),
        "pattern": chosen.get("pattern"),
        "pattern_direction": chosen.get("direction"),
        "status": chosen.get("status"),
        "trigger_level": chosen.get("trigger_level"),
        "invalidation": chosen.get("invalidation"),
        "measured_target": chosen.get("measured_target"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "tp1": plan.get("tp1"),
        "final_target": plan.get("final_target"),
        "skip_reason": setup.get("skip_reason"),
        "patterns_found": [p.get("pattern") for p in setup.get("all_patterns", [])],
    }


def _summarize_retracement(setup):
    """JSON-safe compact view of a retracement.analyze_retracement result."""
    plan = setup.get("plan") or {}
    filters = setup.get("filters") or {}
    return {
        "trade_ready": setup.get("trade_ready", False),
        "aligned": setup.get("aligned", False),
        "stage": setup.get("stage"),
        "direction": setup.get("direction"),
        "fib_confluence": setup.get("fib_confluence", False),
        "trend": filters.get("trend"),
        "pullback_depth_pct": filters.get("pullback_depth_pct"),
        "zone_hits": filters.get("zone_hits", []),
        "recommended_zone": filters.get("recommended_zone"),
        "invalidation": filters.get("invalidation"),
        "exhausted_leg": (filters.get("exhaustion") or {}).get("stretched"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "tp1": plan.get("tp1"),
        "final_target": plan.get("final_target"),
        "skip_reason": setup.get("skip_reason"),
    }


def _compute_engine_bundle(opens, highs, lows, closes, volumes,
                           direction, sr_events, exec_df,
                           base_signals=None, threshold=50):
    """
    Every confluence-only computation that depends on the execution window
    (+ direction / BOS events). Memoized per closed bar by the caller.

    Build #9: engines run in information-value order and are subject to
    BOUND PRUNING Ã¢â‚¬â€ after each engine, if
        earned(base + evaluated) + max_remaining_possible < threshold,
    the conviction verdict is already 'reject' no matter what the rest find,
    so the remaining engines are skipped and labelled
    stage="pruned_verdict_decided". Fired signals are mathematically never
    pruned. Disable with PIPELINE_ENGINE_PRUNE=0 (e.g. for A/B verification).

    base_signals: the eight pre-engine confluence inputs (bias/probe facts).
    Returned values are READ-ONLY (shared from the pipeline memo).
    """
    base_signals = base_signals or {}
    engine_order = ("session_timing", "pattern_match", "structure_retest_confirm",
                    "breakout_score", "chart_pattern_align", "fib_score",
                    "elliott_score", "retracement_confirm")
    evaluated = {}

    def _bound():
        total = sum(_earned_points(k, v) for k, v in base_signals.items())
        total += sum(_earned_points(k, v) for k, v in evaluated.items())
        total += sum(WEIGHTS[k] for k in engine_order if k not in evaluated)
        return total

    active = {k: v for k, v in base_signals.items()}
    pruned = []

    def _exhausted():
        # record every not-yet-evaluated engine key as pruned
        for k in engine_order:
            if k not in evaluated and k not in pruned:
                pruned.append(k)

    # --- 1. session quality (weight 5, effectively free) --------------------
    session_info = {"score": 0, "session": None}
    try:
        if len(exec_df):
            session_info = session_quality(exec_df.index[-1])
    except Exception as e:
        session_info = {"score": 0, "session": None,
                        "error": f"{type(e).__name__}: {e}"}
    evaluated["session_timing"] = session_info.get("score", 0)
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(session=session_info, pruned=pruned)

    # --- 2. TA-Lib candlestick patterns (weight 9) --------------------------
    active_patterns = get_active_patterns(exec_df)
    pattern_bias = summarize_bias(active_patterns)
    agreeing = (pattern_bias["bullish_patterns"] if direction == "LONG"
                else pattern_bias["bearish_patterns"])
    pattern_name = agreeing[0] if agreeing else None
    evaluated["pattern_match"] = bool(pattern_name)
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(session=session_info, pruned=pruned,
                              pattern_name=None)

    # --- 3. structure retest (weight 6) -------------------------------------
    structure_retest_info = {"trade_ready": False, "aligned": False}
    try:
        setup = analyze_structure_retest(
            sr_events, opens, highs, lows, closes,
            trade_direction=direction,
        )
        structure_retest_info = {
            "trade_ready": bool(setup.get("trade_ready")),
            "aligned": bool(setup.get("aligned")),
            "stage": setup.get("stage"),
            "direction": setup.get("direction"),
            "level": setup.get("level"),
            "entry": (setup.get("plan") or {}).get("entry"),
            "stop": (setup.get("plan") or {}).get("stop"),
            "tp1": (setup.get("plan") or {}).get("tp1"),
            "skip_reason": setup.get("skip_reason"),
        }
    except Exception as e:
        structure_retest_info = {"trade_ready": False, "aligned": False,
                                 "stage": "error",
                                 "error": f"{type(e).__name__}: {e}"}
    evaluated["structure_retest_confirm"] = bool(structure_retest_info.get("trade_ready"))
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(structure_retest=structure_retest_info,
                              session=session_info, pruned=pruned,
                              pattern_name=pattern_name)

    # --- 4. breakout engine (weight 6) --------------------------------------
    try:
        brk = analyze_breakout(opens, highs, lows, closes, volumes=volumes)
        breakout_info = {
            "score": int(brk.get("score") or 0),
            "trade_ready": bool(brk.get("trade_ready")),
            "direction": (brk.get("breakout") or {}).get("direction"),
            "shape": ((brk.get("key_levels") or {}).get("shape")),
            "momentum_confirmed": bool((brk.get("momentum") or {}).get("is_momentum")),
            "volume_confirmed": bool((brk.get("volume") or {}).get("confirmed")),
            "entry": (brk.get("entry_stop") or {}).get("entry"),
            "stop": (brk.get("entry_stop") or {}).get("stop"),
            "tp1": (brk.get("take_profit_plan") or {}).get("tp1"),
            "chandelier_stop": brk.get("chandelier_stop"),
        }
    except Exception as e:
        breakout_info = {"score": 0, "trade_ready": False, "stage": "error",
                         "error": f"{type(e).__name__}: {e}"}
    evaluated["breakout_score"] = breakout_info.get("score", 0)
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(breakout=breakout_info,
                              structure_retest=structure_retest_info,
                              session=session_info, pruned=pruned,
                              pattern_name=pattern_name)

    # --- 5. chart patterns (weight 6) ---------------------------------------
    try:
        pattern_setup = analyze_pattern_setup(
            opens, highs, lows, closes, volumes,
            trade_direction=direction,
        )
        chart_pattern_info = _summarize_chart_pattern(pattern_setup)
    except Exception as e:
        chart_pattern_info = {"trade_ready": False, "aligned": False,
                              "stage": "error", "error": f"{type(e).__name__}: {e}"}
    evaluated["chart_pattern_align"] = bool(chart_pattern_info.get("trade_ready"))
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(chart_pattern=chart_pattern_info,
                              breakout=breakout_info,
                              structure_retest=structure_retest_info,
                              session=session_info, pruned=pruned,
                              pattern_name=pattern_name)

    # --- 6. fibonacci (weight 5; shared into retracement) -------------------
    fib_info = {"score": 0, "trade_ready": False}
    fib_raw = None
    try:
        fib_raw = analyze_fibonacci(opens, highs, lows, closes, volumes=volumes)
        zone_hits = [n for n, v in (fib_raw.get("zone_hits") or {}).items() if v]
        fib_info = {
            "score": compute_fib_score(fib_raw),
            "trade_ready": bool(fib_raw.get("trade_ready")),
            "in_any_zone": len(zone_hits) > 0,
            "zone_hits": zone_hits,
            "invalidation": (fib_raw.get("invalidation") or {}).get("status"),
        }
    except Exception as e:
        fib_raw = None
        fib_info = {"score": 0, "trade_ready": False, "stage": "error",
                    "error": f"{type(e).__name__}: {e}"}
    evaluated["fib_score"] = fib_info.get("score", 0)
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(chart_pattern=chart_pattern_info,
                              breakout=breakout_info, fib=fib_info,
                              structure_retest=structure_retest_info,
                              session=session_info, pruned=pruned,
                              pattern_name=pattern_name)

    # --- 7. elliott wave (weight 5, most expensive -> last big block) -------
    try:
        ew = analyze_elliott_wave(opens, highs, lows, closes, volumes=volumes)
        pos = ew.get("position") or {}
        elliott_info = {
            "score": int(ew.get("elliott_score") or 0),
            "trade_ready": bool(ew.get("trade_ready")),
            "count_valid": bool(ew.get("count_valid")),
            "candidate_found": bool(ew.get("candidate_found")),
            "direction": ew.get("direction"),
            "position_stage": pos.get("stage"),
            "position_direction": pos.get("direction"),
            "trigger_level": pos.get("trigger_level"),
            "invalidation": pos.get("invalidation"),
            "stale": bool(pos.get("stale")),
            "truncated": bool((ew.get("truncation") or {}).get("truncated")),
            "divergence": bool(((ew.get("wave5_divergence") or {}).get("divergence"))),
        }
    except Exception as e:
        elliott_info = {"score": 0, "trade_ready": False, "stage": "error",
                        "error": f"{type(e).__name__}: {e}"}
    evaluated["elliott_score"] = elliott_info.get("score", 0)
    if _prune_enabled() and _bound() < threshold:
        _exhausted()
        return _bundle_result(chart_pattern=chart_pattern_info,
                              breakout=breakout_info, fib=fib_info,
                              elliott=elliott_info,
                              structure_retest=structure_retest_info,
                              session=session_info, pruned=pruned,
                              pattern_name=pattern_name)

    # --- 8. retracement (weight 4; consumes fib_raw) ------------------------
    try:
        retr_setup = analyze_retracement(
            opens, highs, lows, closes, volumes,
            trade_direction=direction,
            fib_precomputed=fib_raw,
        )
        retracement_info = _summarize_retracement(retr_setup)
    except Exception as e:
        retracement_info = {"trade_ready": False, "aligned": False,
                            "stage": "error", "error": f"{type(e).__name__}: {e}"}
    evaluated["retracement_confirm"] = bool(retracement_info.get("trade_ready"))

    return {
        "pattern_name": pattern_name,
        "chart_pattern": chart_pattern_info,
        "fib": fib_info,
        "breakout": breakout_info,
        "elliott": elliott_info,
        "structure_retest": structure_retest_info,
        "session": session_info,
        "retracement": retracement_info,
        "pruned": pruned,
    }


_PRUNED_STAGE = "pruned_verdict_decided"


def _pruned_block(extra=None):
    blk = {"trade_ready": False, "aligned": False, "stage": _PRUNED_STAGE}
    if extra:
        blk.update(extra)
    return blk


def _bundle_result(chart_pattern=None, breakout=None, fib=None, elliott=None,
                   structure_retest=None, session=None, retracement=None,
                   pruned=None, pattern_name=None):
    """Bundle shape with pruned blocks filled as labelled stubs."""
    return {
        "pattern_name": pattern_name,
        "chart_pattern": chart_pattern or _pruned_block(),
        "fib": fib or _pruned_block({"score": 0}),
        "breakout": breakout or _pruned_block({"score": 0}),
        "elliott": elliott or _pruned_block({"score": 0}),
        "structure_retest": structure_retest or _pruned_block(),
        "session": session or {"score": 0, "session": None},
        "retracement": retracement or _pruned_block(),
        "pruned": pruned or [],
    }


def analyze_pair_with_bias(pair, timeframe, df_by_tf,
                            opens, highs, lows, closes, volumes,
                            db_path=paths.SIGNALS_DB, news_calendar=None,
                            min_confluence_score=50):
    """
    Top-down bias resolution wrapper. 
    Pass news_calendar (list of dicts with 'time' and 'impact') to enforce hard news blackouts.
    """
    # Bias over the HTF frames is the single most expensive step on large
    # history Ã¢â‚¬â€ and, like everything here, a pure function of closed bars.
    bias_key = ("bias", PIPELINE_MEMO_VERSION, _frames_bias_key(df_by_tf))
    bias = _memo_get(bias_key)
    if bias is None:
        bias = resolve_bias(df_by_tf)
        _memo_put(bias_key, bias)

    if not bias["tradable"] or bias["direction"] is None:
        return {"skipped": f"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"}

    # ---------------------------------------------------------------------
    # NEW: Market Structure Shift (MSS) Conflict Check
    # ---------------------------------------------------------------------
    no_mss_conflict = True
    if bias.get("structure") and bias["structure"].get("events"):
        mss_events = detect_mss(bias["structure"]["events"], bias["structure"].get("trend", ""))
        if mss_events:
            last_mss = mss_events[-1]["event"]
            conflicting = (
                (bias["direction"] == "LONG" and last_mss == "MSS_bear") or
                (bias["direction"] == "SHORT" and last_mss == "MSS_bull")
            )
            no_mss_conflict = not conflicting

    # ---------------------------------------------------------------------
    # MSS as a WEIGHTED input, not a veto (Build #7). A conflicting shift
    # costs the no_mss_conflict points (4/100) inside calculate_confluence,
    # so a strong setup survives discounted while a weak one fails the
    # conviction gate anyway. detect_mss itself is bounded to the post-BOS
    # segment, so this reflects the CURRENT shift only.
    # ---------------------------------------------------------------------

    # ---------------------------------------------------------------------
    # NEW: News Blackout Hard Gate
    # ---------------------------------------------------------------------
    if news_calendar is not None:
        blackout = is_news_blackout(news_calendar, datetime.utcnow())
        if blackout["blackout"]:
            return {"skipped": f"news blackout: {blackout['events_today']}"}

    # ---------------------------------------------------------------------
    # Existing Bias & Pattern Alignment Logic
    # ---------------------------------------------------------------------
    # (The TA-Lib pattern scan moved into _compute_engine_bundle Ã¢â‚¬â€ it is
    # memoized and deferred until a candidate survives the cheap probe.)

    # Structure/liquidity confluence inputs
    structure_bos_align = False
    if bias.get("structure") and bias["structure"].get("events"):
        recent_bos = [e for e in bias["structure"]["events"][-5:] if "BOS" in e["event"]]
        if recent_bos:
            last_bos = recent_bos[-1]["event"]
            structure_bos_align = (
                (bias["direction"] == "LONG" and last_bos == "BOS_bull") or
                (bias["direction"] == "SHORT" and last_bos == "BOS_bear")
            )

    liquidity_target = False
    if bias.get("liquidity") is not None and not bias["liquidity"].empty:
        current_price = closes[-1]
        pool = bias["liquidity"][bias["liquidity"]["liquidity_type"].isin(["major", "low_hanging"])]
        if not pool.empty:
            if bias["direction"] == "LONG":
                targets = pool[pool["price"] > current_price]
            else:
                targets = pool[pool["price"] < current_price]
            liquidity_target = not targets.empty

    # ---------------------------------------------------------------------
    # Build #6 ordering: cheap gates BEFORE the expensive engine stack.
    # Most windows die at entry / SL-TP / validity (see any skip funnel);
    # those never pay for Elliott/patterns/fib again. The probe result is
    # memoized per execution window Ã¢â‚¬â€ bars are immutable once closed.
    # ---------------------------------------------------------------------
    exec_df = list(df_by_tf.values())[-1]
    prep_key = ("prep", PIPELINE_MEMO_VERSION,
                _arrays_key(opens, highs, lows, closes))
    prepared = _memo_get(prep_key)
    if prepared is None:
        prepared = _prepare_candidate(
            opens, highs, lows, closes, volumes, bias["direction"],
            df_by_tf=df_by_tf)
        _memo_put(prep_key, prepared)
    if "skip" in prepared:
        return {"skipped": prepared["skip"]}

    # Engine stack + TA-Lib pattern scan, memoized for the life of the bar:
    # repeat predictions within one candle are pure cache hits. The eight
    # pre-engine confluence facts ride along so Build #9 bound-pruning knows
    # exactly what is already earned before the first engine runs.
    sr_events = (bias.get("structure") or {}).get("events") or []
    base_signals = {
        "trend_align": bool(bias["trend_aligned"]),
        "mtf_alignment": bool(bias["mtf_full_alignment"]),
        "sr_level_strength": prepared["level_touches"],
        "wick_rejection": bool(prepared["wick_at_entry"]),
        "volume_confirm": bool(prepared["volume_confirm"]),
        "structure_bos_align": bool(structure_bos_align),
        "liquidity_target": bool(liquidity_target),
        "no_mss_conflict": bool(no_mss_conflict),
    }
    eng_key = ("engines", PIPELINE_MEMO_VERSION,
               _frames_bias_key(df_by_tf),
               _arrays_key(opens, highs, lows, closes),
               bias["direction"], len(sr_events),
               tuple(sorted(base_signals.items())), int(min_confluence_score))
    bundle = _memo_get(eng_key)
    if bundle is None:
        bundle = _compute_engine_bundle(
            opens, highs, lows, closes, volumes,
            direction=bias["direction"], sr_events=sr_events,
            exec_df=exec_df, base_signals=base_signals,
            threshold=min_confluence_score)
        _memo_put(eng_key, bundle)

    return analyze_pair(
        pair=pair, timeframe=timeframe,
        opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes,
        direction=bias["direction"], pattern_name=bundle["pattern_name"],
        trend_aligned=bias["trend_aligned"], mtf_full_alignment=bias["mtf_full_alignment"],
        structure_bos_align=structure_bos_align, liquidity_target=liquidity_target,
        no_mss_conflict=no_mss_conflict,
        db_path=db_path,
        chart_pattern_info=bundle["chart_pattern"],
        retracement_info=bundle["retracement"],
        fib_info=bundle["fib"],
        breakout_info=bundle["breakout"],
        elliott_info=bundle["elliott"],
        structure_retest_info=bundle["structure_retest"],
        session_info=bundle["session"],
        min_confluence_score=min_confluence_score,
        prepared=prepared,
    )


def _prepare_candidate(opens, highs, lows, closes, volumes, direction,
                       df_by_tf=None):
    """
    Cheap structural path shared by analyze_pair: swings -> S/R -> entry ->
    SL/TP -> validity. Returns a context dict on success or {"skip": reason}
    carrying the exact same strings analyze_pair has always returned.

    Split out so the bias wrapper can reject dead windows BEFORE paying for
    the engine stack, and so the result can be memoized per window.

    df_by_tf is optional and read-only: the HTF frames the caller already holds,
    forwarded unchanged to the entry-generator context. Callers that do not have
    it (analyze_pair's legacy path, backtest helpers) simply omit it — nothing
    that reads the returned ctx sees a change.
    """
    swings = get_zigzag_swings(highs, lows, closes)
    sr_levels = find_sr_levels(swings)
    zones = find_consolidation_zones(swings)
    sr_breaks = detect_sr_breakout(closes, highs, lows, sr_levels)
    cons_breaks = detect_consolidation_breakout(closes, zones)
    wick_events = detect_wicks(opens, highs, lows, closes)
    wick_hits = wick_at_level(wick_events, sr_levels, highs, lows)
    vol_spikes = detect_volume_spike(volumes)
    vol_div = detect_volume_divergence(closes, volumes)

    current_price = closes[-1]
    atr = float(talib.ATR(np.array(highs), np.array(lows), np.array(closes), timeperiod=14)[-1])

    # Entry selection. The flag is read HERE, at call time (never at import).
    # Only a NON-default ENTRY_GENERATORS selection routes through the generator
    # framework; with the default "sr_retest" the exact same find_best_entry()
    # call runs as before, so the default regression holds by construction.
    active_generators = entry_gen.enabled_generators()
    if active_generators and active_generators != [entry_gen.DEFAULT_GENERATORS]:
        gen_ctx = entry_gen.GeneratorContext(
            current_price, direction, opens, highs, lows, closes, volumes,
            swings, sr_levels, atr, df_by_tf=df_by_tf)
        merged = entry_gen.resolve_entry(gen_ctx, names=active_generators)
        entry_result = entry_gen.to_entry_result(merged)
        if entry_result is None:
            return {"skip": "no qualifying entry level found"}
        entry_method = list(merged.get("methods") or [entry_gen.DEFAULT_GENERATORS])
    else:
        entry_result = find_best_entry(current_price, direction, sr_levels)
        if entry_result is None:
            return {"skip": "no qualifying entry level found"}
        entry_method = [entry_gen.DEFAULT_GENERATORS]

    trade = get_trade_levels(
        entry_result["entry_price"], direction, atr, swings, sr_levels
    )
    if trade is None:
        return {"skip": "sl/tp calculation failed"}

    check = validate_trade(trade, current_price)
    if not check["valid"]:
        return {"skip": f"invalid trade: {check['reasons']}"}

    last_idx = len(closes) - 1
    return {
        "swings_found": len(swings),
        "sr_levels_found": len(sr_levels),
        "consolidation_zones": len(zones),
        "sr_breakouts": len(sr_breaks),
        "consolidation_breakouts": len(cons_breaks),
        "wick_rejections_at_level": len(wick_hits),
        "volume_spikes": len(vol_spikes),
        "volume_divergences": len(vol_div),
        "level_touches": entry_result["level_touches"],
        "entry_method": entry_method,
        "trade": trade,
        "wick_at_entry": any(w["index"] >= last_idx - 2 for w in wick_hits),
        "volume_confirm": any(v["index"] >= last_idx - 2 for v in vol_spikes),
    }


def analyze_pair(pair, timeframe, opens, highs, lows, closes, volumes,
                  direction, pattern_name=None, trend_aligned=False,
                  mtf_full_alignment=False, structure_bos_align=False,
                  liquidity_target=False, no_mss_conflict=True, db_path=paths.SIGNALS_DB,
                  chart_pattern_info=None, retracement_info=None,
                  fib_info=None, breakout_info=None, elliott_info=None,
                  structure_retest_info=None, session_info=None,
                  min_confluence_score=50, prepared=None):
    """
    Core execution logic: structure, entry, SL/TP, confluence.

    Pass prepared=_prepare_candidate(...) to reuse an already-validated
    candidate (the bias wrapper does this); None keeps the legacy behaviour of
    computing everything internally.
    """
    ctx = prepared if prepared is not None else \
        _prepare_candidate(opens, highs, lows, closes, volumes, direction)
    if "skip" in ctx:
        return {"skipped": ctx["skip"]}

    trade = ctx["trade"]

    # Engine facts when the bias wrapper supplied them; legacy direct callers
    # that pass nothing keep the old zero-contribution behaviour.
    retracement_confirmed = bool(retracement_info and retracement_info.get("trade_ready"))
    fib_score_val = int(fib_info.get("score") or 0) if fib_info else 0
    breakout_score_val = int(breakout_info.get("score") or 0) if breakout_info else 0
    elliott_score_val = int(elliott_info.get("score") or 0) if elliott_info else 0
    session_score_val = int(session_info.get("score") or 0) if session_info else 0

    score_result = calculate_confluence({
        "pattern_match": pattern_name is not None,
        "trend_align": trend_aligned,
        "mtf_alignment": mtf_full_alignment,
        "sr_level_strength": ctx["level_touches"],
        "wick_rejection": ctx["wick_at_entry"],
        "fib_score": fib_score_val,
        "volume_confirm": ctx["volume_confirm"],
        "structure_bos_align": structure_bos_align,
        "liquidity_target": liquidity_target,
        "no_mss_conflict": no_mss_conflict, # NEW: Added to confluence scoring
        "chart_pattern_align": bool(chart_pattern_info and chart_pattern_info.get("trade_ready")),
        "retracement_confirm": retracement_confirmed,
        "breakout_score": breakout_score_val,
        "elliott_score": elliott_score_val,
        "structure_retest_confirm": bool(structure_retest_info and structure_retest_info.get("trade_ready")),
        "session_timing": session_score_val,
    })

    # Conviction gate: weighted evidence decides, not presence of every input.
    # A signal only fires when the gathered confluence is at least MEDIUM
    # (default 50). Pass min_confluence_score=0 to disable.
    if score_result["score"] < min_confluence_score:
        out = {
            "skipped": f"low conviction: confluence {score_result['score']}/100 "
                       f"below required {min_confluence_score}",
            "confluence_breakdown": score_result["breakdown"],
            "entry_method": ctx.get("entry_method", [entry_gen.DEFAULT_GENERATORS]),
            "chart_pattern": chart_pattern_info,
            "retracement": retracement_info,
            "fibonacci": fib_info,
            "breakout_engine": breakout_info,
            "elliott": elliott_info,
            "structure_retest": structure_retest_info,
            "session": session_info,
        }
        # Build #9 transparency: engines skipped because the bound proved the
        # verdict before they ran. Only present when pruning actually fired.
        pruned = [name for name, blk in (
            ("chart_pattern", chart_pattern_info),
            ("fibonacci", fib_info),
            ("breakout_engine", breakout_info),
            ("elliott", elliott_info),
            ("structure_retest", structure_retest_info),
            ("retracement", retracement_info),
        ) if isinstance(blk, dict) and blk.get("stage") == _PRUNED_STAGE]
        if pruned:
            out["engines_pruned"] = pruned
        return out

    # db_path=None => compute and return WITHOUT logging. 
    # The signal maths above is unchanged either way; this only gates the side effect, 
    # so a stateless preview (POST /predict) cannot pollute the signals.db ledger 
    # that live and backtest runs share.
    if db_path is not None:
        init_db(db_path)
        log_signal(
            pair=pair, timeframe=timeframe, direction=direction,
            entry_price=trade["entry"], sl_price=trade["sl"]["sl_price"],
            tp_price=trade["tp"]["tp_price"], rr=trade["tp"]["rr"],
            confluence_score=score_result["score"], valid=True,
            pattern=pattern_name, db_path=db_path,
        )

    return {
        "pair": pair,
        "timeframe": timeframe,
        "direction": direction,
        "entry": trade["entry"],
        "entry_level_touches": ctx["level_touches"],
        "entry_method": ctx.get("entry_method", [entry_gen.DEFAULT_GENERATORS]),
        "sl": trade["sl"]["sl_price"],
        "sl_method": trade["sl"]["method"],
        "tp": trade["tp"]["tp_price"],
        "tp_method": trade["tp"]["method"],
        "rr": trade["tp"]["rr"],
        "confluence_score": score_result["score"],
        "confidence": confidence_label(score_result["score"]),
        "confluence_breakdown": score_result["breakdown"],
        "swings_found": ctx["swings_found"],
        "sr_levels_found": ctx["sr_levels_found"],
        "consolidation_zones": ctx["consolidation_zones"],
        "sr_breakouts": ctx["sr_breakouts"],
        "consolidation_breakouts": ctx["consolidation_breakouts"],
        "wick_rejections_at_level": ctx["wick_rejections_at_level"],
        "volume_spikes": ctx["volume_spikes"],
        "volume_divergences": ctx["volume_divergences"],
        "no_mss_conflict": no_mss_conflict,
        "chart_pattern": chart_pattern_info,
        "retracement": retracement_info,
        "fibonacci": fib_info,
        "breakout_engine": breakout_info,
        "elliott": elliott_info,
        "structure_retest": structure_retest_info,
        "session": session_info,
    }


if __name__ == "__main__":
    import random
    random.seed(7)
    n = 300
    o, h, l, c, v = [], [], [], [], []
    p = 100.0
    for _ in range(n):
        op = p
        cl = p + random.uniform(-1.2, 1.2)
        hi = max(op, cl) + random.uniform(0, 0.8)
        lo = min(op, cl) - random.uniform(0, 0.8)
        o.append(op); h.append(hi); l.append(lo); c.append(cl)
        v.append(random.uniform(100, 300))
        p = cl

    result = analyze_pair(
        pair="TESTUSDT", timeframe="4h",
        opens=o, highs=h, lows=l, closes=c, volumes=v,
        direction="LONG", pattern_name="hammer",
        trend_aligned=True, mtf_full_alignment=False,
        db_path="/tmp/test_pipeline.db",
    )
    import json
    print(json.dumps(result, indent=2, default=str))
