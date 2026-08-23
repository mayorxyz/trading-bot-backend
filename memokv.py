"""
memokv.py — tiny process-local memo for pure functions of immutable frames.

Bars are immutable once closed, so any computation whose inputs are fully
described by a frame's (last-bar timestamp, row count) can be cached and
reused until that bar advances. This is the shared store behind the causal
artifact caching introduced in Build #8:

  * pipeline.py    — bias / candidate-probe / engine-bundle memos (own copy)
  * bias_bridge.py — per-timeframe EMA bias, imbalances, regime, structure,
                     liquidity components

Keys must embed PIPELINE_MEMO_VERSION-style discipline: bump a version tag
whenever the computation behind a key changes meaning.

Values are READ-ONLY by contract — callers must never mutate a returned
dict/DataFrame in place, because another caller may share it.
"""

import os
import threading
from collections import OrderedDict

_MEMO_MAX = int(os.environ.get("PIPELINE_MEMO_ENTRIES", 512))

_store = OrderedDict()
_lock = threading.Lock()


def frame_key(df):
    """Fingerprint one OHLCV frame: (last-bar ISO str, row count)."""
    if df is None or not len(df):
        return ("empty", 0)
    return (str(df.index[-1]), int(len(df)))


def get(key):
    with _lock:
        if key in _store:
            _store.move_to_end(key)
            return _store[key]
    return None


def put(key, value):
    with _lock:
        _store[key] = value
        while len(_store) > _MEMO_MAX:
            _store.popitem(last=False)
