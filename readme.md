```markdown
# Trading Bot Backend — Structural Trading System

**Status**: Alpha (Phases 1–4 complete, Phase 5 partial, Phase 6 stubs, no live execution)  
**Language**: Python 3.8+  
**Dependencies**: pandas, numpy, talib, asyncio, websockets, sqlite3

---

## 1. Project Purpose

A full-stack crypto/forex trading system implementing **structural trading concepts** from high-quality trading education:

- Entry via imbalance/FVG confluence with double rejection validation
- Risk-managed position sizing (1% base, adaptive via drawdown/profit overlays)
- Top-down multi-timeframe bias alignment before entry
- Automated trade journaling + outcome tracking for pattern optimization
- Backtest harness with future-candle fill simulation (not just latest price)

**Goal**: Reduce discretionary trading noise by codifying price action rules into repeatable, testable patterns.

---

## 2. Architecture Overview

### Six-Phase Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                    RAW OHLCV DATA (Bybit/Exchange)              │
└────────────────────────────┬────────────────────────────────────┘
                             │
        ┌────────────────────▼─────────────────────┐
        │  Phase 1: Primitives & Trend Filter      │
        │  • Candle body/wick analysis (Doji)      │
        │  • 3-bar / 5-bar fractal swings          │
        │  • 50-EMA slope + bias classification    │
        │  • Trend-change flags                    │
        └────────────────────┬─────────────────────┘
                             │
        ┌────────────────────▼──────────────────────────┐
        │  Phase 2: Signal Engine                       │
        │  • Imbalance/FVG detection (3-candle)         │
        │  • Double Rejection (hold/fail/untested)      │
        │  • Sweep vs Break of liquidity levels         │
        └────────────────────┬──────────────────────────┘
                             │
        ┌────────────────────▼────────────────────────────┐
        │  Phase 3: Regime Filters & Entry Model          │
        │  • Chop/Consolidation/Trending regime check     │
        │  • Top-down bias (HTF wins conflicts)           │
        │  • VSSR entry (POI tap → lower-TF imbalance)   │
        └────────────────────┬────────────────────────────┘
                             │
        ┌────────────────────▼──────────────────────────┐
        │  Phase 4: Risk Management & Journal           │
        │  • 1% per-trade, daily/weekly loss caps       │
        │  • Flatline (drawdown) & profit-scale rules   │
        │  • 2-bullets-per-POI limit                    │
        │  • Trade journal + pattern win-rate tracking  │
        └────────────────────┬──────────────────────────┘
                             │
        ┌────────────────────▼──────────────────────────────┐
        │  Phase 5: Market Structure & Liquidity            │
        │  • HH/HL/LL/LH trend confirmation                │
        │  • BOS (Break of Structure) detection             │
        │  • TC (Trend Change) + fake-TC filter             │
        │  • Liquidity classification (low-fruit/major)     │
        │  • MSS (Market Structure Shift) early warning     │
        └────────────────────┬──────────────────────────────┘
                             │
        ┌────────────────────▼──────────────────────────────┐
        │  Phase 6: Supplementary Filters (PARTIAL)         │
        │  • Break-even management                          │
        │  • Leverage/margin/liquidation calcs (crypto)     │
        │  • News filter (NFP/FOMC/CPI blackout)            │
        │  • Session killzones (forex/crypto empirical)     │
        │  • Compounding projection simulation              │
        └────────────────────┬──────────────────────────────┘
                             │
        ┌────────────────────▼──────────────────────────────┐
        │  ⚙️  SL/TP Calculator                             │
        │  • SL = WIDER(ATR-based, swing-based)             │
        │  • TP = next S/R level or fixed R:R fallback      │
        │  • Entry = strongest S/R touch in range           │
        └────────────────────┬──────────────────────────────┘
                             │
        ┌────────────────────▼────────────────┐
        │  ✅ VALID SIGNAL                    │
        │  • Confluence score (0-100)         │
        │  • Log to SQLite signal_store.db    │
        └────────────────────┬────────────────┘
                             │
        ┌────────────────────▼────────────────────────────┐
        │  🧪 BACKTEST HARNESS                            │
        │  • Walk forward through future candles           │
        │  • Check SL/TP hit per candle (not just price)   │
        │  • Aggregate: win rate, RR, profit factor        │
        └────────────────────┬────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  📊 OUTCOMES    │
                    │  (WIN/LOSS/     │
                    │   TIMEOUT)      │
                    └─────────────────┘
```

---

## 3. Data Flow: Entry to Exit

```
 1. RAW OHLCV (from Bybit REST or WebSocket)
         ↓
 2. Phase 1: Build fractal swings + EMA bias
         ↓
 3. Phase 2: Detect imbalances, test for Double Rejection
         ↓
 4. Phase 3a: Filter regime (chop = SKIP)
         ↓
 5. Phase 3b: Resolve top-down bias (wait for ≥2 TF alignment)
         ↓
 6. Phase 3c: Look for VSSR entry (POI tap + lower-TF imbalance)
         ↓
 7. Phase 4: Check risk limits (daily/weekly cap, drawdown overlay)
         ↓
 8. Entry + SL/TP from support_resistance + zigzag
         ↓
 9. Validity check (R:R ≥ 2.0, entry ≤ 2% away)
         ↓
10. Confluence score (0-100, weighted 8 factors)
         ↓
11. Log to SQLite (signal_store.db)
         ↓
12. [BACKTEST ONLY] Simulate: check SL/TP hit vs future candles
         ↓
13. Aggregate stats: win rate, avg RR, profit factor
```

> **MISSING LINK**: Live execution does not exist. Step 11 is the terminal point.  
> To go live, add:
> - Order placement (Bybit REST `POST /v5/order/create`)
> - Position tracking (memory + SQLite cache)
> - Exit handler (check pending signals vs market prices each candle)

---

## 4. File-by-File Description

| File | Responsibility | Key Exports | Dependencies |
|------|---|---|---|
| **phase1_primitives.py** | Candle analysis, fractal swings, EMA trend | `candle_primitives()`, `fractal_swings()`, `ema_trend_filter()`, `build_phase1_features()` | pandas, numpy |
| **phase2_signal_engine.py** | Imbalance detection, Double Rejection, Sweep/Break | `detect_imbalances()`, `evaluate_double_rejection()`, `evaluate_sweep_or_break()`, `mark_tested_imbalances()` | phase1_primitives, pandas |
| **phase3_orchestration.py** | Regime classification, top-down bias, VSSR entry | `detect_regime()`, `per_tf_bias()`, `resolve_topdown_bias()`, `find_vssr_entry()` | phase2_signal_engine, pandas |
| **phase4_risk_journal.py** | RiskManager class, position sizing, trade logging | `RiskManager`, `TradeJournal`, `summary_stats()` | pandas, datetime |
| **phase5_structure_liquidity.py** | Market structure classification, BOS/TC | `classify_structure()`, `check_fake_tc()`, `detect_mss()` | phase1_primitives, pandas |
| **phase6_supplementary.py** | Break-even, leverage/liquidation, news/session filters | `check_breakeven_trigger()`, `compute_liquidation_price()`, `is_news_blackout()`, `is_favorable_session()` | pandas |
| **zigzag.py** | ATR-based swing detection (foundation) | `get_zigzag_swings()`, `print_swings()` | talib, numpy |
| **support_resistance.py** | Cluster swings into S/R levels | `find_sr_levels()`, `nearest_level()` | numpy |
| **consolidation.py** | Consolidation zone detection | `find_consolidation_zones()` | numpy |
| **breakouts.py** | S/R breakout + consolidation breakout detection | `detect_sr_breakout()`, `detect_consolidation_breakout()` | (none) |
| **wicks.py** | Rejection wick detection + S/R confluence | `detect_wicks()`, `wick_at_level()` | numpy |
| **volume.py** | Volume spike + divergence detection | `detect_volume_spike()`, `detect_volume_divergence()` | numpy |
| **entry.py** | Best entry price finder | `find_best_entry()` | (none) |
| **sl_tp.py** | SL/TP calculator (ATR + swing + S/R level) | `calculate_sl()`, `calculate_tp()`, `get_trade_levels()` | (none) |
| **validity.py** | Trade validation (R:R, distance checks) | `validate_trade()` | (none) |
| **confluence.py** | Signal confluence scoring (0-100) | `calculate_confluence()`, `confidence_label()` | (none) |
| **pattern_detector.py** | All 61 TA-Lib candlestick patterns | `detect_all_patterns()`, `get_active_patterns()`, `summarize_bias()` | talib, pandas |
| **pipeline.py** | Full analysis pipeline (1 pair snapshot) | `analyze_pair()` | all above |
| **signal_store.py** | SQLite logging of signals + outcomes | `init_db()`, `log_signal()`, `close_signal()`, `get_pending_signals()` | sqlite3 |
| **pattern_stats.py** | Win-rate queries per pattern/pair/TF | `get_stats()`, `get_pattern_winrate()` | sqlite3 |
| **backtest.py** | Historical simulation harness | `simulate_trade()`, `run_backtest()`, `summarize_backtest()` | numpy |
| **ingestion.py** | Bybit WebSocket candle stream (live) | `listen()` (async) | websockets, asyncio |
| **ingestion_analysis.py** | Bybit WebSocket + TA-Lib pattern detection | `analyze()`, `prefill_history()`, `listen()` (async) | talib, websockets |
| **multi_pair_scanner.py** | (stub/partial) Multi-pair scanning logic | — | — |
| **multi_timeframe_analysis.py** | Multi-TF confluence analysis (4 patterns + custom) | `detect_tweezer()`, `detect_inside_bar_false_breakout()`, `fibonacci_levels()`, `fetch_candles()` | talib, numpy, requests |
| **test_connection.py** | Quick API health check | — | requests |

---

## 5. Implementation Status

### ✅ Fully Implemented

- [x] **Candle primitives** — body, wick, Doji detection (`phase1_primitives.py`)
- [x] **Fractals (3-bar, 5-bar)** — confirmed swing points (`phase1_primitives.py`)
- [x] **EMA50 bias** — slope trend filter, above/below, trend change (`phase1_primitives.py`)
- [x] **Imbalance/FVG detection** — 3-candle gap, tested/untested tracking (`phase2_signal_engine.py`)
- [x] **Double Rejection Rule** — hold/fail/unresolved classification (`phase2_signal_engine.py`)
- [x] **Sweep vs Break** — multi-candle acceptance window check (`phase2_signal_engine.py`)
- [x] **Liquidity classification** — high-prob retest levels via clustering (`support_resistance.py`)
- [x] **S/R Level detection** — zigzag swing clustering (`support_resistance.py` + `zigzag.py`)
- [x] **Regime classification** — chop/consolidation/trending (`phase3_orchestration.py`)
- [x] **Consolidation zones** — tight-range detection (`consolidation.py`)
- [x] **Top-down bias** — HTF wins conflicts, ≥2 TF alignment (`phase3_orchestration.py`)
- [x] **VSSR entry model** — POI tap → lower-TF imbalance confirmation (`phase3_orchestration.py`)
- [x] **SL/TP calculators** — ATR + swing combo, S/R fallback (`sl_tp.py`)
- [x] **Trade validity filters** — R:R, distance, SL width (`validity.py`)
- [x] **Risk manager** — 1% per-trade, daily/weekly caps, flatline, profit scaling (`phase4_risk_journal.py`)
- [x] **Trade journal** — SQL logging, pattern/pair/TF tracking (`phase4_risk_journal.py` + `signal_store.py`)
- [x] **Candlestick patterns** — all 61 TA-Lib functions (`pattern_detector.py`)
- [x] **Backtest harness** — proper future-candle fill simulation (`backtest.py`)
- [x] **Confluence scoring** — 8-factor weighted score (`confluence.py`)
- [x] **Volume signals** — spike + divergence detection (`volume.py`)
- [x] **Wick rejection** — stop-hunt detection at S/R levels (`wicks.py`)
- [x] **Breakout detection** — S/R + consolidation (`breakouts.py`)
- [x] **Entry finder** — strongest level within range (`entry.py`)

### 🟡 Partially Implemented

- [ ] **BOS/TC detection** — structure events logged, but **Phase 3 does not consume** them for trade decisions (`phase5_structure_liquidity.py`)
- [ ] **Fake-TC filter** — coded but **not wired into entry logic** (`phase5_structure_liquidity.py`)
- [ ] **Market Structure Shift (MSS)** — detection function exists but **never called** (`phase6_supplementary.py`)
- [ ] **News filter** — calendar-aware blackout logic exists but **not in pipeline** (`phase6_supplementary.py`)
- [ ] **Session killzones** — defined but **empirical for crypto, no enforcement** (`phase6_supplementary.py`)
- [ ] **Multi-timeframe analysis** — scripts exist (`multi_pair_scanner.py`, `multi_timeframe_analysis.py`) but **not integrated** with decision pipeline
- [ ] **Custom patterns** — Tweezer Top/Bottom, Inside Bar False Breakout coded but **not in active pattern detection** (`multi_timeframe_analysis.py`)

### ❌ Missing / Not Implemented

- [ ] **Live execution connector** — no Bybit/Binance order placement
- [ ] **Position tracker** — no state management for open positions
- [ ] **Order management** — no modify/close/exit logic
- [ ] **Data persistence** — no candle history database (only signal store)
- [ ] **Account integration** — no balance/margin queries
- [ ] **Break-even management** — logic exists but not hooked to position updates
- [ ] **Leverage/liquidation alerts** — calculations exist, no monitoring
- [ ] **Compounding simulation** — projection logic exists but no real P&L feedback loop
- [ ] **Custom ML patterns** — no model integration
- [ ] **Strategy optimizer** — no backtester-to-parameter searcher pipeline
- [ ] **Live multi-pair scanner** — no automated pair watchlist + concurrent analysis

---

## 6. Configuration Parameters

### `phase1_primitives.py`
```python
ema_length = 50            # EMA for trend filter
side_bars = 2              # 5-bar fractals (side_bars=1 → 3-bar)
slope_lookback = 3         # candles to check EMA slope
```

### `phase2_signal_engine.py`
```python
min_body_ratio = 0.6       # expansion candle threshold for FVG
max_candles = 3            # Double Rejection window
acceptance_bars = 3        # Sweep/Break confirmation bars
```

### `phase3_orchestration.py`
```python
VSSR_MAP = {'1D': '1H', '4H': '15M', '1H': '5M', '15M': '3M'}  # POI → entry TF
```

### `phase4_risk_journal.py`
```python
base_risk_pct = 0.01       # 1% per trade
max_daily_loss_pct = 0.02  # 2% daily cap
max_weekly_loss_pct = 0.06 # 6% weekly cap
drawdown_threshold = 0.04  # -4% → halve risk (flatline)
profit_threshold = 0.10    # +10% → +0.25% risk (scale)
```

### `sl_tp.py`
```python
atr_mult = 1.5             # ATR multiplier for SL
min_rr = 2.0               # Minimum reward:risk ratio
```

### `pipeline.py` — Confluence Weights
```python
weights = {
    'pattern_match':    20,   # Candlestick pattern detected
    'trend_align':      20,   # SMA/EMA agrees
    'mtf_alignment':    20,   # Multi-TF full alignment
    'sr_level_strength': 15,  # Entry level touches (scaled)
    'wick_rejection':   10,   # Rejection wick at entry
    'fib_confluence':   10,   # Fib 50/61.8
    'volume_confirm':    5,   # Volume spike on signal
}
```

---

## 7. How to Run: Backtest

```python
from backtest import run_backtest, summarize_backtest
from pipeline import analyze_pair
import numpy as np

# Replace with real historical data
n = 500
price = 100
opens   = [price + np.random.randn() * 0.5 for _ in range(n)]
highs   = [o + abs(np.random.randn()) for o in opens]
lows    = [o - abs(np.random.randn()) for o in opens]
closes  = [opens[i] + np.random.randn() * 0.8 for i in range(n)]
volumes = [np.random.uniform(1000, 5000) for _ in range(n)]

candles = {
    'open': opens,
    'high': highs,
    'low': lows,
    'close': closes,
    'volume': volumes
}

def signal_gen(candles_slice, idx):
    result = analyze_pair(
        pair='BTCUSDT', timeframe='1H',
        opens=candles_slice['open'],
        highs=candles_slice['high'],
        lows=candles_slice['low'],
        closes=candles_slice['close'],
        volumes=candles_slice['volume'],
        direction='LONG',
        pattern_name=None,
        trend_aligned=True,
        mtf_full_alignment=False
    )
    if 'skipped' in result:
        return None
    return {
        'direction': result['direction'],
        'entry_price': result['entry'],
        'sl_price': result['sl'],
        'tp_price': result['tp'],
        'pattern': 'pipeline_signal'
    }

results = run_backtest(candles, signal_gen, max_bars=200)
summary = summarize_backtest(results)
print('Backtest Summary:', summary)
```

---

## 8. Next Steps Roadmap

### Priority 1: Live Execution (High Impact)
- [ ] Create `live_executor.py` — connects to Bybit Order REST API
- [ ] Implement `PlaceOrderRunner` — entry at calculated price with SL/TP legs
- [ ] Add `PositionTracker` class — memory + SQLite state for open trades
- [ ] Wire `signal_store` → executor → position updates

### Priority 2: Multi-Pair Scanner (Medium Impact)
- [ ] `multi_pair_live_loop.py` — iterate pairs, analyze each per TF
- [ ] Concurrent WebSocket per pair (or single multiplexed connection)
- [ ] Round-robin analysis with configurable scan interval

### Priority 3: Phase 5 Integration (Medium Impact)
- [ ] Consume `classify_structure()` output in Phase 3 entry logic
- [ ] Wire BOS events as **entry confirmation trigger**
- [ ] Use TC + fake-TC filter to **exit early on reversal signal**

### Priority 4: Phase 6 Enforcement (Low Impact)
- [ ] Add news filter to `can_trade()` check in RiskManager
- [ ] Session-aware entry (forex) + empirical windows (crypto)
- [ ] Break-even update hook in position tracker

### Priority 5: Optimization & Backtesting (Medium Impact)
- [ ] Parameter grid search (SMA length, ATR mult, confluence threshold)
- [ ] Monte Carlo walk-forward validation
- [ ] Per-pattern + per-pair win-rate filtering

---

## 9. Testing & Validation

### Unit Tests (Not Yet Implemented)
```
tests/
  test_phase1_primitives.py
  test_phase2_signals.py
  test_phase3_orchestration.py
  test_phase4_risk.py
  test_sl_tp_calculator.py
  test_backtest_engine.py
```

### Manual Validation
1. Run backtest on known market (e.g., 2024 BTC range) → expect >50% win rate
2. Check signal store for pattern win-rates → filter low-confidence (<2 occurrences)
3. Paper trade on live feeds before deploying real orders

---

## 10. Dependencies

```
pandas>=1.3.0
numpy>=1.21.0
ta-lib>=0.4.24
websockets>=10.0
requests>=2.28.0
```

### Database
```
signals.db (SQLite)
  ├── signals (table)
  │   ├── id (PRIMARY KEY)
  │   ├── timestamp, pair, timeframe
  │   ├── direction, pattern, shape
  │   ├── entry/sl/tp prices, R:R
  │   ├── confluence_score, valid
  │   ├── outcome (WIN/LOSS/PENDING)
  │   └── closed_price, closed_at
  └── [FUTURE] positions (open trade state)
```

### API Keys (Not Yet Wired)
```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
```

---

## 11. Limitations & Caveats

1. **Backtest assumes perfect fills** — uses candle high/low, not spread/slippage
2. **Signal store queries are simple** — no correlation between patterns, no ML ranking
3. **Multi-TF alignment is hard-coded** — no adaptive timeframe selection per pair
4. **No position management** — SL/TP are static after entry (no trailing stop, no partial TP)
5. **News filter is schema-only** — needs live calendar data integration
6. **Phase 5 outputs unused** — BOS/TC events calculated but not fed back to Phase 3
7. **Backtest is single-threaded** — slow for large parameter grids

---

## 12. Glossary

| Term | Meaning |
|------|---------|
| **FVG** | Fair Value Gap — imbalance zone where price is likely to return |
| **DR** | Double Rejection — price taps zone then holds (proven support/resistance) |
| **BOS** | Break of Structure — price body-closes beyond last confirmed extreme |
| **TC** | Trend Change — close breaks last confirmed swing point in opposite direction |
| **fake-TC** | TC that fails to extend via BOS within N bars, then reverts |
| **MSS** | Market Structure Shift — swing point fails to extend trend (early warning) |
| **VSSR** | Valid Setup, Smart Reentry — POI tap → lower-TF imbalance entry |
| **POI** | Point of Interest — higher-TF imbalance zone |
| **HTF** | Higher Timeframe (wins conflicts in multi-TF bias resolution) |
| **R:R** | Risk:Reward ratio (distance to TP / distance to SL) |
| **Confluence** | Multiple independent signals agreeing → higher confidence |
| **Flatline** | Drawdown overlay — halve risk when down -4% or more |
| **Liquidation Price** | Crypto perps — price level where position is force-closed |

---

## License

Internal Use Only
```