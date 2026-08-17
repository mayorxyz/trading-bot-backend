"""
Phase 4 — Risk management overlays + journaling.
1. Risk per trade (1% default), max daily/weekly loss caps
2. Flatlining drawdown (risk cut) / profit scaling (risk increase)
3. Two-bullets-per-POI limit
4. Trade journal schema + logging

Stateful: wrap in a RiskManager instance per account.
"""

import pandas as pd
from datetime import datetime, timezone
from dataclasses import dataclass, field


# ---------- 1+2. Risk manager (per-trade, daily/weekly caps, drawdown/profit overlays) ----------

@dataclass
class RiskManager:
    initial_balance: float
    base_risk_pct: float = 0.01          # 1%
    max_daily_loss_pct: float = 0.02     # 2%
    max_weekly_loss_pct: float = 0.06    # 6%
    drawdown_threshold_pct: float = 0.04 # -4% -> halve risk
    profit_threshold_pct: float = 0.10   # +10% -> +0.25% risk
    profit_risk_bump: float = 0.0025

    balance: float = field(init=False)
    daily_pnl: float = field(default=0.0, init=False)
    weekly_pnl: float = field(default=0.0, init=False)
    current_day: str = field(default=None, init=False)
    current_week: str = field(default=None, init=False)
    poi_attempts: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        self.balance = self.initial_balance

    def _day_key(self, ts: datetime) -> str:
        return ts.astimezone(timezone.utc).strftime('%Y-%m-%d')  # UTC cutoff, crypto-safe

    def _week_key(self, ts: datetime) -> str:
        return ts.astimezone(timezone.utc).strftime('%Y-W%W')

    def _roll_period(self, ts: datetime):
        day_key, week_key = self._day_key(ts), self._week_key(ts)
        if day_key != self.current_day:
            self.daily_pnl = 0.0
            self.current_day = day_key
        if week_key != self.current_week:
            self.weekly_pnl = 0.0
            self.current_week = week_key

    def current_drawdown_pct(self) -> float:
        return (self.balance - self.initial_balance) / self.initial_balance

    def effective_risk_pct(self) -> float:
        """Applies flatline (drawdown) and profit-scaling overlays to base risk."""
        dd = self.current_drawdown_pct()
        if dd <= -self.drawdown_threshold_pct:
            return self.base_risk_pct * 0.5
        if dd >= self.profit_threshold_pct:
            return self.base_risk_pct + self.profit_risk_bump
        return self.base_risk_pct

    def can_trade(self, ts: datetime) -> dict:
        """Check daily/weekly loss caps before allowing a new trade."""
        self._roll_period(ts)
        daily_loss_pct = -self.daily_pnl / self.initial_balance if self.daily_pnl < 0 else 0
        weekly_loss_pct = -self.weekly_pnl / self.initial_balance if self.weekly_pnl < 0 else 0

        if daily_loss_pct >= self.max_daily_loss_pct:
            return {'allowed': False, 'reason': 'max_daily_loss_hit'}
        if weekly_loss_pct >= self.max_weekly_loss_pct:
            return {'allowed': False, 'reason': 'max_weekly_loss_hit'}
        return {'allowed': True, 'reason': None}

    def can_enter_poi(self, poi_id: str, max_attempts: int = 2) -> bool:
        """Two bullets per trade idea."""
        return self.poi_attempts.get(poi_id, 0) < max_attempts

    def register_poi_attempt(self, poi_id: str):
        self.poi_attempts[poi_id] = self.poi_attempts.get(poi_id, 0) + 1

    def record_trade_result(self, ts: datetime, pnl: float):
        """Update balance + daily/weekly accumulators after a trade closes."""
        self._roll_period(ts)
        self.balance += pnl
        self.daily_pnl += pnl
        self.weekly_pnl += pnl

    def position_size(self, entry_price: float, stop_price: float) -> float:
        risk_amount = self.balance * self.effective_risk_pct()
        stop_distance = abs(entry_price - stop_price)
        return risk_amount / stop_distance if stop_distance > 0 else 0.0


# ---------- 3. Trade journal ----------

JOURNAL_COLUMNS = [
    'trade_id', 'timestamp', 'pair', 'direction', 'entry_model', 'htf_poi_tf',
    'poi_id', 'entry_price', 'stop_price', 'take_profit', 'stop_distance',
    'planned_rr', 'realized_rr', 'risk_pct_used', 'position_size',
    'outcome', 'pnl', 'balance_after', 'hour_of_day', 'day_of_week', 'notes',
]


class TradeJournal:
    def __init__(self):
        self.rows = []

    def log_trade(self, **kwargs):
        row = {col: kwargs.get(col) for col in JOURNAL_COLUMNS}
        ts = kwargs.get('timestamp')
        if ts is not None:
            row['hour_of_day'] = ts.hour
            row['day_of_week'] = ts.strftime('%A')
        self.rows.append(row)

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=JOURNAL_COLUMNS)

    def summary_stats(self) -> dict:
        df = self.to_dataframe()
        if df.empty:
            return {}
        closed = df[df['outcome'].isin(['win', 'loss'])]
        if closed.empty:
            return {}
        wins = closed[closed['outcome'] == 'win']
        losses = closed[closed['outcome'] == 'loss']
        win_rate = len(wins) / len(closed)
        avg_rr = closed['realized_rr'].mean()
        gross_profit = wins['pnl'].sum()
        gross_loss = abs(losses['pnl'].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        return {
            'total_trades': len(closed),
            'win_rate': win_rate,
            'avg_rr': avg_rr,
            'profit_factor': profit_factor,
            'net_pnl': closed['pnl'].sum(),
        }


if __name__ == "__main__":
    rm = RiskManager(initial_balance=10000)
    journal = TradeJournal()

    t1 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
    check = rm.can_trade(t1)
    print(f"Can trade: {check}")

    entry, stop = 100.0, 98.0
    size = rm.position_size(entry, stop)
    print(f"Risk pct: {rm.effective_risk_pct()}, position size: {size:.2f}")

    rm.record_trade_result(t1, pnl=-100)  # simulate a loss
    journal.log_trade(
        trade_id=1, timestamp=t1, pair='BTCUSDT', direction='long',
        entry_model='VSSR', htf_poi_tf='4H', poi_id='poi_1',
        entry_price=entry, stop_price=stop, take_profit=103,
        stop_distance=2.0, planned_rr=1.5, realized_rr=-1.0,
        risk_pct_used=rm.effective_risk_pct(), position_size=size,
        outcome='loss', pnl=-100, balance_after=rm.balance, notes='test trade',
    )

    print(f"Balance after: {rm.balance}")
    print(f"Drawdown pct: {rm.current_drawdown_pct():.4f}")
    print(f"Journal summary: {journal.summary_stats()}")

    poi_id = 'poi_1'
    print(f"Can enter POI (attempt 1): {rm.can_enter_poi(poi_id)}")
    rm.register_poi_attempt(poi_id)
    rm.register_poi_attempt(poi_id)
    print(f"Can enter POI (after 2 attempts): {rm.can_enter_poi(poi_id)}")
