"""
Core domain models for the simulated prop firm engine.

Design notes:
- All money math uses Decimal. Floats are never used for balances/PnL.
- Models are storage-agnostic; persistence is handled by a repository layer
  so this swaps cleanly from in-memory (dev) to Postgres (Railway prod).
- Default instrument is BTC/USDT per house convention.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"          # stop-market trigger


class OrderStatus(str, Enum):
    PENDING = "pending"     # resting limit/stop
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ChallengePhase(str, Enum):
    PHASE_1 = "phase_1"
    PHASE_2 = "phase_2"
    FUNDED = "funded"
    FAILED = "failed"
    PASSED = "passed"       # terminal state for single-phase products


class BreachReason(str, Enum):
    DAILY_LOSS = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    TRAILING_DRAWDOWN = "trailing_drawdown"
    LIQUIDATION = "liquidation"
    PROHIBITED_BEHAVIOR = "prohibited_behavior"


# ---------------------------------------------------------------------------
# Challenge configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChallengeConfig:
    """Parameters that define one challenge product (e.g. '$50K 2-Step')."""

    name: str
    starting_balance: Decimal

    # Targets, as fraction of starting balance (0.10 == 10%)
    phase1_profit_target: Decimal = Decimal("0.10")
    phase2_profit_target: Decimal = Decimal("0.05")

    # Loss limits, as fraction of starting balance
    daily_loss_limit: Decimal = Decimal("0.04")     # 4% of start, per day
    max_drawdown: Decimal = Decimal("0.08")          # 8% static from start
    trailing_drawdown: Optional[Decimal] = None      # if set, trails equity HWM

    # Discipline rules
    min_trading_days_phase1: int = 3
    min_trading_days_phase2: int = 3
    max_leverage: Decimal = Decimal("10")
    # Consistency: no single day may exceed this fraction of total profit
    consistency_cap: Optional[Decimal] = Decimal("0.40")

    two_step: bool = True

    # Funded-account economics
    profit_split: Decimal = Decimal("0.80")

    def target_for(self, phase: ChallengePhase) -> Decimal:
        if phase == ChallengePhase.PHASE_1:
            return self.phase1_profit_target
        if phase == ChallengePhase.PHASE_2:
            return self.phase2_profit_target
        return Decimal("0")


# ---------------------------------------------------------------------------
# Orders & positions
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Order:
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal                       # base-asset quantity (e.g. BTC)
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False

    id: str = field(default_factory=_new_id)
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    fill_price: Optional[Decimal] = None
    reject_reason: Optional[str] = None


@dataclass
class Position:
    account_id: str
    symbol: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    leverage: Decimal

    id: str = field(default_factory=_new_id)
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    exit_price: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def notional(self) -> Decimal:
        return self.qty * self.entry_price

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        if not self.is_open:
            return Decimal("0")
        direction = Decimal("1") if self.side == Side.LONG else Decimal("-1")
        return (mark - self.entry_price) * self.qty * direction


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@dataclass
class Account:
    """A single challenge attempt's trading account."""

    config: ChallengeConfig
    user_id: str

    id: str = field(default_factory=_new_id)
    phase: ChallengePhase = ChallengePhase.PHASE_1
    balance: Decimal = field(init=False)            # realized cash
    created_at: float = field(default_factory=time.time)

    # Risk-tracking state
    equity_high_water_mark: Decimal = field(init=False)
    day_start_equity: Decimal = field(init=False)
    current_day: Optional[str] = None               # 'YYYY-MM-DD' UTC
    trading_days: set = field(default_factory=set)  # days with >=1 fill
    daily_realized: dict = field(default_factory=dict)  # day -> realized pnl

    breached: bool = False
    breach_reason: Optional[BreachReason] = None
    breach_detail: Optional[str] = None

    def __post_init__(self) -> None:
        self.balance = self.config.starting_balance
        self.equity_high_water_mark = self.config.starting_balance
        self.day_start_equity = self.config.starting_balance

    @property
    def is_active(self) -> bool:
        return not self.breached and self.phase not in (
            ChallengePhase.FAILED,
            ChallengePhase.PASSED,
        )
