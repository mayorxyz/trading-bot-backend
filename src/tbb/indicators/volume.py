"""
volume.py â€” volume spike + volume-price divergence detection.
"""

import numpy as np


def detect_volume_spike(volumes, lookback=20, spike_mult=2.0):
    """
    Returns list of indices where volume >= spike_mult * avg(lookback).
    """
    volumes = np.asarray(volumes, dtype=float)
    events = []
    for i in range(lookback, len(volumes)):
        avg = np.mean(volumes[i - lookback:i])
        if avg > 0 and volumes[i] >= avg * spike_mult:
            events.append({"index": i, "volume": float(volumes[i]), "avg": float(avg),
                            "ratio": round(float(volumes[i] / avg), 2)})
    return events


def detect_volume_divergence(closes, volumes, lookback=10):
    """
    Flags where price makes a new high/low but volume is declining
    (weakening move â€” potential reversal warning).
    Returns list of {"index", "type": "BEARISH_DIV"/"BULLISH_DIV"}.
    """
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    events = []

    for i in range(lookback, len(closes)):
        window_c = closes[i - lookback:i + 1]
        window_v = volumes[i - lookback:i + 1]

        if closes[i] == window_c.max() and volumes[i] < np.mean(window_v[:-1]):
            events.append({"index": i, "type": "BEARISH_DIV"})
        elif closes[i] == window_c.min() and volumes[i] < np.mean(window_v[:-1]):
            events.append({"index": i, "type": "BULLISH_DIV"})

    return events
