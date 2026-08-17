# Trading Bot Backend — Structural Trading System

**Author**: AI Trading Systems  
**Status**: Alpha (Phases 1-4 complete, Phase 5 partial, Phase 6 stubs, no live execution)  
**Language**: Python 3.8+  
**Dependencies**: pandas, numpy, talib, asyncio, websockets, sqlite3

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
