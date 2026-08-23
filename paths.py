"""
paths.py — single source of truth for every location the backend reads or writes.

Source code and runtime artifacts are deliberately separated: all databases and
candle history live under data/, so cleaning the repo root can never touch the
live ledger, and git never sees binary churn.

Every constant is env-overridable (tests point them at scratch files), and
DATA_DIR is created on import so first runs never fail on a missing directory.

Storage-boundary contract lives in readme.md; the four DBs stay separate FILES
so live, analysis, and signal data can never mix.
"""

import os

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT_DIR, "data"))

# LIVE path — owned by live_store.py. Rolling hand-off buffer + trade ledger.
LIVE_DB = os.environ.get("LIVE_DB_PATH", os.path.join(DATA_DIR, "live_state.db"))

# ANALYSIS/backtest path — owned by analysis_store.py. Queued jobs + results.
ANALYSIS_DB = os.environ.get("ANALYSIS_DB_PATH",
                             os.path.join(DATA_DIR, "analysis_runs.db"))

# Pipeline signal ledger — owned by signal_store.py.
SIGNALS_DB = os.environ.get("SIGNALS_DB_PATH", os.path.join(DATA_DIR, "signals.db"))

# Backtest pipeline-logging DB — passed to the pipeline by backtest.py.
BACKTEST_LOG_DB = os.environ.get("BACKTEST_LOG_DB_PATH",
                                 os.path.join(DATA_DIR, "backtest_signals.db"))

os.makedirs(DATA_DIR, exist_ok=True)
