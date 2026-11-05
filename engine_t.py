"""
Code2Cash (C2C) Engine T - Institutional Swing Engine v7.5
Asset: Thailand Futures Exchange (TFEX) SET50 Index Futures (S50)
Specifications:
  - Multiplier: ฿200 THB / pt
  - Capital Tier 1: ฿800,000 THB (10 Contracts: 7 T1 Bank / 3 T2 Runner)
  - Execution Windows: 10:15-11:45 & 14:15-15:30 BKK
  - Microstructure T1: Prior Swing Pivot Wick +/- 0.2 pt front-run
  - 60-Minute Time-Stop Invariant (4 consecutive 15M bars)
  - Mandatory Stop-Market (MP) routing
  - 16:00 Intraday Flatten Rule (Zero unbracketed overnight risk)
"""

import datetime
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import numpy as np
import pandas as pd


@dataclass
class TradeBracket:
    entry_price: float
    side: str  # 'LONG' or 'SHORT'
    total_contracts: int
    t1_contracts: int
    t2_contracts: int
    t1_target: float
    hard_stop: float
    entry_bar_idx: int
    entry_timestamp: pd.Timestamp
    t1_filled: bool = False
    closed: bool = False
    exit_price: Optional[float] = None
    exit_timestamp: Optional[pd.Timestamp] = None
    exit_reason: Optional[str] = None
    realized_pts: float = 0.0
    realized_thb: float = 0.0


class Code2CashEngineT_v75:
    def __init__(
        self,
        capital_base: float = 800_000.0,
        point_multiplier: float = 200.0,
        commission_per_contract: float = 80.0,
        base_contracts: int = 10,
        t1_ratio: float = 0.70,
        max_stop_loss_pts: float = 3.5,
        time_stop_bars: int = 4  # 4 * 15M = 60 minutes
    ):
        self.capital_base = capital_base
        self.point_multiplier = point_multiplier
        self.commission_per_contract = commission_per_contract
        self.base_contracts = base_contracts
        self.t1_contracts = int(base_contracts * t1_ratio)  # 7 contracts
        self.t2_contracts = base_contracts - self.t1_contracts  # 3 contracts
        self.max_stop_loss_pts = max_stop_loss_pts
        self.time_stop_bars = time_stop_bars

        self.current_equity = capital_base
        self.active_trade: Optional[TradeBracket] = None
        self.trade_history: List[TradeBracket] = []

    # ---------------------------------------------------------
    # Execution Window Validation
    # ---------------------------------------------------------
    @staticmethod
    def is_authorized_entry_window(ts: pd.Timestamp) -> bool:
        """
        Layer D: Institutional Execution Windows
        - Morning: 10:15 - 11:45 BKK
        - Afternoon: 14:15 - 15:30 BKK
        - Blackouts: 09:45-10:15 (auction), 11:45-14:15 (lunch dead zone), >15:45 (close)
        """
        t = ts.time()
        morning_ok = datetime.time(10, 15) <= t <= datetime.time(11, 45)
        afternoon_ok = datetime.time(14, 15) <= t <= datetime.time(15, 30)
        return morning_ok or afternoon_ok

    @staticmethod
    def is_closing_flatten_window(ts: pd.Timestamp) -> bool:
        """16:00 BKK mandatory flatten check for unbracketed trades"""
        return ts.time() >= datetime.time(16, 0)

    # ---------------------------------------------------------
    # Core Indicator Calculation
    # ---------------------------------------------------------
    @staticmethod
    def calculate_indicators(df_15m: pd.DataFrame) -> pd.DataFrame:
        df = df_15m.copy()
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ma200'] = df['close'].rolling(window=200).mean()

        # RSI 14
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi14'] = 100 - (100 / (1 + rs))

        # Wicks
        df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
        df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']

        # Prior 5-bar swing pivots for microstructure front-running
        df['prior_swing_low'] = df['low'].shift(1).rolling(window=5).min()
        df['prior_swing_high'] = df['high'].shift(1).rolling(window=5).max()

        return df

    # ---------------------------------------------------------
    # Signal Verification & Order Placement
    # ---------------------------------------------------------
    def evaluate_entry(
        self,
        bar_idx: int,
        df: pd.DataFrame,
        macro_regime: str,  # 'BULLISH' or 'BEARISH' from Daily Gate
        intermarket_delta_veto: bool = False,
        basis_veto: bool = False
    ) -> Optional[TradeBracket]:
        if self.active_trade is not None:
            return None

        row = df.iloc[bar_idx]
        ts = row.name if isinstance(row.name, pd.Timestamp) else pd.to_datetime(row['timestamp'])

        if not self.is_authorized_entry_window(ts):
            return None

        if intermarket_delta_veto or basis_veto:
            return None

        # Short Entry Check
        if macro_regime == 'BEARISH':
            # MSS Trigger: Close below EMA20 and MA200, rejection upper wick > 0.2 pt, RSI < 48
            is_bearish_mss = (
                (row['close'] < row['ema20']) and
                (row['close'] < row['ma200']) and
                (row['upper_wick'] >= 0.2) and
                (row['rsi14'] < 48.0)
            )
            # Extension filter: Do not short if already over-extended > 2.0 pts below EMA9
            not_overextended = (row['ema9'] - row['close']) <= 2.0

            if is_bearish_mss and not_overextended:
                entry_price = float(row['close'])
                # Microstructure Front-Run: 0.2 pt ABOVE the prior swing low wick
                t1_target = float(row['prior_swing_low']) + 0.2
                if t1_target >= entry_price:
                    t1_target = entry_price - 3.5  # Fallback to standard minimum R:R

                # Invalidation Hard Stop: Pivot high + 0.3 pt, capped strictly at max 3.5 pts
                swing_high = float(row['prior_swing_high'])
                calc_stop = swing_high + 0.3
                hard_stop = min(calc_stop, entry_price + self.max_stop_loss_pts)

                trade = TradeBracket(
                    entry_price=entry_price,
                    side='SHORT',
                    total_contracts=self.base_contracts,
                    t1_contracts=self.t1_contracts,
                    t2_contracts=self.t2_contracts,
                    t1_target=round(t1_target, 1),
                    hard_stop=round(hard_stop, 1),
                    entry_bar_idx=bar_idx,
                    entry_timestamp=ts
                )
                self.active_trade = trade
                return trade

        # Long Entry Check
        elif macro_regime == 'BULLISH':
            is_bullish_mss = (
                (row['close'] > row['ema20']) and
                (row['close'] > row['ma200']) and
                (row['lower_wick'] >= 0.2) and
                (row['rsi14'] > 52.0)
            )
            not_overextended = (row['close'] - row['ema9']) <= 2.0

            if is_bullish_mss and not_overextended:
                entry_price = float(row['close'])
                # Microstructure Front-Run: 0.2 pt BELOW the prior swing high wick
                t1_target = float(row['prior_swing_high']) - 0.2
                if t1_target <= entry_price:
                    t1_target = entry_price + 3.5

                swing_low = float(row['prior_swing_low'])
                calc_stop = swing_low - 0.3
                hard_stop = max(calc_stop, entry_price - self.max_stop_loss_pts)

                trade = TradeBracket(
                    entry_price=entry_price,
                    side='LONG',
                    total_contracts=self.base_contracts,
                    t1_contracts=self.t1_contracts,
                    t2_contracts=self.t2_contracts,
                    t1_target=round(t1_target, 1),
                    hard_stop=round(hard_stop, 1),
                    entry_bar_idx=bar_idx,
                    entry_timestamp=ts
                )
                self.active_trade = trade
                return trade

        return None

    # ---------------------------------------------------------
    # Trade Management Lifecycle (T1, Trail, Time-Stop, Lockouts)
    # ---------------------------------------------------------
    def process_bar(self, bar_idx: int, df: pd.DataFrame) -> Optional[TradeBracket]:
        if self.active_trade is None:
            return None

        trade = self.active_trade
        row = df.iloc[bar_idx]
        ts = row.name if isinstance(row.name, pd.Timestamp) else pd.to_datetime(row['timestamp'])
        bars_held = bar_idx - trade.entry_bar_idx

        # --- 1. HARD STOP EVALUATION (Mandatory Stop-Market / MP routing) ---
        if trade.side == 'SHORT':
            if row['high'] >= trade.hard_stop:
                trade.closed = True
                trade.exit_price = trade.hard_stop
                trade.exit_timestamp = ts
                trade.exit_reason = 'STOP_MARKET_TRIGGER'
                pts = (trade.entry_price - trade.hard_stop)
                trade.realized_pts = pts
                contracts = trade.t2_contracts if trade.t1_filled else trade.total_contracts
                comm = contracts * self.commission_per_contract
                trade.realized_thb = (pts * contracts * self.point_multiplier) - comm
                self.current_equity += trade.realized_thb
                self.trade_history.append(trade)
                self.active_trade = None
                return trade
        else:  # LONG
            if row['low'] <= trade.hard_stop:
                trade.closed = True
                trade.exit_price = trade.hard_stop
                trade.exit_timestamp = ts
                trade.exit_reason = 'STOP_MARKET_TRIGGER'
                pts = (trade.hard_stop - trade.entry_price)
                trade.realized_pts = pts
                contracts = trade.t2_contracts if trade.t1_filled else trade.total_contracts
                comm = contracts * self.commission_per_contract
                trade.realized_thb = (pts * contracts * self.point_multiplier) - comm
                self.current_equity += trade.realized_thb
                self.trade_history.append(trade)
                self.active_trade = None
                return trade

        # --- 2. TRANCHE 1 PROFIT-TAKING (70% Volume Front-Run Fill) ---
        if not trade.t1_filled:
            t1_hit = (row['low'] <= trade.t1_target) if trade.side == 'SHORT' else (row['high'] >= trade.t1_target)
            if t1_hit:
                trade.t1_filled = True
                pts = (trade.entry_price - trade.t1_target) if trade.side == 'SHORT' else (trade.t1_target - trade.entry_price)
                comm = trade.t1_contracts * self.commission_per_contract
                banked_thb = (pts * trade.t1_contracts * self.point_multiplier) - comm
                trade.realized_pts += (pts * (trade.t1_contracts / trade.total_contracts))
                trade.realized_thb += banked_thb
                self.current_equity += banked_thb

                # INVARIANT: Shift hard stop on remaining contracts to ENTRY (Breakeven)
                trade.hard_stop = trade.entry_price

        # --- 3. 60-MINUTE TIME-STOP INVARIANT (4 * 15M Bars) ---
        if not trade.t1_filled and bars_held >= self.time_stop_bars:
            trade.closed = True
            trade.exit_price = float(row['close'])
            trade.exit_timestamp = ts
            trade.exit_reason = '60M_TIME_STOP_SCRATCH'
            pts = (trade.entry_price - trade.exit_price) if trade.side == 'SHORT' else (trade.exit_price - trade.entry_price)
            comm = trade.total_contracts * self.commission_per_contract
            trade.realized_pts = pts
            trade.realized_thb = (pts * trade.total_contracts * self.point_multiplier) - comm
            self.current_equity += trade.realized_thb
            self.trade_history.append(trade)
            self.active_trade = None
            return trade

        # --- 4. RUNNER TRAILING MANAGEMENT (EMA 9 Trail) ---
        if trade.t1_filled:
            if trade.side == 'SHORT':
                # Trail stop behind closed 15M EMA9
                if row['close'] > row['ema9']:
                    trade.closed = True
                    trade.exit_price = float(row['close'])
                    trade.exit_timestamp = ts
                    trade.exit_reason = 'T2_RUNNER_EMA9_TRAIL'
                    pts = (trade.entry_price - trade.exit_price)
                    comm = trade.t2_contracts * self.commission_per_contract
                    runner_thb = (pts * trade.t2_contracts * self.point_multiplier) - comm
                    trade.realized_thb += runner_thb
                    self.current_equity += runner_thb
                    self.trade_history.append(trade)
                    self.active_trade = None
                    return trade
            else:
                if row['close'] < row['ema9']:
                    trade.closed = True
                    trade.exit_price = float(row['close'])
                    trade.exit_timestamp = ts
                    trade.exit_reason = 'T2_RUNNER_EMA9_TRAIL'
                    pts = (trade.exit_price - trade.entry_price)
                    comm = trade.t2_contracts * self.commission_per_contract
                    runner_thb = (pts * trade.t2_contracts * self.point_multiplier) - comm
                    trade.realized_thb += runner_thb
                    self.current_equity += runner_thb
                    self.trade_history.append(trade)
                    self.active_trade = None
                    return trade

        # --- 5. 16:00 MANDATORY CLOSING FLATTEN (Zero Overnight Gap Risk) ---
        if self.is_closing_flatten_window(ts):
            trade.closed = True
            trade.exit_price = float(row['close'])
            trade.exit_timestamp = ts
            trade.exit_reason = '1600_PRE_CLOSE_FLATTEN'
            contracts = trade.t2_contracts if trade.t1_filled else trade.total_contracts
            pts = (trade.entry_price - trade.exit_price) if trade.side == 'SHORT' else (trade.exit_price - trade.entry_price)
            comm = contracts * self.commission_per_contract
            net_thb = (pts * contracts * self.point_multiplier) - comm
            trade.realized_thb += net_thb
            self.current_equity += net_thb
            self.trade_history.append(trade)
            self.active_trade = None
            return trade

        return None
