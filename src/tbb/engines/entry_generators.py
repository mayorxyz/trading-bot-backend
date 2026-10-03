"""
entry_generators.py — entry-generator framework (additive, flag-gated).

A generator proposes zero or more entry Candidates from the same read-only
inputs the pipeline already computes at candidate-preparation time. The default
registry contains only "sr_retest": a thin wrapper around
engines.entry.find_best_entry() that carries the untouched result through as its
"_legacy" dict. With the default flag, merge() + to_entry_result() therefore
reproduce find_best_entry()'s output byte-for-byte, so the funnel, trades, entry
prices and run_symbol return arity are unchanged.

This module is a pure selection layer. It changes no threshold, gate, SL/TP or
confluence logic, and it imports none of support_resistance, confluence or bias.
"""

import os

from tbb.engines.entry import find_best_entry


DEFAULT_GENERATORS = "sr_retest"
MERGE_PRICE_TOL_PCT = 0.25

# Keys every Candidate carries (the framework contract). Generators may add
# private keys prefixed with "_" (such as _legacy / _hint_score); those never
# reach the downstream entry result.
CANDIDATE_KEYS = (
    "method", "direction", "entry_price", "entry_type",
    "zone_low", "zone_high", "level_touches", "reasons",
    "bias_aligned", "sr_aligned",
)


class GeneratorContext:
    """Read-only inputs shared by every generator.

    Mirrors — without copying — the frames and artifacts the pipeline already
    holds when it prepares a candidate. Treat every attribute as READ-ONLY:
    frames and lists are shared references, exactly like the pipeline's own ctx.
    GeneratorContext is a NEW object used only by generators; the existing
    pipeline ctx dict is untouched, so its consumers see no change.
    """

    __slots__ = ("current_price", "direction", "opens", "highs", "lows",
                 "closes", "volumes", "swings", "sr_levels", "atr", "df_by_tf")

    def __init__(self, current_price, direction, opens, highs, lows, closes,
                 volumes, swings, sr_levels, atr, df_by_tf=None):
        self.current_price = current_price
        self.direction = direction
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.volumes = volumes
        self.swings = swings
        self.sr_levels = sr_levels
        self.atr = atr
        self.df_by_tf = df_by_tf if df_by_tf is not None else {}


# ---- registry -----------------------------------------------------------
_REGISTRY = {}
_ERROR_COUNTS = {}
_INDEPENDENT_METHODS = set()


def register(name, independent_direction=False):
    """Decorator: add a generator under `name`."""
    def _deco(fn):
        _REGISTRY[name] = fn
        if independent_direction:
            _INDEPENDENT_METHODS.add(name)
        return fn
    return _deco


def all_methods():
    return sorted(_REGISTRY)


def independent_direction_methods():
    """Names of generators that can run without a bias direction."""
    return sorted(_INDEPENDENT_METHODS)


def reset_error_counts():
    _ERROR_COUNTS.clear()


def error_counts():
    """Per-method count of generator exceptions seen since the last reset."""
    return dict(_ERROR_COUNTS)


def enabled_generators():
    """Names selected by ENTRY_GENERATORS (comma list), filtered to the registry.

    Default is "sr_retest". Unknown names are silently ignored so a stale env
    value can never break the pipeline.
    """
    raw = os.environ.get("ENTRY_GENERATORS", DEFAULT_GENERATORS)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return [n for n in names if n in _REGISTRY]


# ---- generators ---------------------------------------------------------
@register("sr_retest")
def _gen_sr_retest(ctx):
    """Today's single entry path: the nearest strongest S/R level in the bias
    direction. Wraps find_best_entry() unchanged and preserves its exact dict
    under "_legacy" so the default path is a byte-for-byte passthrough."""
    res = find_best_entry(ctx.current_price, ctx.direction, ctx.sr_levels)
    if res is None:
        return []
    return [{
        "method": "sr_retest",
        "direction": ctx.direction,
        "entry_price": res["entry_price"],
        "entry_type": "limit",
        "zone_low": None,
        "zone_high": None,
        "level_touches": res["level_touches"],
        "reasons": ["sr_level_retest", res.get("level_type")],
        "bias_aligned": True,
        "sr_aligned": True,
        "_hint_score": int(res.get("level_touches") or 0),
        "_legacy": dict(res),
    }]


def run_generators(ctx, names=None):
    """Run the selected generators and return the flat candidate list.

    A generator that raises is caught, counted once under its method name and
    contributes no candidates; every other generator still runs.
    """
    if names is None:
        names = enabled_generators()
    out = []
    for name in names:
        fn = _REGISTRY.get(name)
        if fn is None:
            continue
        try:
            cands = fn(ctx) or []
        except Exception:
            _ERROR_COUNTS[name] = _ERROR_COUNTS.get(name, 0) + 1
            cands = []
        for cand in cands:
            if not isinstance(cand, dict):
                continue
            cand.setdefault("method", name)
            out.append(cand)
    return out


# ---- merge --------------------------------------------------------------
def _hint(cand):
    """Ranking score used only to pick within a merge group."""
    return int(cand.get("_hint_score") or 0)


def _same_direction(a, b):
    return a.get("direction") == b.get("direction")


def _within_tol(pa, pb):
    try:
        pa = float(pa)
        pb = float(pb)
    except (TypeError, ValueError):
        return False
    if pa == 0:
        return pb == 0
    return abs(pa - pb) / pa * 100.0 <= MERGE_PRICE_TOL_PCT


def merge(candidates):
    """Collapse candidates into one chosen entry dict, or None.

    Candidates in the same direction whose entry prices are within 0.25% form one
    group; the group's highest _hint_score wins and every agreeing method is
    recorded under "methods". The overall winner across groups is returned. The
    winner keeps its "_legacy" dict when it came from sr_retest, so the
    downstream entry result is preserved.
    """
    if not candidates:
        return None
    groups = []
    for cand in candidates:
        if cand.get("entry_price") is None:
            continue
        grp = None
        for g in groups:
            if (_same_direction(g["winner"], cand)
                    and _within_tol(g["winner"]["entry_price"], cand["entry_price"])):
                grp = g
                break
        if grp is None:
            groups.append({"winner": cand, "methods": [cand.get("method")]})
        else:
            grp["methods"].append(cand.get("method"))
            if _hint(cand) > _hint(grp["winner"]):
                grp["winner"] = cand
    if not groups:
        return None
    best = max(groups, key=lambda g: _hint(g["winner"]))
    chosen = dict(best["winner"])
    chosen["methods"] = sorted({m for m in best["methods"] if m})
    return chosen


def to_entry_result(merged):
    """Project a merged candidate back onto the legacy entry dict the pipeline
    consumes ({entry_price, level_touches, level_type, distance_pct}).

    When the chosen candidate carries "_legacy" (the default sr_retest path) the
    original find_best_entry() dict is returned verbatim.
    """
    if not merged:
        return None
    legacy = merged.get("_legacy")
    if legacy is not None:
        return dict(legacy)
    return {
        "entry_price": merged.get("entry_price"),
        "level_touches": merged.get("level_touches"),
        "level_type": merged.get("level_type"),
        "distance_pct": merged.get("distance_pct"),
    }


def resolve_entry(ctx, names=None):
    """Convenience: run the selected generators, merge, and return the merged
    candidate dict (with "methods") or None."""
    return merge(run_generators(ctx, names=names))
