# AUDIT_REPORT.md

## 1. File map

Files under tbb/:

- src/tbb/__init__.py — package marker; no runtime logic. Used by: live, backtest, predict, scan indirectly through module import paths.
- src/tbb/config.py — single source of truth for data directory and database paths; env-overridable. Used by: live, backtest, predict, scan.
- src/tbb/logsetup.py — rotating logger setup for live and scanner output. Used by: live, scan.
- src/tbb/formatting.py — stringify signal result dicts for console/alerts. Used by: live, scan.
- src/tbb/memokv.py — in-process cache keyed by immutable frame fingerprints. Used by: bias_bridge and the pipeline memo layer.
- src/tbb/bias_bridge.py — adapts phase1-3 (+5) outputs into the tradable bias/direction result. Used by: pipeline, live, backtest, predict, scan.
- src/tbb/pipeline.py — main signal pipeline: bias gate -> candidate prep -> engine bundle -> confluence -> log or skip. Used by: live, backtest, predict, scan.

Analysis modules:

- src/tbb/analysis/__init__.py — package marker.
- src/tbb/analysis/primitives.py — OHLC primitives, fractal swings, EMA trend filter. Used by: bias_bridge, regime, structure_liquidity, live state snapshots.
- src/tbb/analysis/imbalances.py — detects FVG/imbalance zones and mark/tested flags. Used by: regime, live runner snapshot, structure_liquidity.
- src/tbb/analysis/regime.py — regime classification and top-down bias resolution. Used by: bias_bridge, live runner, pipeline.
- src/tbb/analysis/risk_journal.py — trade journal and summary metrics. Used by: backtest.
- src/tbb/analysis/structure_liquidity.py — classify structure and liquidity on the execution frame. Used by: bias_bridge.
- src/tbb/analysis/supplementary.py — MSS, news blackout checks, session timing and crypto/forex cost models. Used by: pipeline and research-only modules.

Indicators:

- src/tbb/indicators/__init__.py — package marker.
- src/tbb/indicators/zigzag.py — ATR-based swing-point detector. Used by: live runner state overlay, structure detection, S/R, consolidation, retracement.
- src/tbb/indicators/support_resistance.py — S/R levels from swings. Used by: pipeline prep, live runner overlay.
- src/tbb/indicators/fibonacci.py — fib retracement analysis and trade-ready state. Used by: retracement and pipeline.
- src/tbb/indicators/wicks.py — wick/rejection detection. Used by: pipeline prep.
- src/tbb/indicators/volume.py — volume spike/divergence detection. Used by: pipeline prep.
- src/tbb/indicators/pattern_detector.py — TA-Lib pattern scan summaries. Used by: pipeline engine bundle.
- src/tbb/indicators/chart_patterns.py — chart pattern detection primitives. Used by: pattern_strategy.

Engines:

- src/tbb/engines/__init__.py — package marker.
- src/tbb/engines/breakouts.py — breakout detection utilities. Used by: pipeline prep.
- src/tbb/engines/breakout_engine.py — breakout score + trade_ready gating. Used by: pipeline engine bundle.
- src/tbb/engines/confluence.py — weighted score and confidence label. Used by: pipeline.
- src/tbb/engines/consolidation.py — consolidation zone detection. Used by: live runner overlay and pipeline prep.
- src/tbb/engines/entry.py — selects best directional entry level. Used by: pipeline prep.
- src/tbb/engines/elliott_wave.py — Elliott wave score and trade_ready. Used by: pipeline engine bundle.
- src/tbb/engines/pattern_strategy.py — chart-pattern trade plan. Used by: pipeline engine bundle.
- src/tbb/engines/retracement.py — fib pullback trade plan. Used by: pipeline engine bundle.
- src/tbb/engines/sl_tp.py — stop/take-profit price calculations. Used by: pipeline prep.
- src/tbb/engines/structure_retest.py — BOS retest setup. Used by: pipeline engine bundle.
- src/tbb/engines/validity.py — minimum RR and distance checks. Used by: pipeline prep.

Market data:

- src/tbb/marketdata/__init__.py — package marker.
- src/tbb/marketdata/ingestion_bybit.py — REST fetcher for Bybit klines and instruments. Used by: live runner, scanner, predict/market APIs.
- src/tbb/marketdata/market_data.py — Bybit chart caching and symbol/timeframe abstraction. Used by: API chart routes and predict.

Live/scanner:

- src/tbb/live/__init__.py — package marker.
- src/tbb/live/runner.py — live Bybit websocket loop and state persistence. Used by: script run_live.py and scanner.
- src/tbb/live/scanner.py — multi-symbol scan loop across USDT spot pairs. Used by: script run_scanner.py.
- src/tbb/live/trade_manager.py — two-step managed exits for live trade resolution. Used by: live runner.

Backtesting:

- src/tbb/backtesting/__init__.py — package marker.
- src/tbb/backtesting/backtest.py — historical replay engine and outcome evaluation; includes cooldowns and portfolio slot management. Used by: API analysis, scripts, manual backtesting.
- src/tbb/backtesting/profile_pipeline.py — profiling hook for pipeline hotspots. Used by: manual profiling.

Storage:

- src/tbb/storage/__init__.py — package marker.
- src/tbb/storage/live_store.py — live_state.db schema and API queries for ticks, zones, levels, trades. Used by: live runner and API.
- src/tbb/storage/analysis_store.py — analysis_jobs DB. Used by: analysis API.
- src/tbb/storage/signal_store.py — signals.db ledger. Used by: pipeline when db_path is passed.
- src/tbb/storage/scan_store.py — scan_runs.db for scan results. Used by: scan routes.
- src/tbb/storage/pattern_stats.py — pattern statistics store. Unclear if active in current signal path.

API:

- src/tbb/api/__init__.py — package marker.
- src/tbb/api/app.py — FastAPI app creation and router inclusion. Used by: serve script.
- src/tbb/api/common.py — API validation, timeframe regexes, CSV helpers, overlay freshness. Used by: routes_*.
- src/tbb/api/routes_live.py — /live/state, /live/skips, /live/zones, /live/levels, /live/stats. Used by: frontend.
- src/tbb/api/routes_market.py — /symbols and /ohlc. Used by: chart UI.
- src/tbb/api/routes_predict.py — /predict for one-off stateless signal prediction. Used by: frontend; not persistent.
- src/tbb/api/routes_scan.py — /scan start/stop/results. Used by: scanner UI.
- src/tbb/api/routes_analysis.py — /analyze jobs and results. Used by: analysis workflows.

Scripts:

- scripts/run_live.py — launcher for live runner. Used by: live.
- scripts/run_scanner.py — launcher for multi-symbol scanner. Used by: scan.
- scripts/serve.py — uvicorn entrypoint. Used by: API.
- scripts/fetch_history.py — historical data fetcher via Bybit REST; writes CSVs. Used by: backtest and chart history.

Data/config notes:

- data/ is the runtime artifact directory; it is created by src/tbb/config.py and used for CSV and DB files.
- The live database is intentionally separate from analysis and signal logs. See src/tbb/config.py plus comments in src/tbb/storage/live_store.py and readme.md.

---

## 2. Live signal pipeline, end to end

Source path: scripts/run_live.py -> src/tbb/live/runner.py -> src/tbb/pipeline.py -> src/tbb/storage/live_store.py.

Order of execution:

1. scripts/run_live.py main() calls tbb.live.runner.main(); it normalizes CLI args, loads symbol(s), seeds history, and starts the WebSocket loop (scripts/run_live.py, main() and _cli_symbols(); src/tbb/live/runner.py, _cli_symbols() and main()).
2. src/tbb/live/runner.py seed_history() fetches 1D/4H/1H/15M candles via fetch_klines() and stores them in memory; it does not write CSVs (src/tbb/live/runner.py, seed_history() and module docstring; src/tbb/live/runner.py, _normalise()).
3. WebSocket updates are appended via append_or_replace_row(), keeping only the last MAX_ROWS bars; process continues only for confirmed candles (src/tbb/live/runner.py, append_or_replace_row(); run_live_stream() checks candle.get("confirm")).
4. On confirmed 1H/15M closes, run_live_stream() calls run_analysis(df_by_tf, execution_tf=tf, symbol=symbol) (src/tbb/live/runner.py, run_live_stream(), TRIGGER_TFS = {"1H", "15M"}).
5. run_analysis() slices recent = df_by_tf[execution_tf].tail(200), converts the last 200 bars to numpy arrays, and calls analyze_pair_with_bias() with bias frames 1D/4H/1H and execution arrays from the current trigger TF (src/tbb/live/runner.py, run_analysis()).
6. analyze_pair_with_bias() resolves bias via resolve_bias(df_by_tf), rejects early if bias is not tradable, optionally enforces news blackout, performs cheap candidate preparation with _prepare_candidate(), then runs engine bundle evaluation and confluence scoring (src/tbb/pipeline.py, analyze_pair_with_bias() and _prepare_candidate()).
7. _prepare_candidate() does: get_zigzag_swings() -> find_sr_levels() -> find_consolidation_zones() -> detect_sr_breakout() / detect_consolidation_breakout() -> detect_wicks() -> wick_at_level() -> detect_volume_spike() -> detect_volume_divergence() -> find_best_entry() -> get_trade_levels() -> validate_trade() (src/tbb/pipeline.py, _prepare_candidate()).
8. If any early validation fails, it returns {"skipped": ...}; the result is returned immediately before engine bundle runs (src/tbb/pipeline.py, _prepare_candidate(); if "skip" in prepared -> return). This prevents heavy engine stack cost for dead windows.
9. If the candidate survives, analyze_pair_with_bias() evaluates the engine bundle (pattern_match, structure_retest_confirm, breakout_score, chart_pattern_align, fib_score, elliott_score, retracement_confirm, session_timing) and then calls analyze_pair() with all the engine summaries and the prepared candidate (src/tbb/pipeline.py, _compute_engine_bundle() and analyze_pair_with_bias() return block).
10. analyze_pair() computes score_result = calculate_confluence(...) and hard-rejects if score_result["score"] < min_confluence_score (default 50). If it passes, it logs to the DB when db_path is not None; else it returns a signal dict without logging (src/tbb/pipeline.py, analyze_pair(), score_result["score"] < min_confluence_score, db_path logic).
11. In live mode, db_path=None is used, so the pipeline returns a signal dict; the runner then calls collect_state() to snapshot topdown bias, regime, zones, and levels for the current tick, persists it with live_store.record_tick(), and opens a live trade with live_store.open_trade() if result had no skip (src/tbb/live/runner.py, collect_state() and run_analysis()).
12. Next, live runner resolves pending trades via resolve_open_live_trades() using trade_manager.advance() and writes management state; it then purges old records via live_store.purge_old() (src/tbb/live/runner.py, resolve_open_live_trades() and purge_old call).
13. In the skip path, result is stored as a live tick with skip_reason and skip_reason_raw, and no trade is opened (src/tbb/live/runner.py, collect_state() and record_tick(); src/tbb/storage/live_store.py, live_ticks schema and record_tick() INSERT).

---

## 3. Every gate, filter, and threshold that can block a signal

Table: gate condition and site.

| Gate / filter | File + function / lines | Exact condition | Exact numeric value | Configured where | Skip reason string / log |
|---|---|---|---|---|---|
| Top-down bias tradable gate | src/tbb/analysis/regime.py, resolve_topdown_bias(); src/tbb/bias_bridge.py, resolve_bias() | `tradable = aligned_count >= 1 and htf_bias != 'consolidation'` | `aligned_count >= 1`; `h tf_bias != 'consolidation'`; `direction` set only if topdown['bias'] is bullish/bearish | Hard-coded in resolve_topdown_bias() and used by bias bridge | `"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"` from pipeline if not bias["tradable"] |
| Regime filter disabled | src/tbb/analysis/regime.py, tradable() | `return True` | Always true | Hard-coded; no env override | Not an active skip; it neutralizes the regime gate |
| HTF alignment gate | src/tbb/analysis/regime.py, resolve_topdown_bias() | `aligned_count = sum(1 for b in biases if b == htf_bias and b != 'consolidation')`; `tradable` uses that count | threshold = 1 | Hard-coded; `tf_order` defaults to `['1W','1D','4H','1H','15M',...]` | `not tradable - regime=..., topdown={...}` |
| MSS conflict as veto | src/tbb/pipeline.py, analyze_pair_with_bias() | `conflicting = ((bias["direction"] == "LONG" and last_mss == "MSS_bear") or (bias["direction"] == "SHORT" and last_mss == "MSS_bull"))` | not a numeric threshold, but a boolean veto | derived from structure events | no direct skip string; it only disables `no_mss_conflict` input in confluence |
| News blackout | src/tbb/analysis/supplementary.py, is_news_blackout(); src/tbb/pipeline.py, analyze_pair_with_bias() | `if blackout["blackout"]: return {"skipped": f"news blackout: {blackout['events_today']}"}` | blackout if event is in `HIGH_IMPACT_EVENTS = {'NFP', 'FOMC', 'CPI'}` | `events_to_avoid` default `HIGH_IMPACT_EVENTS` | `"news blackout: ..."` |
| Candidate preparation gate: no qualifying entry | src/tbb/pipeline.py, _prepare_candidate(); src/tbb/engines/entry.py, find_best_entry() | `if entry_result is None` | `max_distance_pct=2.0`, `min_touches=2` | hard-coded in `find_best_entry()` | `"no qualifying entry level found"` |
| Candidate preparation gate: SL/TP failure | src/tbb/pipeline.py, _prepare_candidate(); src/tbb/engines/sl_tp.py, get_trade_levels() | `if trade is None` | `min_rr=2.0` in TP; `atr_mult=1.5` in SL; `fallback_rr=2.0` | hard-coded in `sl_tp.py` | `"sl/tp calculation failed"` |
| Validity gate: invalid trade | src/tbb/engines/validity.py, validate_trade(); src/tbb/pipeline.py, _prepare_candidate() | `if not check["valid"]: return {"skip": f"invalid trade: {check['reasons']}"}` | `min_rr=2.0`, `max_entry_distance_pct=2.0`, `max_sl_distance_pct=5.0`, `min_sl_distance_pct=0.1` | hard-coded in function signature | `"invalid trade: ..."` with reasons such as `R:R 2.0 below minimum 2.0` / `entry ... away, exceeds 2.0%` |
| Conviction threshold final gate | src/tbb/pipeline.py, analyze_pair(); src/tbb/engines/confluence.py, calculate_confluence() | `if score_result["score"] < min_confluence_score` | default `50` | `min_confluence_score=50` default in analyze_pair_with_bias and analyze_pair | `"low conviction: confluence {score_result['score']}/100 below required {min_confluence_score}"` |
| Engine prune bound gate | src/tbb/pipeline.py, _compute_engine_bundle() | `_bound() < threshold` triggers early return and pruned stubs | same threshold used as `min_confluence_score` | `PIPELINE_ENGINE_PRUNE` default enabled; `threshold=min_confluence_score` | stage label `"pruned_verdict_decided"` in engine block; not direct skip reason |
| Backtest concurrency / position gate | src/tbb/backtesting/backtest.py, run_symbol() | `if len(open_positions) >= MAX_CONCURRENT_POSITIONS` | `MAX_CONCURRENT_POSITIONS = 3` | constant in backtest.py | counted as `position_open`, not pipeline skip |
| Backtest level cooldown gate | src/tbb/backtesting/backtest.py, run_symbol() | `if pos < level_blocked_until.get(lk, -1)` | `LEVEL_COOLDOWN_BARS = 24` and `LEVEL_BAND_PCT = 0.0025` | constants in backtest.py | counted as `cooldown`, not pipeline skip |
| Backtest no-fill gate | src/tbb/backtesting/backtest.py, evaluate_outcome(); check_outcome() | `if fill_i is None: return "no_fill"` | `MAX_FILL_WAIT_BARS = 24`; `CHECK_FORWARD_BARS = 100` | constants in backtest.py | `no_fill` in report, no pipeline skip |
| Live trade entry fill timeout | src/tbb/live/trade_manager.py, advance(); src/tbb/live/runner.py, resolve_open_live_trades() | `if fill_i is None: outcome = "no_fill" if len(fwd) >= max_fill_wait else "pending"` | `max_fill_wait=24` | `MAX_FILL_WAIT_BARS` imported from backtest.py | `no_fill` on persisted trade; skip is not a signal skip |
| Stop/TP validity and risk rule | src/tbb/engines/sl_tp.py, calculate_tp(); src/tbb/engines/validity.py, validate_trade() | `if rr < min_rr` and stop price must be on the correct side of entry | `min_rr=2.0`; `max_entry_distance_pct=2.0`; `max_sl_distance_pct=5.0`; `min_sl_distance_pct=0.1` | hard-coded in function defaults | `invalid trade: ...` / `R:R ... below minimum 2.0` |
| S/R level entry filter | src/tbb/engines/entry.py, find_best_entry() | `if lvl["touches"] < min_touches` and `abs(current_price - lvl["price"]) / current_price * 100 <= max_distance_pct` | `min_touches=2`; `max_distance_pct=2.0` | in function defaults | `no qualifying entry level found` |
| ATR / volatility floor | src/tbb/engines/sl_tp.py, calculate_sl(); src/tbb/indicators/zigzag.py, get_zigzag_swings() | `atr_distance = atr * atr_mult`; `threshold = atr[i] * atr_mult` | `atr_mult=1.5`; ATR period 14 | hard-coded in function defaults | no direct skip reason; it shapes stop and swing detection; if invalid trade, not passing validity will skip |
| Session timing is weighted only | src/tbb/analysis/supplementary.py, session_quality(); src/tbb/pipeline.py, _compute_engine_bundle() | `session_info = session_quality(exec_df.index[-1])` then scored, not used as a hard gate | `SESSION_QUALITY_CRYPTO` and `OFF_SESSION_QUALITY = 45` | in supplementary.py | no skip reason; weighted only |
| Spread/fee model exists but is not enforced | src/tbb/analysis/supplementary.py, crypto_cost_model() | cost model is defined but never called by pipeline or backtest | fees default `taker_fee_pct=0.0006`, `maker_fee_pct=0.0002`, `bid_ask_spread` none | no runtime use | no skip reason in active path |

Additional explicit conditions from the code:

- src/tbb/pipeline.py, analyze_pair_with_bias(): `if not bias["tradable"] or bias["direction"] is None: return {"skipped": f"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"}`.
- src/tbb/analysis/supplementary.py: `HIGH_IMPACT_EVENTS = {'NFP', 'FOMC', 'CPI'}`; `is_news_blackout()` returns blackout when an event is found for the day.
- src/tbb/engines/pattern_strategy.py: `momentum_candle()` requires `(body / rng) >= min_body_ratio` and `clearance >= min_clearance_ratio`, with default `min_body_ratio=0.5` and `min_clearance_ratio=0.5`.
- src/tbb/engines/retracement.py: `exhaustion_check()` sets `stretched = consecutive >= max_consecutive or (extension_atr >= extension_atr_mult)`, with `max_consecutive=5` and `extension_atr_mult=3.0` but this is not a direct gate in the pipeline; it contributes to retracement scoring.

---

## 4. Scoring system: components, weights, formulas, max score, final threshold

Source: src/tbb/engines/confluence.py, WEIGHTS and calculate_confluence(); used by src/tbb/pipeline.py, analyze_pair().

The weighted score is:

| Component | Weight | Type | How each is computed | Max contribution |
|---|---:|---|---|---:|
| pattern_match | 9 | bool | `pattern_name is not None` in analyze_pair_with_bias(); bundle sets `pattern_name = agreeing[0]` when TA-Lib pattern matches | 9 |
| trend_align | 9 | bool | `bias["trend_aligned"]` from resolve_bias() / topdown bias | 9 |
| mtf_alignment | 9 | bool | `bias["mtf_full_alignment"]` from resolve_bias() | 9 |
| sr_level_strength | 7 | scalar | `ctx["level_touches"]` capped at 6; `min(val, 6) / 6 * weight` | 7 |
| wick_rejection | 7 | bool | `ctx["wick_at_entry"]` from `wick_at_level()` and last-two-bars check | 7 |
| fib_score | 5 | 0-100 scaled | `compute_fib_score(fib_raw)` from fibonacci result; weight * val/100 | 5 |
| volume_confirm | 4 | bool | `ctx["volume_confirm"]` from last-two-bars volume spike check | 4 |
| structure_bos_align | 10 | bool | `structure_bos_align` computed from latest BOS event matching direction | 10 |
| liquidity_target | 4 | bool | `liquidity_target` from major/low_hanging liquidity pool in the correct direction | 4 |
| no_mss_conflict | 4 | bool | `no_mss_conflict` derived from last MSS event | 4 |
| chart_pattern_align | 6 | bool | `chart_pattern_info["trade_ready"]` | 6 |
| retracement_confirm | 4 | bool | `retracement_info["trade_ready"]` | 4 |
| breakout_score | 6 | 0-100 scaled | `breakout_info["score"]` | 6 |
| elliott_score | 5 | 0-100 scaled | `elliott_info["score"]` | 5 |
| structure_retest_confirm | 6 | bool | `structure_retest_info["trade_ready"]` | 6 |
| session_timing | 5 | 0-100 scaled | `session_quality(exec_df.index[-1])['score']` | 5 |

Total max score = 100.

The exact formula in calculate_confluence():

- bool keys: `points = weight if val else 0`.
- `sr_level_strength`: `points = min(val, 6) / 6 * weight if val else 0`.
- scaled keys: `frac = max(0.0, min(float(val), 100.0)) / 100.0; points = weight * frac`.
- total = sum(points) with breakdown for each key.

Final threshold: default `min_confluence_score=50` in `analyze_pair_with_bias()` and `analyze_pair()`. If the score is lower, it returns: `"low conviction: confluence {score}/100 below required {min_confluence_score}"` (src/tbb/pipeline.py, analyze_pair()).

Confidence labels:

- HIGH >= 75
- MEDIUM >= 50
- LOW otherwise

Source: src/tbb/engines/confluence.py, confidence_label().

Potentially correlated components (same underlying data source):

- `trend_align` and `mtf_alignment` both derive from `resolve_bias()` and EMA/topdown bias output; they are not independent.
- `structure_bos_align`, `structure_retest_confirm`, `no_mss_conflict`, and `liquidity_target` all depend on the same structure/liquidity event stream from `classify_structure()` / `classify_liquidity()` and BOS events from `bias["structure"]`.
- `sr_level_strength`, `wick_rejection`, `liquidity_target`, and `find_best_entry()` reuse the same S/R and swing data from `get_zigzag_swings()` and `find_sr_levels()`.
- `chart_pattern_align`, `pattern_match`, `structure_retest_confirm`, and breakout/elliott/fib entries all sit on top of the same execution window and can be correlated because they are all driven by a short, recent OHLCV slice.

---

## 5. Multi-timeframe logic

The active framing is:

- Bias/timeframes: 1D, 4H, 1H (BIAS_TFS = ("1D", "4H", "1H")) in src/tbb/live/runner.py and PREDICT_BIAS_TFS in src/tbb/api/common.py.
- Trigger/execution timeframes: 1H and 15M in live processing (`TRIGGER_TFS = {"1H", "15M"}` in src/tbb/live/runner.py).
- Backtest path: `load_data(symbol)` reads 1D + 4H + execution timeframe; execution TF default is 1H, but lowers to 15M/30M when local CSV exists (src/tbb/backtesting/backtest.py, load_data()).

How they are combined:

- `analyze_pair_with_bias()` receives `df_by_tf` as the set of HTF frames and passes the last frame in `df_by_tf` to pattern detection (`exec_df = list(df_by_tf.values())[-1]`) in the pipeline (src/tbb/pipeline.py, analyze_pair_with_bias()).
- In live runner, the bias set is intentionally only 1D/4H/1H, while the execution OHLCV arrays are pulled from the currently triggered TF (1H or 15M), not from the HTF frames (src/tbb/live/runner.py, run_analysis()).
- The actual directional bias is resolved in `resolve_bias(df_by_tf)` via `per_tf_bias(df_by_tf)` and `resolve_topdown_bias()`: the first available TF in `TF_LADDER` wins conflicts, and `aligned_count` tracks how many TFs agree with that winner (src/tbb/analysis/regime.py, resolve_topdown_bias()).
- `trend_aligned` is set to `topdown['aligned_count'] >= 2` and `mtf_full_alignment` is set to `topdown['aligned_count'] == len(df_by_tf)` in `resolve_bias()` (src/tbb/bias_bridge.py, resolve_bias()).
- `tradable` requires `aligned_count >= 1` and `htf_bias != 'consolidation'` in `resolve_topdown_bias()`, which is weaker than the comment in the docstring: “want >=2 TFs aligned before trading.”

Trigger timing:

- Live evaluation runs only when a confirmed candle arrives on 1H or 15M, because `run_live_stream()` checks `if tf in TRIGGER_TFS` before `run_analysis()` (src/tbb/live/runner.py, run_live_stream()).
- There is no timer-based or periodic re-evaluation beyond this confirmed-bar trigger.
- A single new 1H or 15M bar can cause a full live analysis cycle and a new persistable tick.

Conflict resolution:

- HTF wins conflicts by taking the earliest TF in `TF_LADDER = ['1W', '1D', '4H', '1H', '15M', ...]` and using that as the winning bias if it exists. Other TFs are counted as aligned only if they match the same bias (src/tbb/analysis/regime.py, TF_LADDER and resolve_topdown_bias()).
- In the live pipeline, `trend_aligned` only requires two TFs aligned; `mtf_full_alignment` requires all frames in `df_by_tf` aligned.

Evaluation cadence:

- Live: on every confirmed 1H or 15M close (not on every price tick) because the stream filters `confirm` and then `if tf in TRIGGER_TFS`.
- Backtest: every `step` bar in `range(lo, hi, step)`; default `step=4` in `run_multi()`, and `points` are every 4th bar (src/tbb/backtesting/backtest.py, run_symbol()).

---

## 6. Candle handling: closed bars only? current-forming / future bars / look-ahead / intrabar assumptions

Evidence from live and backtest shows the signal path uses closed bars. The live websocket loop ignores non-confirmed candles: `if not candle.get("confirm"): continue` (src/tbb/live/runner.py, run_live_stream()). The signal arrays are built from `recent = df_by_tf[execution_tf].tail(200)` and then `opens/highs/lows/closes/volumes` from the closed bar history (src/tbb/live/runner.py, run_analysis()).

Confirmed findings:

- No current-forming candle is used for the signal decision. `confirm` is required from the stream. The trigger is the close of the 1H/15M bar already confirmed by Bybit.
- No future bars are used in the decision itself. `_prepare_candidate()` and all engine functions operate on arrays passed from the closed history only; the backtest slices historical data up to and including `ts` with `hist_exec = df_exec.iloc[:pos + 1]` and `slice_up_to(df, ts)` (src/tbb/backtesting/backtest.py, slice_up_to() and run_symbol()).
- Entry/SL/TP do not assume the signal bar high/low continues; they are evaluated against future bars only after the signal bar. `check_outcome()` loops `for i in range(signal_idx_pos + 1, fill_end)` to find a limit fill; it does not check the same signal bar (`signal_idx_pos` is excluded). If the fill does happen on the next bar or within `MAX_FILL_WAIT_BARS`, that is still from a closed bar after the signal bar (src/tbb/backtesting/backtest.py, check_outcome()).
- The text in backtest is explicit: “Phase 1 - fill: entry is a LIMIT level from find_best_entry, not the signal bar's close” and “The fill bar itself can carry price on to SL or TP after the entry is touched.” The file also states “no lookahead” at the top of `check_outcome()` and `slice_up_to()` (src/tbb/backtesting/backtest.py, check_outcome() and slice_up_to()).
- Live trade manager `advance()` similarly ignores the trade’s open bar when checking entry and resolves from the next bar onward with `fwd_mask = df.index > pd.Timestamp(trade["opened_at"])` (src/tbb/live/trade_manager.py, advance()).
- `momentum_candle()` reads `opens[-1], highs[-1], lows[-1], closes[-1]` from the most recent bar only; it does not use partial intrabar values (src/tbb/engines/pattern_strategy.py, momentum_candle()).
- `detect_break_and_retest()` uses past bars only and a `lookback` parameter; it does not inspect current intrabar price (src/tbb/engines/pattern_strategy.py, detect_break_and_retest()).

Look-ahead risk notes:

- The code intentionally avoids look-ahead in the backtest outcome loop. The main risk is not from future bars in the decision, but from using the same final close in `analyze_pair_with_bias()` for candidate prep and engine scoring while the trade plan may already be derived from that same bar and then filled on subsequent bars. That is not “future” price leakage, but it is a same-bar decision using a closed bar whose close is known in the real-time sequence.
- `analyze_pair_with_bias()` memoizes by `(last bar timestamp, row count)` and not by the entire arrays; this is safe for a closed-bar pipeline but not a live intrabar pipeline (src/tbb/pipeline.py, memoization functions `_frame_key()` and `_arrays_key()`).

---

## 7. Indicator warm-up: bars requested per timeframe vs bars each indicator and TA-Lib call needs

The main in-memory seed request is in live runner:

- 1D: 180 days
- 4H: 60 days
- 1H: 30 days
- 15M: 7 days

This is configured in `SEED_DAYS` in src/tbb/live/runner.py.

Other requested minimums and history windows:

- `LOOKBACK_BARS = 200` in src/tbb/backtesting/backtest.py.
- `PREDICT_EXEC_BARS = 300` and `PREDICT_MIN_BIAS_BARS = 50` in src/tbb/api/common.py.
- `STATE_WINDOW_BARS = 200` in src/tbb/live/runner.py for overlay snapshot calculations.

Indicator warm-up and silent-empty behavior:

- `zigzag.get_zigzag_swings()` returns [] when `len(closes) < atr_period + 2`; it will also produce no swings if ATR is insufficient or threshold is not met (src/tbb/indicators/zigzag.py, get_zigzag_swings()).
- `talib.ATR()` is called in `_prepare_candidate()` with `timeperiod=14` and in `get_zigzag_swings()` with `atr_period=14`; during the first ~14 bars the ATR array may contain NaN values and the code does not explicitly prune them before use (src/tbb/pipeline.py, _prepare_candidate(); src/tbb/indicators/zigzag.py, get_zigzag_swings()).
- `ema_trend_filter()` uses `ewm(span=50, adjust=False).mean()` and `flat_epsilon = ema.std() * 0.01` to set the trend slope. It will not fail on short history, but it may produce weak or noisy bias until the EMA stabilizes; the code uses the current bar’s bias anyway (src/tbb/analysis/primitives.py, ema_trend_filter()).
- `find_best_entry()` requires at least 2 touches and a level within 2% of price; if those conditions are not met it returns None without an exception, which is a silent early rejection (src/tbb/engines/entry.py, find_best_entry()).
- `get_trade_levels()` returns None if `calculate_tp` fails or if `trade` is invalid; the pipeline converts that into `"sl/tp calculation failed"` (src/tbb/engines/sl_tp.py; src/tbb/pipeline.py, _prepare_candidate()).
- `session_quality()` will always return a score because it is computed from clock time; `session` can be “off_session” with 45, but this is a weighted input, not a gate (src/tbb/analysis/supplementary.py, session_quality()).
- `pattern_detector.get_active_patterns(exec_df)` can be empty or weak when an execution window is not long enough; the pipeline treats that as `pattern_name is None` and not an exception (src/tbb/pipeline.py, _compute_engine_bundle() and get_active_patterns not shown, but used there).

---

## 8. Live vs backtest parity: differences in logic, parameters, data, fills, fees, and slippage

live vs backtest differences:

1. Data source:
   - live: Bybit WebSocket and REST-seeded memory; no disk CSV writes (src/tbb/live/runner.py, module docstring and seed_history()).
   - backtest: local CSVs loaded from `data/{symbol}_{tf}.csv` (`src/tbb/backtesting/backtest.py, load_data()` and `has_execution_csv()`).

2. Signal logging target:
   - live: `db_path=None` in `analyze_pair_with_bias()`; the runner persists tick + trade state in `live_state.db` via `live_store.record_tick()` and `open_trade()`, not `signals.db` (src/tbb/live/runner.py, run_analysis()).
   - backtest: `db_path=BACKTEST_DB` and logs to `BACKTEST_LOG_DB` in `paths.py`/`backtest.py` (src/tbb/backtesting/backtest.py, BACKTEST_DB constant). It also records trades in `TradeJournal`, not live_store.

3. Top-down data passed to the pipeline:
   - live: `df_by_tf = {"1D":{"..."}, "4H":{"..."}, "1H":{"..."}}` exactly; execution OHLCV arrays are a tail slice from the current trigger TF (src/tbb/live/runner.py, run_analysis()).
   - backtest: `df_by_tf={"1D": hist_1d, "4H": hist_4h, "1H": hist_exec}` while the `opens/highs/lows/...` arrays are taken from the most recent `LOOKBACK_BARS` of `hist_exec` (src/tbb/backtesting/backtest.py, run_symbol()).

4. Runtime fill management:
   - live: `trade_manager.advance()` is called on every pending/open trade on each tick; TP1 / breakeven / chandelier trail logic is active (src/tbb/live/trade_manager.py, advance()).
   - backtest: default `MANAGED_EXITS = os.environ.get("BACKTEST_MANAGED_EXITS", "1") != "0"`; if enabled, it uses `check_outcome_managed()` -> `trade_manager.advance()` exactly like live. If disabled, it uses flat SL/TP resolution (`check_outcome()`) (src/tbb/backtesting/backtest.py, MANAGED_EXITS, evaluate_outcome()).

5. Fill waiting and forward horizon scaling:
   - live: `MAX_FILL_WAIT_BARS` is 24 and is used directly (`MAX_FILL_WAIT_BARS` imported from backtest.py) (src/tbb/live/runner.py, resolve_open_live_trades(); src/tbb/backtesting/backtest.py, top constants).
   - backtest: `fill_wait = int(round(MAX_FILL_WAIT_BARS * scale))` and `forward = int(round(CHECK_FORWARD_BARS * scale))` for lower TFs, preserving wall-clock horizons while adjusting to 15M/30M (src/tbb/backtesting/backtest.py, run_symbol()).

6. Constraint differences:
   - live does not enforce `MAX_CONCURRENT_POSITIONS`, `LEVEL_COOLDOWN_BARS`, or `zone_occupied` portfolio caps (src/tbb/backtesting/backtest.py, run_symbol(); no equivalent in live runner). The live runner resolves pending trades but does not exclude additional simultaneous positions by symbol or level.
   - backtest records `cooldown`, `zone_occupied`, and `position_open` counts in its funnel counters; live does not compute or expose these as a gate (src/tbb/backtesting/backtest.py, _blank_counters(); run_symbol()).

7. Fees and slippage:
   - Neither live nor backtest applies spread, slippage, or broker fee to entry/exit price in signal generation. The only fee/spread model in the code is `crypto_cost_model()` and `forex_pip_model()`, but the pipeline never calls them (src/tbb/analysis/supplementary.py, crypto_cost_model(), forex_pip_model()).
   - `live_store.live_stats()` computes `pnl` as price delta times R-multiple, not account-dollar PnL; it is not position-size-weighted or spread-adjusted (src/tbb/storage/live_store.py, live_stats()).

8. Timeframe parity:
   - live runs at trigger TFs 1H/15M; backtest can also evaluate 15M or 30M if the CSV exists. Most pipeline logic is common, but the exact execution bar set differs based on the actual data source.

---

## 9. Data and storage: live_state.db tables, retention, memory-only vs persisted, /live/skips map, /live/stats actual outputs

Database schema in src/tbb/storage/live_store.py, init_db():

- `live_ticks` columns: `id`, `ts`, `recorded_at`, `symbol`, `execution_tf`, `topdown_bias`, `aligned_count`, `tradable`, `direction`, `skip_reason`, `skip_reason_raw`, `signal_json`.
- `live_tf_state` columns: `id`, `tick_id`, `ts`, `symbol`, `timeframe`, `bias`, `regime`, `is_consolidation`.
- `live_zones` columns: `id`, `tick_id`, `ts`, `symbol`, `timeframe`, `kind`, `direction`, `price_low`, `price_high`, `start_ts`, `end_ts`, `tested`.
- `live_levels` columns: `id`, `tick_id`, `ts`, `symbol`, `timeframe`, `price`, `touches`, `level_type`.
- `live_trades` columns: `id`, `tick_id`, `symbol`, `timeframe`, `direction`, `opened_at`, `filled_at`, `resolved_at`, `entry_price`, `stop_price`, `take_profit`, `stop_distance`, `planned_rr`, `realized_rr`, `outcome`, `pnl`, `confluence`, `notes`, plus managed-trade fields added by `_ensure_trade_columns()` (`tp1_filled`, `tp1_price`, `stop_current`, `exit_price`).
- There is no dedicated `live_skips` table. `skip_reason` and `skip_reason_raw` live on `live_ticks` and are exposed via /live/skips and /live/state queries (src/tbb/storage/live_store.py, init_db() and recent_skips()).

Retention rules:

- `LIVE_RETENTION_HOURS = float(os.environ.get("LIVE_RETENTION_HOURS", 6))` in src/tbb/storage/live_store.py.
- `purge_old()` deletes tick records older than the cutoff based on `recorded_at`, but keeps the newest tick per symbol. It also deletes child rows from `live_tf_state`, `live_zones`, and `live_levels`, and nulls `tick_id` in `live_trades` when their tick is deleted. `live_trades` themselves are never purged (src/tbb/storage/live_store.py, purge_old()).

Memory-only vs persisted:

- Live runner explicitly states: “NOTHING IN THIS PROCESS TOUCHES THE DISK except live_state.db” and “seed history is fetched straight into in-memory DataFrames; no CSV is ever written or read here” (src/tbb/live/runner.py module docstring; seed_history()).
- The scan path also uses in-memory data and only writes to live_state.db via the same live_store layer (src/tbb/live/scanner.py, module docstring).

Endpoint payloads:

- `/live/skips` returns `{symbol, count, skips, counts}` where `skips` is `recent_skips(symbol, limit=limit)` and `counts` is `skip_reason_counts(symbol)`; `recent_skips()` selects `ts, execution_tf, skip_reason, skip_reason_raw FROM live_ticks WHERE symbol=? AND skip_reason IS NOT NULL ORDER BY id DESC LIMIT ?` (src/tbb/api/routes_live.py, live_skips(); src/tbb/storage/live_store.py, recent_skips(), skip_reason_counts()).
- `/live/stats` returns `source: "live"`, then `symbol`, `total_trades`, `in_flight_trades`, `win_rate`, `avg_rr`, `profit_factor_R`, `wins`, `losses`, `trades_today`, `today_pnl`, and a list of recent resolved trades with their fields (`src/tbb/api/routes_live.py, live_stats(); src/tbb/storage/live_store.py, live_stats(), resolved_trades()`).
- `profit_factor_R` is computed as `gross_win / gross_loss` on realized R-multiples, not on price deltas; this is explicitly documented in live_store.live_stats() and backtest.rr_profit_factor() (src/tbb/storage/live_store.py, live_stats(); src/tbb/backtesting/backtest.py, rr_profit_factor()).

---

## 10. Symbols: how loaded, validated, and processed per symbol; crypto/Bybit/USDT hardcoding and forex breakage

Symbol loading:

- `scripts/run_live.py` accepts `--symbol`, `--symbols`, or `TBB_SYMBOL`/`LIVE_SYMBOL` environment variables. It uppercases and strips comma-separated values in `_symbol_list()` (src/tbb/live/runner.py, _symbol_list(), _cli_symbols()).
- `src/tbb/live/scanner.py` uses `get_symbols(quote_coin=QUOTE_COIN)` and fetches `fetch_instruments(category="spot")`, then filters for `status == "Trading"` and `quote_coin == quote_coin` (default `USDT`) (`src/tbb/live/scanner.py, get_symbols()`).
- The API symbol validator is `SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,20}$")` in `src/tbb/api/common.py`, then `_clean_symbol()` uppercases the symbol; it does not validate that the symbol is a supported base/quote pair beyond the regex.
- Market data endpoints also filter through Bybit `category="spot"` and `fetch_instruments()` (src/tbb/marketdata/market_data.py, `CATEGORY = os.environ.get("BYBIT_CATEGORY", "spot")`; src/tbb/marketdata/ingestion_bybit.py, fetch_instruments()).

Hardcoded crypto/Bybit/USDT assumptions:

- `STREAM_URL = "wss://stream.bybit.com/v5/public/spot"` in src/tbb/live/runner.py; this is spot-only and fully Bybit-specific.
- `TF_STREAM_CODE = {"1D": "D", "4H": "240", "1H": "60", "15M": "15"}` is also built around Bybit interval codes; these do not generalize to forex brokers or CFTC-style sessions.
- `SCANNER_QUOTE = os.environ.get("SCANNER_QUOTE", "USDT").upper()` in src/tbb/live/scanner.py means the scanner defaults to USDT quote coins only.
- `market_data.CATEGORY = os.environ.get("BYBIT_CATEGORY", "spot")` in market_data.py and `fetch_instruments(category="spot")` mean the data path is cryptocurrency-spot default.
- `analysis/supplementary.py` contains `PAIR_SESSION_MAP` for forex (e.g., EUR, GBP, CHF, AUD, NZD, JPY) and `is_favorable_session(pair, ny_time, is_crypto=False)` is designed to handle forex sessions; however, the main pipeline does not call it for signal gating, so the code is not active in the live or backtest path (src/tbb/analysis/supplementary.py, is_favorable_session() and session_quality()).
- The direct signal route is therefore crypto-spot and USDT-quote oriented by default. It would not work unchanged for non-spot/USDT symbols or a non-Bybit source without code changes.

---

## 11. Why signals are rare: top blockers ranked by likely impact

Ranked by likely impact from code logic and without running the system.

1. Confluence threshold + score composition (very high impact)
   - Code: `if score_result["score"] < min_confluence_score` in src/tbb/pipeline.py, analyze_pair(); default `min_confluence_score=50` in `analyze_pair_with_bias()` and `analyze_pair()`.
   - Condition: final score must exceed 50/100 and there are many weighted components. Because the system includes pattern, trend, MTF, SR strength, fib, breakout, elliott, structure retest, session timing, etc., several weak or missing components will keep a bar below 50 unless multiple factors align.
   - Estimated frequency: common. This is the final hard gate in the pipeline and is likely hit often when a single setup lacks several engine confirmations.
   - Hard AND-gate: yes. The signal must survive candidate prep, pass the bias gate, and then also reach `>= 50`.

2. Candidate prep: entry level + valid trade config (very high impact)
   - Code: `entry_result = find_best_entry(current_price, direction, sr_levels); if entry_result is None: return {"skip": "no qualifying entry level found"}` and `check = validate_trade(trade, current_price)`; `if not check["valid"]: return {"skip": f"invalid trade: {check['reasons']}"}` in `_prepare_candidate()`.
   - Condition: valid directional S/R level within 2% and requiring at least two touches; keyed to the trade direction; then `RR >= 2.0`, `entry_dist_pct <= 2.0`, `sl_dist_pct <= 5.0` and `>= 0.1%`.
   - Estimated frequency: high. A valid, directional, within-range, high-touch level plus a valid SL/TP profile is much narrower than all price bars.
   - Hard AND-gate: yes. This is a classic multi-condition hard gate on the same bar.

3. Bias/tradable gate (high impact)
   - Code: `if not bias["tradable"] or bias["direction"] is None: return {"skipped": f"not tradable - regime={bias['regime']}, topdown={bias['topdown']}"}` in src/tbb/pipeline.py, analyze_pair_with_bias(); `resolve_topdown_bias()` uses `aligned_count >= 1 and htf_bias != 'consolidation'` in src/tbb/analysis/regime.py.
   - Condition: no valid top-down bias or the bias is consolidation or not aligned with the higher-timeframe winner.
   - Estimated frequency: moderate to high. It removes a large number of setups when HTF logic is mixed, but the requirement is only one TF aligned, so it is not as restrictive as “two aligned” in the comments.
   - Hard AND-gate: yes, with the execution candidate path and score gate.

4. Entry SR filter itself (high impact)
   - Code: `find_best_entry()` only accepts `lvl["touches"] >= 2`, `direction` matching `SUPPORT`/`RESISTANCE`, and within 2% of current price (src/tbb/engines/entry.py, find_best_entry()).
   - Condition: there must be an S/R level in the correct side of the market and close enough to current price.
   - Estimated frequency: high, because price often sits away from a touched level or in a low-touch region.

5. Structural/liquidity mismatch and BOS/MSS data gating (moderate impact)
   - Code: `if bias.get("structure") ... last_bos ... structure_bos_align = ...` and `liquidity_target = ...` and `no_mss_conflict` in src/tbb/pipeline.py, analyze_pair_with_bias().
   - Condition: the trend must align with recent BOS events and there must be a relevant liquidity target without a conflicting MSS event.
   - Estimated frequency: moderate, because it depends on recent BOS and liquidity events being present and directional.

6. Backtest-only portfolio and cooldown logic (backtest-specific, not live) (high in backtest)
   - Code: `if len(open_positions) >= MAX_CONCURRENT_POSITIONS` and `if pos < level_blocked_until.get(lk, -1)` in src/tbb/backtesting/backtest.py, run_symbol(); constants `MAX_CONCURRENT_POSITIONS = 3`, `LEVEL_COOLDOWN_BARS = 24`.
   - Condition: no free slot and same level is blocked for 24 bars.
   - Estimated frequency: moderate, especially in a high-signal environment, but it only affects backtesting, not the live pipeline.

7. No-fill / fill wait time (moderate impact in both live and backtest)
   - Code: `if fill_i is None: return "no_fill"` in `check_outcome()` and `if fill_i is None: outcome = "no_fill" if len(fwd) >= max_fill_wait else "pending"` in `trade_manager.advance()` with `MAX_FILL_WAIT_BARS = 24`.
   - Condition: price never reaches the limit entry inside 24 bars.
   - Estimated frequency: moderate to high depending on market noise; this is a realistic source of “rare signals” because a valid setup can still be a no-fill.

8. Regime filter disabled (not a blocker in current code)
   - Code: `def tradable(regime: str) -> bool: return True` in src/tbb/analysis/regime.py.
   - Effect: the regime hash is not currently active as a blocker. This is a defect, not a blocker; it weakens the signal filter rather than making it rare.

9. News blackout is not active unless a calendar is passed (low impact in current code)
   - Code: `if news_calendar is not None` in src/tbb/pipeline.py, analyze_pair_with_bias(); the live path does not pass a non-None calendar.
   - Condition: only active if the caller supplies a date-structured event calendar.
   - Estimated frequency: effectively zero in current runtime unless explicitly configured.

Hard AND-gates requiring several conditions at one bar:

- `analyze_pair_with_bias()` hard gate: `bias['tradable']` true + `bias['direction']` not None + `prepared` candidate passes + `score >= min_confluence_score`.
- `_prepare_candidate()` hard gate: `find_best_entry()` not None + `get_trade_levels()` not None + `validate_trade()` valid == true.
- `check_outcome()` after a signal: no-fill or fill plus outcome resolution; not a signal gate but a trade-production gate.

---

## 12. Defects and risks: bugs, dead code, unused config, duplicated logic, silent exceptions, swallowed errors, unclear naming

1. `tradable()` is always `True` in src/tbb/analysis/regime.py.
   - This nullifies the regime gate despite the code and comments implying regime-driven selection should be active.
   - It is a direct functional defect because the pipeline is supposed to reject non-tradable regimes at the top level.

2. The regime “consolidation” logic in `detect_regime()` is suspiciously coded and likely wrong.
   - The local variable `fails` is computed per-iteration and then the later `if len(fails) >= 3` executes outside the loop in the shown code; this leaves only the last iteration’s results in scope. The comment says “find 2 consecutive fails in opposing directions,” but the implementation only checks the last pass of `outcomes` (src/tbb/analysis/regime.py, detect_regime()).

3. `resolve_topdown_bias()` documentation says “want >=2 TFs aligned before trading,” but the actual requirement is `aligned_count >= 1` and `h tf_bias != 'consolidation'` (src/tbb/analysis/regime.py, resolve_topdown_bias()).
   - This is a semantic mismatch between comments and runtime logic.

4. `news_calendar` is an optional parameter in the pipeline, but the live path never passes it; the default `None` means the news blackout gate is inactive unless the caller explicitly provides a calendar (src/tbb/pipeline.py, analyze_pair_with_bias()).
   - This is not necessarily a bug, but it is an unexercised gate, which should be documented if production behavior relies on it.

5. `session_quality()` is not a gate but it is mathematically used as a confluence input; the naming is “session quality,” but there is no actual session killzone gate in the live/backtest path (src/tbb/analysis/supplementary.py, session_quality(); src/tbb/pipeline.py, _compute_engine_bundle()).
   - This means the session logic is mostly ornamental in the current pipeline.

6. `crypto_cost_model()` and `forex_pip_model()` exist in supplementary.py but are not used by live/backtest/pipeline; they are dead-weight code and create the appearance of real cost/fee gating without actually enforcing it (src/tbb/analysis/supplementary.py, crypto_cost_model(), forex_pip_model()).

7. The code is very noisy about “hard gates” in engine modules (`trade_ready` in `pattern_strategy`, `retracement`, `elliott_wave`, `breakout_engine`), but the actual final signal gate is still the weighted confluence threshold and the pre-candidate validity checks. Some `trade_ready` flags are used as weighted inputs, not hard vetoes (src/tbb/engines/*.py; src/tbb/pipeline.py, analyze_pair()).
   - This creates naming ambiguity and weakens reproducibility.

8. There is no dedicated “skip reason” table; the skip classification is stored as a text field on `live_ticks`, so analyses that want to count not-tradable reasons must read and classify string values rather than a normalized table (src/tbb/storage/live_store.py, live_ticks schema; src/tbb/backtesting/backtest.py, classify_skip()).

9. Some comments describe “hard gates” and “trade_ready” in modules, but the actual implementation often turns them into weighted inputs or partial checks. This blurs the difference between a gate and a scoring signal (see `pattern_strategy.py`, `retracement.py`, `elliott_wave.py`, `breakout_engine.py`, `confluence.py`).

10. The live runner imports `MAX_FILL_WAIT_BARS` from backtest.py; this is a cross-layer coupling that allows live logic to depend on backtest constants. This is not necessarily wrong, but it does make logic harder to reason about and makes a constant in the backtest module effectively part of the live runtime contract (src/tbb/live/runner.py, import line; src/tbb/backtesting/backtest.py top constants).

11. `scanner.py` and `runner.py` both rely on in-memory state and use the same live_store table for persistence, so multi-symbol scan output can overwrite or interleave the same live state data. The design is intentionally a hand-off buffer and not an archive, but it is easy to mistake it for a true multi-symbol analysis ledger (src/tbb/live/runner.py, `collect_state()` and `live_store.record_tick()`; src/tbb/live/scanner.py, `scan_once()`).

12. `trade_manager.advance()` uses `fill_i = None` and `outcome = "no_fill" if len(fwd) >= max_fill_wait else "pending"`; if `len(fwd)` is exactly the max wait window, a trade is marked no_fill without a chance to continue. This is explicit, but it makes live trade resolution sensitive to the exact bar count and may undercount partially-filled trades (src/tbb/live/trade_manager.py, advance()).

13. `current_price` and `entry` distance checks assume percent distance can be computed in a stable way over all assets; for low-price coins or very different symbols, the scale is not normalized by volatility or market structure. The pipeline treats `2%` gate as universal, but the comment on forex cost model suggests pair-type differences were anticipated but never applied in the live pipeline (src/tbb/engines/entry.py, find_best_entry(); src/tbb/analysis/supplementary.py, forex_pip_model()).

---

## 13. Open questions

1. Which production runtime actually uses the code today: live runner, scanner, API, or a custom orchestrator? The repo does not contain a production deployment manifest showing which mode is active.
2. Are there real market calendars or news blackouts passed into the pipeline in production, or is the gate dead code in practice? The code path is present, but the call site is not visible here.
3. Do the local CSVs and Bybit data match the same symbol/timeframe cadence in production? The backtest and live paths assume same data conventions, but the actual stored history in the runtime environment is not here.
4. How much of the unusual “rare signals” is due to market regime, and how much is due to the current thresholds? The code alone does not tell us the empirical distribution of historical signals by symbol/timeframe.
5. Is the intended meaning of `trade_ready` to be a hard gate or a weighted evidence flag? The code comments contradict the actual usage, and the runtime behavior is not clearly specified in one place.
6. Which of the engine modules are definitely active in live production and which are only research scaffolding? Some modules exist and are imported, but no runtime config file identifies the active feature set.
7. Are the 1H/15M trigger TFs in live runner the intended cadence for real-world use, or are they placeholders for a different execution schedule? The code sets them explicitly, but the data/market assumptions behind them are not visible here.
8. Is the “USDT spot only” design deliberate for this backend or a temporary limitation for a crypto-only bot? The code strongly suggests a crypto/Bybit/USDT bias, but the repo does not state the intended broader scope.
9. Do the historical backtest local CSVs match the same timezone and bar semantics as the live Bybit websocket stream? The code handles UTC conversion but does not prove the local CSVs are all time-normalized to the same basis.
10. Is the live API intended to show only the most recent tick per symbol or a multi-tick history with reasoning? The code stores a rolling buffer but the schema and API read behavior imply a snapshot model, not a complete archive.

---

## Section completion status

Completed: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13.
Partial: none.
