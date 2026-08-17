"""
Phase 6 — Remaining MEDIUM/LOW priority concepts.
1. Break-even management
2. Leverage / margin / liquidation engine (crypto perps)
3. Lot/pip/tick value & spread/fee model (crypto: tick + fees, not pips)
4. Market Structure Shift (MSS) — weak early warning
5. News filter (NFP/FOMC/CPI) — volatility avoidance for crypto too
6. Session killzones — optional/empirical for crypto, core for forex
7. Compounding projection (simulation only)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone, time


# ---------- 1. Break-even management ----------

def check_breakeven_trigger(current_price: float, entry_price: float, direction: str,
                             liquidity_target_hit: bool) -> dict:
    """
    Move stop to entry only AFTER price has taken the target liquidity level
    (not too early). Returns {'move_to_be': bool, 'new_stop': entry_price or None}
    """
    if not liquidity_target_hit:
        return {'move_to_be': False, 'new_stop': None}
    return {'move_to_be': True, 'new_stop': entry_price}


# ---------- 2. Leverage / margin / liquidation ----------

def compute_margin_requirements(notional_position: float, leverage: float) -> dict:
    required_margin = notional_position / leverage
    return {'required_margin': required_margin, 'buying_power_used': notional_position}


def compute_liquidation_price(entry_price: float, leverage: float, direction: str,
                               maintenance_margin_pct: float = 0.005) -> float:
    """
    Simplified isolated-margin liquidation estimate for crypto perps.
    Long: liq = entry * (1 - 1/leverage + maintenance_margin_pct)
    Short: liq = entry * (1 + 1/leverage - maintenance_margin_pct)
    """
    if direction == 'long':
        return entry_price * (1 - 1 / leverage + maintenance_margin_pct)
    else:
        return entry_price * (1 + 1 / leverage - maintenance_margin_pct)


def margin_call_check(equity: float, required_margin: float) -> bool:
    return equity < required_margin


# ---------- 3. Tick/fee model (crypto) & pip model (forex) ----------

def crypto_cost_model(price: float, quantity: float, taker_fee_pct: float = 0.0006,
                       maker_fee_pct: float = 0.0002, is_taker: bool = True,
                       bid_ask_spread: float = None) -> dict:
    fee_pct = taker_fee_pct if is_taker else maker_fee_pct
    notional = price * quantity
    fee_cost = notional * fee_pct
    spread_cost = (bid_ask_spread * quantity) if bid_ask_spread else 0.0
    return {'notional': notional, 'fee_cost': fee_cost, 'spread_cost': spread_cost,
            'total_cost': fee_cost + spread_cost}


def forex_pip_model(pair: str, price: float, lot_size_units: float) -> dict:
    is_jpy = 'JPY' in pair.upper()
    pip_size = 0.01 if is_jpy else 0.0001
    pip_value = (pip_size / price) * lot_size_units
    return {'pip_size': pip_size, 'pip_value': pip_value}


# ---------- 4. MSS (Market Structure Shift) — weak early warning ----------

def detect_mss(structure_events: list, trend: str) -> list:
    """
    MSS (bearish, in uptrend): price makes a lower high without breaking last high.
    MSS (bullish, in downtrend): price makes a higher low without breaking last low.
    Operates on Phase 5's structure['events'] list; flags swing points that
    fail to extend the trend (no accompanying BOS).
    """
    mss_events = []
    highs = [e for e in structure_events if e['event'] == 'swing_high']
    lows = [e for e in structure_events if e['event'] == 'swing_low']
    bos_idxs = {e['idx'] for e in structure_events if 'BOS' in e['event']}

    if trend == 'uptrend':
        for i in range(1, len(highs)):
            if highs[i]['price'] < highs[i - 1]['price'] and highs[i]['idx'] not in bos_idxs:
                mss_events.append({**highs[i], 'event': 'MSS_bear'})
    elif trend == 'downtrend':
        for i in range(1, len(lows)):
            if lows[i]['price'] > lows[i - 1]['price'] and lows[i]['idx'] not in bos_idxs:
                mss_events.append({**lows[i], 'event': 'MSS_bull'})
    return mss_events


# ---------- 5. News filter ----------

HIGH_IMPACT_EVENTS = {'NFP', 'FOMC', 'CPI'}


def is_news_blackout(event_calendar: pd.DataFrame, check_date: datetime,
                      events_to_avoid: set = None) -> dict:
    """
    event_calendar: DataFrame with columns ['date', 'event', 'impact'] (date as date obj)
    Returns {'blackout': bool, 'events_today': [list]}
    """
    events_to_avoid = events_to_avoid or HIGH_IMPACT_EVENTS
    day = check_date.date() if isinstance(check_date, datetime) else check_date
    todays = event_calendar[event_calendar['date'] == day]
    matching = todays[todays['event'].isin(events_to_avoid)]
    return {'blackout': not matching.empty, 'events_today': matching['event'].tolist()}


# ---------- 6. Session killzones ----------

SESSION_WINDOWS_NY = {
    'london': (time(2, 0), time(5, 0)),
    'ny': (time(6, 0), time(11, 0)),
    'asia': (time(18, 0), time(23, 59)),  # wraps past midnight; handle separately for 00:00-02:00
    'lunch_lull': (time(5, 0), time(6, 0)),
}

PAIR_SESSION_MAP = {
    'EUR': ['london', 'ny'], 'GBP': ['london', 'ny'], 'CHF': ['london', 'ny'],
    'AUD': ['asia'], 'NZD': ['asia'], 'JPY': ['asia'], 'XAU': ['asia', 'london', 'ny'],
}


def get_active_session(ny_time: time) -> str:
    for name, (start, end) in SESSION_WINDOWS_NY.items():
        if start <= ny_time <= end:
            return name
    if time(0, 0) <= ny_time <= time(2, 0):
        return 'asia'
    return 'off_session'


def is_favorable_session(pair: str, ny_time: time, is_crypto: bool = False) -> dict:
    """
    Crypto: sessions are optional/empirical — always returns favorable=True with a note,
    unless caller wants to enforce a validated volatility window separately.
    Forex: checks pair's mapped sessions.
    """
    session = get_active_session(ny_time)
    if is_crypto:
        return {'favorable': True, 'session': session,
                'note': 'crypto has no official sessions; validate empirically before enforcing'}

    base = pair[:3].upper()
    mapped = PAIR_SESSION_MAP.get(base, ['london', 'ny'])
    return {'favorable': session in mapped, 'session': session, 'mapped_sessions': mapped}


# ---------- 7. Compounding projection (simulation only) ----------

def project_compounding(initial_balance: float, monthly_return_pct: float, months: int) -> pd.DataFrame:
    balances = [initial_balance]
    for _ in range(months):
        balances.append(balances[-1] * (1 + monthly_return_pct))
    return pd.DataFrame({'month': range(months + 1), 'balance': balances})


if __name__ == "__main__":
    # BE management
    print(check_breakeven_trigger(105, 100, 'long', liquidity_target_hit=True))

    # Leverage/margin
    print(compute_margin_requirements(10000, leverage=10))
    print(f"Liq price (long, 10x): {compute_liquidation_price(100, 10, 'long'):.2f}")

    # Cost models
    print(crypto_cost_model(50000, 0.1))
    print(forex_pip_model('EURUSD', 1.0850, 10000))

    # News filter
    cal = pd.DataFrame({
        'date': [datetime(2026, 8, 17).date(), datetime(2026, 8, 18).date()],
        'event': ['NFP', 'ECB'],
        'impact': ['high', 'medium'],
    })
    print(is_news_blackout(cal, datetime(2026, 8, 17)))

    # Sessions
    print(is_favorable_session('GBPUSD', time(8, 0), is_crypto=False))
    print(is_favorable_session('BTCUSDT', time(8, 0), is_crypto=True))

    # Compounding
    proj = project_compounding(10000, 0.06, 12)
    print(f"\n12-month projection at 6%/mo: final balance = {proj['balance'].iloc[-1]:.2f}")
