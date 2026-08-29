"""
trade_manager.py â€” two-step live trade management (TP1 -> breakeven -> trail).

Engines describe a plan; this executes it against candles:

    1. Limit fill check (same convention as backtest/live_runner: the entry
       level must be TOUCHED, within max_fill_wait bars).
    2. Half off at TP1 (entry +/- risk * tp1_rr_mult); stop ratchets to
       breakeven immediately after.
    3. Remaining half trails a chandelier stop (22 bars, 2.0 ATR), ratcheting
       only in the favourable direction.
    4. Resolution when the current stop is touched (or original TP, fallback).
       Conservative intrabar rule: a bar spanning both stop and target counts
       as the STOP first.

Stateless-in-code: every advance() call re-derives everything from the trade
row + candle history, so restarts and repeated ticks are safe. Persistence of
tp1_filled / stop_current happens in the caller via live_store.

realized_rr for managed trades = 0.5 * tp1_rr + 0.5 * (exit return on risk),
so /live/stats reflects the actual two-step plan rather than a flat SL/TP bet.
"""

import numpy as np
import pandas as pd

from tbb.engines.breakout_engine import calculate_atr


CHANDELIER_PERIOD = 22
CHANDELIER_ATR_MULT = 2.0
ATR_PERIOD = 14


def _is_long(direction):
    return str(direction).upper() == "LONG"


def advance(trade, df, tp1_rr_mult=1.5, max_fill_wait=24):
    """
    Walk an in-flight trade forward through `df` (full timeframe DataFrame).

    Returns dict:
        fill_ts        timestamp of limit fill or None
        outcome        'pending' | 'no_fill' | 'open' | 'win' | 'loss'
        exit_price     only set when resolved
        resolved_ts    only set when resolved
        tp1_filled     bool â€” cumulative state (may be pre-existing)
        stop_current   latest ratcheted stop (persist this)
        realized_rr    combined-R RR (set when resolved)
        pnl            price-delta equivalent (set when resolved)
        events         human-readable list of what happened this walk
    """
    fwd_mask = df.index > pd.Timestamp(trade["opened_at"])
    fwd = df[fwd_mask]
    if fwd.empty:
        return _result(outcome="pending", tp1_filled=bool(trade.get("tp1_filled")),
                       stop_current=trade.get("stop_current"))

    entry = float(trade["entry_price"])
    orig_stop = float(trade["stop_price"])
    orig_tp = float(trade["take_profit"])
    long = _is_long(trade["direction"])
    sign = 1.0 if long else -1.0
    risk = abs(entry - orig_stop)
    if risk <= 0:
        return _result(outcome="pending", tp1_filled=False,
                       stop_current=orig_stop,
                       events=["degenerate stop distance â€” cannot manage"])

    tp1_level = entry + sign * risk * tp1_rr_mult
    tp1_done = bool(trade.get("tp1_filled"))
    stop_cur = float(trade["stop_current"]) if trade.get("stop_current") is not None \
        else orig_stop

    highs_all = df["high"].to_numpy(dtype=float)
    lows_all = df["low"].to_numpy(dtype=float)
    closes_all = df["close"].to_numpy(dtype=float)
    atr_series = calculate_atr(highs_all, lows_all, closes_all, period=ATR_PERIOD)

    pos_in_df = np.flatnonzero(fwd_mask)
    idx = fwd.index
    highs = fwd["high"].to_numpy(dtype=float)
    lows = fwd["low"].to_numpy(dtype=float)

    # ---- phase 1: limit fill ----
    fill_i = None
    for i in range(min(len(fwd), max_fill_wait)):
        if long and lows[i] <= entry:
            fill_i = i
            break
        if not long and highs[i] >= entry:
            fill_i = i
            break
    if fill_i is None:
        outcome = "no_fill" if len(fwd) >= max_fill_wait else "pending"
        return _result(outcome=outcome, tp1_filled=tp1_done, stop_current=stop_cur)

    events = []
    fill_ts = idx[fill_i]

    # ---- phase 2: manage from fill bar onward ----
    for k in range(fill_i, len(fwd)):
        h, l = highs[k], lows[k]
        df_pos = int(pos_in_df[k])
        cur_ts = idx[k]

        if long and l <= stop_cur:
            return _finish(trade, fill_ts, cur_ts, stop_cur, risk, sign,
                           tp1_done, tp1_rr_mult, stop_current=stop_cur,
                           tp1_filled=tp1_done, events=events)
        if not long and h >= stop_cur:
            return _finish(trade, fill_ts, cur_ts, stop_cur, risk, sign,
                           tp1_done, tp1_rr_mult, stop_current=stop_cur,
                           tp1_filled=tp1_done, events=events)

        if not tp1_done:
            tp1_hit = (h >= tp1_level) if long else (l <= tp1_level)
            if tp1_hit:
                tp1_done = True
                be = entry
                stop_cur = max(stop_cur, be) if long else min(stop_cur, be)
                events.append(
                    f"TP1 filled @ {cur_ts} ({tp1_rr_mult:.1f}R half) â€” "
                    f"stop moved to breakeven")
                continue  # stop applies from next bar (conservative ordering)

        if tp1_done:
            start = max(0, df_pos - CHANDELIER_PERIOD + 1)
            hh = float(np.max(highs_all[start:df_pos + 1]))
            ll = float(np.min(lows_all[start:df_pos + 1]))
            atr_now = float(atr_series[df_pos]) if atr_series is not None and \
                df_pos < len(atr_series) else None
            if atr_now and atr_now > 0:
                chand = (hh - CHANDELIER_ATR_MULT * atr_now) if long \
                    else (ll + CHANDELIER_ATR_MULT * atr_now)
                if long and chand > stop_cur:
                    stop_cur = chand
                elif not long and chand < stop_cur:
                    stop_cur = chand

        # Original take-profit as full-exit fallback (e.g. gap past TP1 zone).
        if (long and h >= orig_tp) or (not long and l <= orig_tp):
            return _finish(trade, fill_ts, cur_ts, orig_tp, risk, sign,
                           tp1_done, tp1_rr_mult, stop_current=stop_cur,
                           tp1_filled=tp1_done, events=events)

    return _result(outcome="open", fill_ts=fill_ts, tp1_filled=tp1_done,
                   stop_current=stop_cur, events=events)


def _finish(trade, fill_ts, resolved_ts, exit_price, risk, sign,
            tp1_done, tp1_rr_mult, stop_current, tp1_filled, events):
    entry = float(trade["entry_price"])
    exit_leg = (float(exit_price) - entry) * sign / risk if risk > 0 else 0.0
    realized_rr = round(0.5 * tp1_rr_mult + 0.5 * exit_leg, 4) if tp1_done \
        else round(exit_leg, 4)
    pnl = realized_rr * risk
    outcome = "win" if realized_rr > 0 else "loss"
    if tp1_done:
        events.append(f"resolved @ {resolved_ts} exit {exit_price} "
                      f"(combined R={realized_rr:+.2f})")
    return _result(outcome=outcome, fill_ts=fill_ts, exit_price=float(exit_price),
                   resolved_ts=resolved_ts, tp1_filled=True if tp1_done else tp1_filled,
                   stop_current=stop_current, realized_rr=realized_rr, pnl=pnl,
                   events=events)


def _result(outcome, fill_ts=None, exit_price=None, resolved_ts=None,
            tp1_filled=False, stop_current=None, realized_rr=None, pnl=None,
            events=None):
    return {
        "fill_ts": fill_ts,
        "outcome": outcome,
        "exit_price": exit_price,
        "resolved_ts": resolved_ts,
        "tp1_filled": bool(tp1_filled),
        "stop_current": stop_current,
        "realized_rr": realized_rr,
        "pnl": pnl,
        "events": events or [],
    }
