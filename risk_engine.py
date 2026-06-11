"""
Risk engine: the rule layer that turns a paper-trading ledger into a
prop-firm challenge.

Evaluated on every mark-price update (not just on fills), because
drawdown limits in serious firms apply to EQUITY (balance + unrealized),
not balance. Evaluating only on close would let traders ride enormous
open losses invisibly.

Rules:
- Daily loss limit: equity may not fall more than X% of starting balance
  below the day-start equity snapshot (UTC day boundary).
- Max drawdown (static): equity floor at starting_balance * (1 - X).
- Trailing drawdown (optional): floor trails the equity high-water mark.
- Profit target: phase passes when equity >= start * (1 + target),
  subject to min trading days and the consistency cap.
- Consistency cap: no single day's realized PnL may exceed N% of total
  profit at evaluation time (anti one-lucky-punt rule).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from models import Account, BreachReason, ChallengePhase


def utc_day(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


class RiskEngine:
    # -- equity & day rollover ----------------------------------------------

    @staticmethod
    def equity(account: Account, unrealized: Decimal) -> Decimal:
        return account.balance + unrealized

    @staticmethod
    def roll_day_if_needed(account: Account, now_ts: float, unrealized: Decimal) -> None:
        """Snapshot day-start equity at the UTC boundary."""
        today = utc_day(now_ts)
        if account.current_day != today:
            account.current_day = today
            account.day_start_equity = account.balance + unrealized

    # -- breach checks (run on every tick) ------------------------------------

    def check(self, account: Account, unrealized: Decimal, now_ts: float) -> Optional[BreachReason]:
        if account.breached:
            return account.breach_reason

        self.roll_day_if_needed(account, now_ts, unrealized)
        eq = self.equity(account, unrealized)
        start = account.config.starting_balance

        # Track high-water mark for trailing drawdown
        if eq > account.equity_high_water_mark:
            account.equity_high_water_mark = eq

        # 1) Daily loss limit
        daily_floor = account.day_start_equity - start * account.config.daily_loss_limit
        if eq <= daily_floor:
            return self._breach(
                account, BreachReason.DAILY_LOSS,
                f"equity {eq} <= daily floor {daily_floor}",
            )

        # 2) Static max drawdown
        static_floor = start * (Decimal("1") - account.config.max_drawdown)
        if eq <= static_floor:
            return self._breach(
                account, BreachReason.MAX_DRAWDOWN,
                f"equity {eq} <= static floor {static_floor}",
            )

        # 3) Trailing drawdown (if configured)
        if account.config.trailing_drawdown is not None:
            trail_floor = account.equity_high_water_mark - start * account.config.trailing_drawdown
            if eq <= trail_floor:
                return self._breach(
                    account, BreachReason.TRAILING_DRAWDOWN,
                    f"equity {eq} <= trailing floor {trail_floor}",
                )

        return None

    @staticmethod
    def _breach(account: Account, reason: BreachReason, detail: str) -> BreachReason:
        account.breached = True
        account.breach_reason = reason
        account.breach_detail = detail
        account.phase = ChallengePhase.FAILED
        return reason

    # -- pass evaluation (run after each realized close) -----------------------

    def evaluate_pass(self, account: Account) -> bool:
        """
        Check whether the current phase's profit target is met, with
        discipline rules. Returns True if the phase was advanced.
        """
        if not account.is_active:
            return False

        cfg = account.config
        start = cfg.starting_balance
        target_equity = start * (Decimal("1") + cfg.target_for(account.phase))

        if account.balance < target_equity:
            return False

        # Min trading days
        min_days = (
            cfg.min_trading_days_phase1
            if account.phase == ChallengePhase.PHASE_1
            else cfg.min_trading_days_phase2
        )
        if len(account.trading_days) < min_days:
            return False

        # Consistency cap: best day <= cap * total profit
        if cfg.consistency_cap is not None:
            total_profit = account.balance - start
            if total_profit > 0:
                best_day = max(account.daily_realized.values(), default=Decimal("0"))
                if best_day > total_profit * cfg.consistency_cap:
                    return False  # not failed — just not passed yet

        # Advance phase
        if account.phase == ChallengePhase.PHASE_1 and cfg.two_step:
            account.phase = ChallengePhase.PHASE_2
            # Reset per-phase state for phase 2
            account.balance = start
            account.equity_high_water_mark = start
            account.day_start_equity = start
            account.trading_days = set()
            account.daily_realized = {}
        elif account.phase in (ChallengePhase.PHASE_1, ChallengePhase.PHASE_2):
            account.phase = ChallengePhase.FUNDED
        return True
