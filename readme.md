# Trading Bot Backend

Python backend for structural market analysis, live Bybit candle processing, historical backtests, and a read-only FastAPI surface. The repository currently has three deliberately separate data paths:

- **LIVE**: `live_runner.py` computes rolling live snapshots and writes `live_state.db`; `api.py` reads those snapshots.
- **ANALYSIS/backtest**: `backtest.py` replays local historical CSVs and writes analysis jobs/results to `analysis_runs.db` through `api.py`.
- **PREDICT**: `api.py` fetches live candles into memory, runs the pipeline once, and writes nothing.

All three use the same core `pipeline.py` logic where applicable, but they do not share data with one another. The live database is not an analysis archive, analysis jobs do not feed live state, and `/predict` has no storage side effect.

## How It Fits Together

### LIVE path

```text
live_runner.py -> pipeline.py -> live_state.db -> api.py (/live/*)
```

`live_runner.py` seeds and streams Bybit candles, computes live overlays/signals, and persists a rolling hand-off buffer through `live_store.py`. `api.py` exposes that buffer through `/live/state`, `/live/zones`, `/live/levels`, and `/live/stats`. Live processing does not read historical CSVs.

### ANALYSIS/backtest path

```text
backtest.py or POST /analyze -> pipeline.py -> historical data/*.csv
                                                   -> analysis_runs.db
```

`POST /analyze` queues a date-bounded backtest. `backtest.py` loads local `1D`, `4H`, and `1H` CSV history, calls the pipeline while replaying bars, and stores job/trade results through `analysis_store.py`. A backtest may also use the lower-level `backtest.py` API directly. This path does not read `live_state.db`.

### PREDICT path

```text
POST /predict -> live in-memory candle buffer -> pipeline.py
```

`/predict` fetches enough Bybit candles for the bias and execution timeframes, runs `analyze_pair_with_bias()` synchronously, passes `db_path=None`, and returns a signal or skip reason. It is stateless: it does not read or write `live_state.db`, `analysis_runs.db`, `signals.db`, or CSV files.

## Python File Inventory

Each row identifies the file's role, direct callers/importers, direct callees/dependencies, and verified storage effects. “None” means the file itself does not touch that storage type; a caller may still do so.

| File | Role | Called by | Calls / depends on | Storage touched |
|---|---|---|---|---|
| `analysis_store.py` | Persists queued analysis jobs, status, summaries, and completed trades. | `api.py` | `sqlite3`, `pandas`, JSON serialization. | Reads/writes `analysis_runs.db`; no `live_state.db` or CSV. |
| `api.py` | FastAPI HTTP surface for health, symbols, live state, charts, backtests, and prediction. | Uvicorn/HTTP clients. | `analysis_store.py`, `backtest.py`, `live_runner.py`, `live_store.py`, `market_data.py`, `phase4_risk_journal.py`, `pipeline.py`; pandas. | Reads local `data/*.csv`; reads/writes `analysis_runs.db` indirectly; reads `live_state.db` indirectly; `/predict` persists nothing. |
| `backtest.py` | Loads historical candles, replays bars, simulates SL/TP outcomes, and summarizes results. | `api.py`, `profile_pipeline.py`; `live_runner.py` imports constants. | `pipeline.py`, `phase4_risk_journal.py`, pandas, NumPy. | Reads `data/{symbol}_{1d,4h,1h}.csv`; pipeline logging uses `backtest_signals.db`; no `live_state.db` or `analysis_runs.db` directly. |
| `bias_bridge.py` | Adapts Phase 1-3 calculations into the pipeline's tradable direction/bias result. | `pipeline.py`. | `phase1_primitives.py`, `phase2_signal_engine.py`, `phase3_orchestration.py`. | None. |
| `breakout_engine.py` | Standalone key-level breakout engine: consolidation trendlines, shape labels, momentum-candle/volume/base-volatility/trend filters, entry/stop, TP1 + chandelier plan, and a 0-100 breakout quality score. | `pipeline.py`. | NumPy only; no project imports. | None. |
| `elliott_wave.py` | Elliott Wave rule validator + position-in-count locator: 5 unbreakable rules as hard gates, fib retracement/extension/equality/alternation scoring, RSI+MACD wave-5 divergence, recency guard, A-B-C correction context, and a 0-100 quality score. | `pipeline.py`. | `fibonacci.py` shared utilities (find_swings/calculate_rsi/calculate_ema), NumPy. | None. |
| `chart_patterns.py` | Detects classical reversal/continuation chart patterns (double/triple tops and bottoms, H&S, triangles, flags, pennants, cup & handle) from swing topology; exposes trigger/invalidation/measured-move per pattern. | `pattern_strategy.py`. | NumPy only; no project imports. | None. |
| `pattern_strategy.py` | Turns chart patterns into trade setups: momentum-candle break gate, break-and-retest state machine, first-pullback stop, base volatility contraction filter, two-step TP plan. | `pipeline.py`. | `chart_patterns.py`, NumPy. | None. |
| `retracement.py` | Fib-retracement pullback entries: HH/HL/LH/LL sequence labels, structure trend from swings, pullback-depth tracker, exhaustion heuristic, and a trade plan gated by fibonacci.py zone/candle/invalidation checks. | `pipeline.py`. | `fibonacci.py`, NumPy. | None. |
| `breakouts.py` | Detects support/resistance and consolidation breakouts. | `pipeline.py`. | Input S/R levels and consolidation zones; no imported project module. | None. |
| `confluence.py` | Calculates the weighted signal score and confidence label. | `pipeline.py`. | Input pattern, trend, MTF, S/R, wick, volume, chart-pattern, fib-retracement facts, and scaled 0-100 fib/breakout/elliott engine scores; no imported project module. | None. |
| `consolidation.py` | Finds tight consolidation zones from swing points. | `pipeline.py`, `live_runner.py`. | NumPy; swing data from callers. | None. |
| `entry.py` | Selects the strongest directional S/R entry level. | `pipeline.py`; `test.py`. | Input S/R levels; no imported project module. | None. |
| `ingestion.py` | Minimal standalone Bybit WebSocket candle listener/printer. | CLI only; no repository importer. | `asyncio`, `websockets`, JSON. | None. |
| `ingestion_analysis.py` | Standalone rolling WebSocket analysis with TA-Lib patterns, RSI, and ATR. | CLI only; no repository importer. | Bybit REST prefill, `websockets`, TA-Lib, NumPy. | None. |
| `ingestion_bybit.py` | Bybit REST kline/instrument fetcher and optional CSV exporter. | `live_runner.py`, `market_data.py`; CLI. | `requests`, pandas, filesystem. | `fetch_and_save_all()` writes CSVs; other functions do not touch `live_state.db` or `analysis_runs.db`. |
| `live_runner.py` | Seeds Bybit data, consumes the live WebSocket, runs live analysis, and writes snapshots/trades. | CLI process; `api.py` imports constants/helpers. | `ingestion_bybit.py`, `pipeline.py`, `live_store.py`, `backtest.py` constants, Phases 1-3, `consolidation.py`, `support_resistance.py`, `zigzag.py`, `websockets`. | Writes `live_state.db` through `live_store.py`; no CSV reads in the live seed/stream path and no `analysis_runs.db`. |
| `live_store.py` | Owns the rolling SQLite hand-off store for live snapshots, overlays, skips, and trades. | `live_runner.py`, `api.py`, `test_live_persistence.py`. | `sqlite3`, pandas, JSON, datetime. | Reads/writes `live_state.db`; purges old snapshots while retaining live trades; no analysis CSV path. |
| `market_data.py` | Caches Bybit candles, instrument symbols, and timeframe metadata for API charts. | `api.py`. | `ingestion_bybit.py`, pandas, threading/time. | In-memory caches only; no DB or CSV. |
| `multi_pair_scanner.py` | Standalone multi-pair pattern, Fibonacci, and trend scanner. | CLI only; optional import attempt from `multi_timeframe_analysis.py`. | `requests`, TA-Lib, NumPy. | None. |
| `multi_timeframe_analysis.py` | Standalone multi-timeframe pattern, trend, RSI, and Fibonacci analysis. | CLI only; optional import attempt from `multi_pair_scanner.py`. | `requests`, TA-Lib, NumPy. | None. |
| `pattern_detector.py` | Runs TA-Lib candlestick recognizers and summarizes active bullish/bearish patterns. | `pipeline.py`. | TA-Lib, pandas, NumPy. | None. |
| `pattern_stats.py` | Queries completed signal win rates by pattern, pair, and timeframe. | No production importer found; CLI/library use. | `signal_store.py` database path, `sqlite3`. | Reads `signals.db` by default; no `live_state.db`, `analysis_runs.db`, or CSV. |
| `phase1_primitives.py` | Computes candle primitives, fractal swings, and EMA trend/bias features. | `phase2_signal_engine.py`, `bias_bridge.py`, `phase5_structure_liquidity.py`, `live_runner.py`. | pandas, NumPy. | None. |
| `phase2_signal_engine.py` | Detects FVG/imbalances, double rejection, tested zones, and sweep/break behavior. | `phase3_orchestration.py`, `bias_bridge.py`, `live_runner.py`, `phase5_structure_liquidity.py`. | `phase1_primitives.py`, pandas, NumPy. | None. |
| `phase3_orchestration.py` | Classifies regime, resolves top-down bias, and supports VSSR/opposing-imbalance logic. | `bias_bridge.py`, `live_runner.py`; internal demo. | `phase2_signal_engine.py` and Phase 1 EMA logic. | None. |
| `phase4_risk_journal.py` | Provides in-memory risk management, position sizing, trade journaling, and statistics. | `backtest.py`, `api.py`; standalone demo. | pandas, dataclasses, datetime. | No direct DB/CSV access; its journal is in memory. |
| `phase5_structure_liquidity.py` | Detects market structure, BOS/TC, MSS-related structure, liquidity, and market phase. | No production importer; standalone demo. | `phase1_primitives.py`, `phase2_signal_engine.py`. | None. |
| `phase6_supplementary.py` | Provides break-even, leverage/liquidation, cost, MSS, news/session, and compounding helpers. | No production importer; standalone demo. | pandas, NumPy, datetime; caller-provided event data. | None. |
| `pipeline.py` | Shared core pipeline: bias, swings, S/R, zones, breakouts, wicks, volume, entry, SL/TP, validation, confluence (incl. scaled fib/breakout/elliott scores and a low-conviction gate defaulting to 50), chart-pattern setup and retracement analysis, and optional signal logging. | `api.py`, `backtest.py`, `live_runner.py`, `profile_pipeline.py`. | `zigzag.py`, `support_resistance.py`, `consolidation.py`, `breakouts.py`, `wicks.py`, `entry.py`, `sl_tp.py`, `validity.py`, `confluence.py`, `volume.py`, `signal_store.py`, `bias_bridge.py`, `pattern_detector.py`, `pattern_strategy.py`, `retracement.py`, `fibonacci.py`, `breakout_engine.py`, `elliott_wave.py`. | Writes the caller-selected signal DB, default `signals.db`; `db_path=None` writes nothing; no CSV. |
| `profile_pipeline.py` | Profiles representative backtest/pipeline execution points. | CLI only. | `backtest.py`, `pipeline.py`, `cProfile`, `pstats`. | Reads historical CSVs through `backtest.py`; pipeline logging uses `backtest_signals.db`. |
| `signal_store.py` | Creates and updates the SQLite signal ledger, including pending and closed outcomes. | `pipeline.py`; `pattern_stats.py` imports its default path. | `sqlite3`, datetime. | Reads/writes configurable DB, default `signals.db`; not `live_state.db` or `analysis_runs.db`. |
| `sl_tp.py` | Calculates stop-loss and take-profit levels from ATR, swings, S/R, and fallback R:R. | `pipeline.py`. | Caller-provided ATR, swings, and S/R; no imported project module. | None. |
| `support_resistance.py` | Clusters swing points into S/R levels and finds nearest levels. | `pipeline.py`, `live_runner.py`; `test.py`. | NumPy; caller-provided swing data. | None. |
| `test.py` | Ad hoc synthetic check for zigzag, S/R, and entry helpers. | CLI only. | `zigzag.py`, `support_resistance.py`, `entry.py`. | None. |
| `test_api.py` | Black-box HTTP checks for API routes, Bybit chart behavior, validation, and storage separation. | CLI only; requires Uvicorn. | `urllib`, SQLite, running API. | Reads test live state and documents test analysis DB paths; does not create application rows itself. |
| `test_connection.py` | One-shot Bybit server-time connectivity probe. | CLI only. | `requests`. | None. |
| `test_live_persistence.py` | Replays historical candles through live persistence and checks retention/data integrity. | CLI only. | `live_runner.py`, `live_store.py`, backtest CSV loaders, pandas, SQLite. | Reads `data/*.csv`; writes scratch `live_state_test.db`; no production analysis DB. |
| `validity.py` | Checks trade direction, stop/target validity, R:R, and distance constraints. | `pipeline.py`. | Input trade and current price; no imported project module. | None. |
| `volume.py` | Detects volume spikes and price-volume divergence. | `pipeline.py`. | NumPy. | None. |
| `wicks.py` | Detects rejection wicks and matches recent wick events to S/R levels. | `pipeline.py`. | NumPy; caller-provided S/R. | None. |
| `zigzag.py` | Finds ATR-based swing highs and lows used by structure and S/R logic. | `pipeline.py`, `live_runner.py`; `test.py`. | TA-Lib ATR, NumPy. | None. |

## API Endpoints

- `GET /health` - Returns API status, configured DB paths, live retention information, and market-data cache diagnostics.
- `GET /symbols` - Lists Bybit/local symbols, chart timeframes, local timeframes, and whether local analysis history is available.
- `GET /live/state` - Returns the latest live tick, timeframe state, bias, signal/skip information, recent skips, and overlay freshness.
- `GET /live/zones` - Returns current FVG/imbalance and consolidation zones, optionally filtered by timeframe.
- `GET /live/levels` - Returns current support/resistance levels, optionally filtered by timeframe and ordered by touches.
- `GET /live/stats` - Returns resolved live-trade win rate, average R:R, profit factor, in-flight count, and recent resolved trades.
- `GET /ohlc` - Returns recent candles oldest-first; source can prefer Bybit, force local CSV, or force Bybit.
- `POST /analyze` - Validates local `1D`/`4H`/`1H` history, queues a date-bounded backtest, stores the job in `analysis_runs.db`, and returns a job ID.
- `GET /analyze/status/{job_id}` - Returns queued/running/completed/error state, summary, funnel counts, and completed trades when available.
- `GET /analyze/jobs` - Lists recent analysis jobs newest first.
- `POST /predict` - Fetches live candles into memory, runs the shared pipeline synchronously, and returns a signal or skip without persistence.

## How To Run Locally

Use three terminals from the backend directory. Adjust paths if the frontend lives in another repository; this repository contains the backend Python files only.

**Terminal 1: API**

```powershell
cd C:\Users\OFFICIAL\Desktop\trading-bot-backend
python -m uvicorn api:app --reload --port 8000
```

**Terminal 2: live runner**

```powershell
cd C:\Users\OFFICIAL\Desktop\trading-bot-backend
python live_runner.py
```

**Terminal 3: frontend**

```powershell
cd path\to\frontend
npm run dev
```

For a backend-only setup, run the first two terminals and call the API directly at `http://127.0.0.1:8000`. The live runner requires the configured Bybit network access and dependencies in the local Python environment.

## Storage Boundaries

- `live_state.db` is the rolling live hand-off owned by `live_store.py`.
- `analysis_runs.db` stores queued `/analyze` jobs and backtest results owned by `analysis_store.py`.
- `signals.db` is the default pipeline signal ledger owned by `signal_store.py`.
- `backtest_signals.db` is used when `backtest.py` passes that path to the pipeline.
- `data/*.csv` is historical/local candle input for backtests and local chart fallback. `ingestion_bybit.fetch_and_save_all()` can write CSV history.

The repository also contains test/probe databases such as `live_state_test.db`, `analysis_runs_test.db`, and other local artifacts. They are not part of the three runtime paths above.
