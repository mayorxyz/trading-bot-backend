"""
rejection_diagnostics.py — additive, read-only diagnostics for the backtest
funnel.

Records WHY test points were rejected and prints aggregate statistics meant to
sit directly under the existing funnel report. Nothing here feeds back into the
strategy: no threshold, gate, ordering, fill or journal value is read back or
modified, so the funnel counts and trade results are bit-identical with the
feature on or off.

Enabled by one flag, BACKTEST_REJECTION_DIAGNOSTICS (default "1" in the backtest
CLI entry points, never set by the live path).

Stages, in real funnel order (see backtest.run_symbol):
    insufficient_htf -> slots -> bias -> S/R(+entry/sl_tp/validity)
    -> confluence -> zone -> cooldown -> fill

Only the two decision stages that can be diagnosed without re-deriving a trade
plan are recorded here:
    * confluence: low conviction   (pipeline already returns the breakdown)
    * entry: no qualifying S/R level
"""

import os
import statistics

from tbb.engines import entry_generators as entry_gen
from tbb.engines.confluence import WEIGHTS

# All 16 confluence inputs, in WEIGHTS order; WEIGHTS[k] is each one's max
# contribution and every component's earned value is exactly what
# pipeline.analyze_pair() already returns in "confluence_breakdown".
COMPONENTS = list(WEIGHTS.keys())

# Replica of the find_best_entry() qualification constants. These MIRROR the
# defaults in engines/entry.py (min_touches=2, max_distance_pct=2.0) and the
# tolerance default in indicators/support_resistance.py (tolerance_pct=0.5).
# entry.py is deliberately NOT modified or imported for the check — the
# conditions are reproduced here so the real gate stays untouched.
SR_MIN_TOUCHES = 2
SR_MAX_DISTANCE_PCT = 2.0
SR_TOLERANCE_PCT = 0.5


def diagnostics_enabled() -> bool:
    """One flag for the whole feature (backtest default ON, live OFF)."""
    return os.environ.get("BACKTEST_REJECTION_DIAGNOSTICS", "1") != "0"


def _bucket10(score):
    """Label for the width-10 score bucket holding `score`."""
    lo = int(score // 10) * 10
    return f"{lo}-{lo + 9}"


def _gap_bucket(gap):
    """Label for the gap-to-threshold bucket holding `gap`."""
    if gap <= 5:
        return "1-5"
    if gap <= 10:
        return "6-10"
    if gap <= 20:
        return "11-20"
    return "21+"


# Forward test horizons, in bars after the signal bar.
HORIZONS = (4, 8, 12)


def forward_stats(df_exec, signal_bar_index, direction, horizons=HORIZONS):
    """No-look-ahead direction quality for one signal bar — the single helper
    used for every direction-quality row.

    Reference price is the close of the signal bar; only bars strictly after it
    are read (index = signal_bar_index + h). Signed return is
    (close[i+h] - ref) / ref, multiplied by +1 for LONG / -1 for SHORT; a hit is
    signed return > 0. A horizon with fewer than h bars remaining is skipped
    (recorded as None) so it is not counted in that horizon's n.

    Returns {h: {"signed": float, "hit": 0/1} or None} or None when there is no
    usable direction.
    """
    if direction not in ("LONG", "SHORT"):
        return None
    closes = df_exec["close"].to_numpy(dtype=float)
    n = len(closes)
    ref = float(closes[signal_bar_index])
    sign = 1.0 if direction == "LONG" else -1.0
    out = {}
    for h in horizons:
        j = signal_bar_index + h
        if j >= n or ref == 0:
            out[h] = None
        else:
            signed = (closes[j] - ref) / ref * sign
            out[h] = {"signed": signed, "hit": 1 if signed > 0 else 0}
    return out


class RejectionDiagnostics:
    """Accumulates per-symbol rejection records and prints the summary blocks.

    One instance per symbol per run_symbol() call; the CLI entry points create
    it (only when the flag is on) and print it under the funnel. API and any
    other caller that does not pass one simply gets no diagnostics.
    """

    def __init__(self, symbol, execution_tf="1H", threshold=50):
        self.symbol = symbol
        self.execution_tf = execution_tf
        self.threshold = threshold
        # STEP 1 — points rejected as "low conviction".
        self.confluence = []
        # STEP 2 — points rejected as "entry: no qualifying S/R level".
        self.sr = []
        self.sr_mismatches = []
        self.sr_no_direction = 0
        # STEP 4 — direction quality (no SL/TP). One flat list of records, each
        # tagged by category, so every printed row is a filter over this list.
        self.rows = []
        self._dir_by_pos = {}
        self._df_exec = None
        # Entry-generator diagnostics, keyed by method name.
        self.entry_methods = {}

    # ---- recording ------------------------------------------------------
    def record_skip(self, key, result, pos, context=None):
        """Dispatch a rejected point to the matching stage recorder.

        key is the classify_skip() bucket (computed by the caller so this module
        needs no import of backtest.py and creates no import cycle). context is
        the read-only frame bundle the S/R replica needs (see _record_sr); it is
        ignored by stages that do not need it.
        """
        if key == "confluence: low conviction":
            self._record_confluence(result, pos)
            self.record_entry_result(result, pos)
        elif key == "entry: no qualifying S/R level" and context is not None:
            self._record_sr(context)
        elif key.startswith("bias"):
            # Bias rejected the point. The pipeline's skip result exposes no
            # candidate direction, so the spec's "otherwise" branch applies:
            # counted as no direction.
            self._add_row("bias", None, pos)

    def record_entry_result(self, result, pos):
        """Method-level generator summary without altering any trade logic.

        The returned result may be a success dict with "entry_method" or a low-
        conviction skip containing "entry_method" as an additive field. When the
        method is absent, the legacy sr_retest fallback is assumed.
        """
        direction = self._dir_by_pos.get(pos)
        methods = result.get("entry_method") if isinstance(result, dict) else None
        if not methods:
            methods = [entry_gen.DEFAULT_GENERATORS]
        if isinstance(methods, str):
            methods = [methods]

        for method in methods:
            if not method:
                continue
            stats = self.entry_methods.setdefault(method, {
                "count": 0,
                "score_bucket": {},
                "confluence_rejected": 0,
                "rows": [],
                "score_values": [],
            })
            stats["count"] += 1
            score = result.get("confluence_score")
            if score is not None:
                try:
                    score = int(round(float(score)))
                except (TypeError, ValueError):
                    score = None
                if score is not None:
                    stats["score_values"].append(score)
                    lab = _bucket10(score)
                    stats["score_bucket"][lab] = stats["score_bucket"].get(lab, 0) + 1
            if isinstance(result, dict) and result.get("skipped") and str(result.get("skipped")).startswith("low conviction"):
                stats["confluence_rejected"] += 1
            fwd = (forward_stats(self._df_exec, pos, direction)
                   if (direction in ("LONG", "SHORT") and self._df_exec is not None)
                   else None)
            if fwd is not None:
                stats["rows"].append({"category": f"entry:{method}", "direction": direction,
                                       "pos": pos, "fwd": fwd})
            else:
                stats["rows"].append({"category": f"entry:{method}", "direction": direction,
                                       "pos": pos, "fwd": None})

    # ---- step 4 helpers -------------------------------------------------
    def begin_point(self, pos, hist_1d, hist_4h, hist_exec, df_exec):
        """Once per test point, exactly as the pipeline would: resolve the bias
        direction and record the BASELINE direction-quality row (no entry, no
        SL/TP required). Called after the insufficient_htf gate, so slots- and
        later-rejected points are still counted in the baseline.
        """
        from tbb.bias_bridge import resolve_bias
        self._df_exec = df_exec
        bias = resolve_bias({"1D": hist_1d, "4H": hist_4h, "1H": hist_exec})
        direction = bias.get("direction")
        self._dir_by_pos[pos] = direction
        if direction:
            self._add_row("baseline", direction, pos)
        return direction

    def _add_row(self, category, direction, pos):
        """Append one direction-quality record using the shared forward_stats."""
        fwd = (forward_stats(self._df_exec, pos, direction)
               if (direction in ("LONG", "SHORT") and self._df_exec is not None)
               else None)
        self.rows.append({"category": category, "direction": direction,
                          "pos": pos, "fwd": fwd})

    def record_candidate(self, category, pos, direction):
        """A point that passed every gate (pipeline produced a trade plan),
        split by the post-signal gate that handled it: zone_occupied, cooldown,
        no_fill, timeout or filled."""
        self._add_row(f"cand:{category}", direction, pos)

    def _record_sr(self, context):
        """Read-only replica of find_best_entry()'s qualification, stage by
        stage, for a point the real gate already rejected as "no qualifying S/R
        level".

        Uses the exact same inputs the pipeline used: current price (last close),
        the direction resolve_bias() chose for this bar, and the level list from
        find_sr_levels() over the same 200-bar window. It never calls or edits
        find_best_entry — the conditions are reproduced from its source.
        """
        from tbb.indicators.zigzag import get_zigzag_swings
        from tbb.indicators.support_resistance import find_sr_levels

        recent = context["recent"]
        highs = recent["high"].to_numpy(dtype=float)
        lows = recent["low"].to_numpy(dtype=float)
        closes = recent["close"].to_numpy(dtype=float)
        current_price = float(closes[-1])

        # Reuse the direction resolved once at begin_point() for this bar
        # (identical resolve_bias() call, so the value is the same and no
        # second resolve is needed). Fall back only if begin_point was skipped.
        pos = context.get("pos")
        if pos in self._dir_by_pos:
            direction = self._dir_by_pos[pos]
        else:
            from tbb.bias_bridge import resolve_bias
            direction = resolve_bias({"1D": context["hist_1d"],
                                      "4H": context["hist_4h"],
                                      "1H": context["hist_exec"]}).get("direction")

        swings = get_zigzag_swings(highs, lows, closes)
        levels = find_sr_levels(swings, tolerance_pct=SR_TOLERANCE_PCT)

        # Stage 1: found by find_sr_levels.
        found = len(levels)
        # Stage 2: touches >= 2.
        after_touches = [l for l in levels if l["touches"] >= SR_MIN_TOUCHES]
        # Stage 3: correct side for the chosen direction (excluded when no dir).
        if direction == "LONG":
            after_side = [l for l in after_touches
                          if l["price"] <= current_price
                          and l["type"] in ("SUPPORT", "BOTH")]
        elif direction == "SHORT":
            after_side = [l for l in after_touches
                          if l["price"] >= current_price
                          and l["type"] in ("RESISTANCE", "BOTH")]
        else:
            after_side = []

        def dist_pct(lvl):
            return abs(current_price - lvl["price"]) / current_price * 100 if current_price else float("inf")

        # Stage 4: distance <= 2.0%.
        after_dist = [l for l in after_side if dist_pct(l) <= SR_MAX_DISTANCE_PCT]
        nearest_dist = min((dist_pct(l) for l in after_side), default=None)

        # Reason = first stage that becomes empty.
        if found == 0:
            reason = "no_levels"
        elif not after_touches:
            reason = "weak_touches"
        elif direction is None:
            reason = "no_direction"
        elif not after_side:
            reason = "wrong_side"
        elif not after_dist:
            reason = "too_far"
        else:
            reason = "other"

        if direction is None:
            self.sr_no_direction += 1

        # Consistency: the real gate returned None, so the replica must also
        # end with zero surviving levels. Anything else is a mismatch.
        if after_dist:
            self.sr_mismatches.append({
                "pos": int(context.get("pos", -1)),
                "found": found,
                "after_touches": len(after_touches),
                "after_side": len(after_side),
                "after_dist": len(after_dist),
                "direction": direction,
                "reason": reason,
            })

        self.sr.append({
            "pos": pos,
            "found": found,
            "reason": reason,
            "nearest_dist": nearest_dist,
        })

        # STEP 4 — direction quality for this S/R rejection, per reason code.
        self._add_row(f"sr:{reason}", direction, pos)

    def _record_confluence(self, result, pos):
        breakdown = result.get("confluence_breakdown") or {}
        comps = {k: float(breakdown.get(k, 0.0)) for k in COMPONENTS}
        score = int(round(sum(comps.values())))
        self.confluence.append({
            "pos": pos,
            "score": score,
            "threshold": self.threshold,
            "gap": self.threshold - score,
            "comps": comps,
        })

        # STEP 4 — direction quality for this confluence rejection, per score
        # bucket and per gap bucket. Direction comes from the bar's bias.
        direction = self._dir_by_pos.get(pos)
        self._add_row(f"conf_score:{_bucket10(score)}", direction, pos)
        self._add_row(f"conf_gap:{_gap_bucket(self.threshold - score)}", direction, pos)

    # ---- reporting ------------------------------------------------------
    def print_report(self):
        print(f"\n--- rejection diagnostics: {self.symbol} ---")
        self._print_confluence()
        self._print_sr()
        self._print_entry_generators()
        self._print_direction_quality()
        print("diagnostics: funnel counts unchanged by design")

    def _print_entry_generators(self):
        print("entry generators")
        errs = entry_gen.error_counts()
        methods = sorted(set(self.entry_methods) | set(errs))
        if not methods:
            print("  no generator candidates observed")
            return
        for method in methods:
            stats = self.entry_methods.get(method, {"count": 0, "score_bucket": {},
                                                   "confluence_rejected": 0, "rows": []})
            rows = stats["rows"]
            s = self._summarize(rows)
            print(f"  {method:18s} produced={stats['count']:4d}  errors={errs.get(method, 0):4d}  "
                  f"rejected_at_confluence={stats['confluence_rejected']:4d}")
            if stats["score_values"]:
                dist = {}
                for score in stats["score_values"]:
                    lab = _bucket10(score)
                    dist[lab] = dist.get(lab, 0) + 1
                print("    confluence score buckets: " + ", ".join(f"{b}={dist.get(b, 0)}" for b in sorted(dist)))
            else:
                print("    confluence score buckets: none")
            if s["count"]:
                print("    forward_stats: " + "  ".join(self._fmt_h(s, h) for h in HORIZONS))
                self._print_split(f"    method:{method}", rows)
            else:
                print("    forward_stats: no usable direction rows")

    def _print_confluence(self):
        n = len(self.confluence)
        print(f"confluence: low conviction = {n}")
        if not n:
            return

        # score distribution (10-point buckets up to the threshold)
        dist = {}
        for r in self.confluence:
            lab = _bucket10(r["score"])
            dist[lab] = dist.get(lab, 0) + 1
        print("score distribution:")
        hi = max(self.threshold - 1, 0)
        for lo in range(0, hi + 1, 10):
            lab = f"{lo}-{lo + 9}"
            print(f"  {lab:>6}: {dist.get(lab, 0)}")

        # gap to threshold
        gaps = {"1-5": 0, "6-10": 0, "11-20": 0, "21+": 0}
        for r in self.confluence:
            g = r["gap"]
            if g <= 5:
                gaps["1-5"] += 1
            elif g <= 10:
                gaps["6-10"] += 1
            elif g <= 20:
                gaps["11-20"] += 1
            else:
                gaps["21+"] += 1
        print("gap to threshold: " + ", ".join(f"{b}={gaps[b]}" for b in gaps))

        # components: count of points where earned == 0, and mean earned/max,
        # sorted by zero-count descending (then by mean descending).
        rows = []
        for k in COMPONENTS:
            mx = WEIGHTS[k] or 1
            zeros = sum(1 for r in self.confluence if r["comps"][k] == 0.0)
            frac = sum(r["comps"][k] / mx for r in self.confluence) / n
            rows.append((k, zeros, frac))
        rows.sort(key=lambda t: (-t[1], -t[2]))
        print("components (zero-count of rejected, mean earned/max):")
        for k, zeros, frac in rows:
            print(f"  {k:26s} zero={zeros:5d}/{n}  mean={frac:.3f}")

    def _print_sr(self):
        n = len(self.sr)
        print(f"entry: no qualifying S/R level = {n}")
        if not n:
            print("no direction (excluded): 0")
            print(f"replica mismatches = {len(self.sr_mismatches)}")
            return

        # levels found per point, bucketed 0 / 1 / 2 / 3+
        found_b = {"0": 0, "1": 0, "2": 0, "3+": 0}
        for r in self.sr:
            f = r["found"]
            key = "0" if f == 0 else "1" if f == 1 else "2" if f == 2 else "3+"
            found_b[key] += 1
        print("levels found per point: " + ", ".join(f"{k}={found_b[k]}" for k in ("0", "1", "2", "3+")))

        # rejection reasons, sorted descending by count
        reasons = {}
        for r in self.sr:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        print("rejection reasons:")
        for code, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {code:14s}: {cnt}")

        # too_far nearest-distance distribution
        too_far = [r["nearest_dist"] for r in self.sr
                   if r["reason"] == "too_far" and r["nearest_dist"] is not None]
        if too_far:
            med = statistics.median(too_far)
            mx = max(too_far)
            b = {"2-3%": 0, "3-5%": 0, "5%+": 0}
            for d in too_far:
                if d <= 3.0:
                    b["2-3%"] += 1
                elif d <= 5.0:
                    b["3-5%"] += 1
                else:
                    b["5%+"] += 1
            print(f"too_far distance: median={med:.2f}%  max={mx:.2f}%  "
                  + ", ".join(f"{k}={b[k]}" for k in ("2-3%", "3-5%", "5%+")))
        elif any(r["reason"] == "too_far" for r in self.sr):
            print("too_far distance: (none)")

        print(f"no direction (excluded): {self.sr_no_direction}")

        mismatches = len(self.sr_mismatches)
        print(f"replica mismatches = {mismatches}")
        if mismatches:
            # Per spec: do not continue past a mismatch — report the cases.
            print("  STOP: replica found qualifying levels the real gate rejected:")
            for case in self.sr_mismatches:
                print(f"    pos={case['pos']} found={case['found']} "
                      f"after_touches={case['after_touches']} "
                      f"after_side={case['after_side']} "
                      f"after_dist={case['after_dist']} "
                      f"direction={case['direction']} reason={case['reason']}")

    # ---- step 4: direction quality --------------------------------------
    def _summarize(self, rows):
        """Aggregate a set of direction-quality records for the shared rows."""
        s = {"count": len(rows),
             "no_direction": sum(1 for r in rows if r["direction"] not in ("LONG", "SHORT")),
             "n": {h: 0 for h in HORIZONS},
             "hits": {h: 0 for h in HORIZONS},
             "signed_sum": {h: 0.0 for h in HORIZONS}}
        for r in rows:
            fwd = r.get("fwd")
            if not fwd:
                continue
            for h in HORIZONS:
                v = fwd.get(h)
                if v is not None:
                    s["n"][h] += 1
                    s["hits"][h] += v["hit"]
                    s["signed_sum"][h] += v["signed"]
        return s

    @staticmethod
    def _fmt_h(s, h):
        n = s["n"][h]
        if not n:
            return f"h{h}: n=0"
        return (f"h{h}: n={n} hit={s['hits'][h] / n:.3f} "
                f"mean={s['signed_sum'][h] / n:+.4f}")

    def _print_dq_row(self, label, rows):
        s = self._summarize(rows)
        low = "  [low sample]" if s["count"] < 30 else ""
        nd = f" no_dir={s['no_direction']}" if s["no_direction"] else ""
        print(f"  {label:28s} count={s['count']:5d}{nd}{low}  "
              + "  ".join(self._fmt_h(s, h) for h in HORIZONS))

    def _print_split(self, label, rows):
        for d in ("LONG", "SHORT"):
            sub = [r for r in rows if r["direction"] == d]
            s = self._summarize(sub)
            print(f"      {label} [{d:5s}] count={s['count']:5d}  "
                  + "  ".join(self._fmt_h(s, h) for h in HORIZONS))

    def _print_direction_quality(self):
        print("direction quality (no SL/TP, reference = signal bar close)")
        groups = {}
        for r in self.rows:
            groups.setdefault(r["category"], []).append(r)

        baseline = groups.get("baseline", [])
        self._print_dq_row("a) baseline (bias dir)", baseline)
        self._print_dq_row("b) bias-rejected", groups.get("bias", []))

        sr_all = [r for r in self.rows if r["category"].startswith("sr:")]
        for code in ("no_levels", "weak_touches", "wrong_side", "too_far",
                     "no_direction", "other"):
            rows = groups.get(f"sr:{code}", [])
            if rows:
                self._print_dq_row(f"c) S/R {code}", rows)

        conf_score = [r for r in self.rows if r["category"].startswith("conf_score:")]
        conf_gap = [r for r in self.rows if r["category"].startswith("conf_gap:")]
        for lo in range(0, max(self.threshold, 10), 10):
            lab = f"{lo}-{lo + 9}"
            rows = groups.get(f"conf_score:{lab}", [])
            if rows:
                self._print_dq_row(f"d) conf score {lab}", rows)
        for gb in ("1-5", "6-10", "11-20", "21+"):
            rows = groups.get(f"conf_gap:{gb}", [])
            if rows:
                self._print_dq_row(f"d) conf gap {gb}", rows)

        for cat in ("zone_occupied", "cooldown", "no_fill", "timeout", "filled"):
            rows = groups.get(f"cand:{cat}", [])
            if rows:
                self._print_dq_row(f"e) candidate:{cat}", rows)
        cand_all = [r for r in self.rows if r["category"].startswith("cand:")]
        self._print_dq_row("e) all candidates", cand_all)
        filled = groups.get("cand:filled", [])
        self._print_dq_row("f) accepted (filled)", filled)

        # LONG / SHORT split for rows a, c-too_far, d(all), e(all).
        self._print_split("baseline", baseline)
        self._print_split("S/R too_far", groups.get("sr:too_far", []))
        self._print_split("conf all buckets", conf_score + conf_gap)
        self._print_split("all candidates", cand_all)

        # Hit-rate difference (percentage points) at 8 bars vs baseline.
        base = self._summarize(baseline)
        base_n = base["n"][8]
        base_hit = base["hits"][8] / base_n if base_n else None

        def diff(rows):
            if base_hit is None:
                return "n/a"
            s = self._summarize(rows)
            if not s["n"][8]:
                return "n/a"
            return f"{(s['hits'][8] / s['n'][8] - base_hit) * 100:+.1f}pp"

        print("hit-rate @8 vs baseline: "
              f"b(bias)={diff(groups.get('bias', []))}  "
              f"c(S/R)={diff(sr_all)}  "
              f"d(conf)={diff(conf_score + conf_gap)}  "
              f"e(candidates)={diff(cand_all)}  "
              f"f(filled)={diff(filled)}")
