"""
Trading engine orchestrator: ties the fill engine, position ledger and
risk engine together. This is the layer the API talks to.

One TradingEngine per process; accounts are looked up via the store.
Persistence is a plain dict here — swap `Store` for a Postgres-backed
repository (same interface) for Railway prod.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from .fill_engine import Candle, FillEngine
from .models import (
    Account,
    BreachReason,
    ChallengeConfig,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from .risk_engine import RiskEngine, utc_day


class Store:
    """In-memory store. Replace with Postgres repo in prod (same methods)."""

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.positions: dict[str, Position] = {}
        self.pending_orders: dict[str, Order] = {}
        self.fills: list[Order] = []

    def open_positions(self, account_id: str) -> list[Position]:
        return [
            p for p in self.positions.values()
            if p.account_id == account_id and p.is_open
        ]

    def account_pending(self, account_id: str) -> list[Order]:
        return [o for o in self.pending_orders.values() if o.account_id == account_id]


class TradingEngine:
    def __init__(self, fill_engine: Optional[FillEngine] = None):
        self.store = Store()
        self.fills = fill_engine or FillEngine()
        self.risk = RiskEngine()
        self.marks: dict[str, Decimal] = {}  # symbol -> last mark

    # -- lifecycle -----------------------------------------------------------

    def create_account(self, user_id: str, config: ChallengeConfig) -> Account:
        acct = Account(config=config, user_id=user_id)
        self.store.accounts[acct.id] = acct
        return acct

    # -- order entry -----------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        acct = self.store.accounts[order.account_id]
        if not acct.is_active:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"account_inactive:{acct.phase.value}"
            return order

        mark = self.marks.get(order.symbol)
        if mark is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "no_mark_price"
            return order

        # Leverage / margin check on opening orders
        if not order.reduce_only:
            err = self._margin_check(acct, order, mark)
            if err:
                order.status = OrderStatus.REJECTED
                order.reject_reason = err
                return order

        if order.order_type == OrderType.MARKET:
            order = self.fills.fill_market(order, mark)
            if order.status == OrderStatus.FILLED:
                self._apply_fill(acct, order)
        else:
            self.store.pending_orders[order.id] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        order = self.store.pending_orders.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELLED
            return True
        return False

    def _margin_check(self, acct: Account, order: Order, mark: Decimal) -> Optional[str]:
        ref_price = order.limit_price or order.stop_price or mark
        new_notional = order.qty * ref_price
        open_notional = sum(
            (p.notional for p in self.store.open_positions(acct.id)),
            Decimal("0"),
        )
        unrealized = self._unrealized(acct.id)
        equity = self.risk.equity(acct, unrealized)
        if equity <= 0:
            return "no_equity"
        gross_leverage = (open_notional + new_notional) / equity
        if gross_leverage > acct.config.max_leverage:
            return f"max_leverage_exceeded:{gross_leverage:.2f}x"
        return None

    # -- market data path -------------------------------------------------------

    def on_candle(self, symbol: str, candle: Candle) -> None:
        """
        Main heartbeat. Called per candle of the live feed:
        1. update mark, 2. evaluate resting orders, 3. risk-check every
        active account with exposure to the symbol.
        """
        self.marks[symbol] = candle.close
        now = candle.ts / 1000

        # Resting orders
        for order in list(self.store.pending_orders.values()):
            if order.symbol != symbol:
                continue
            acct = self.store.accounts[order.account_id]
            if not acct.is_active:
                order.status = OrderStatus.CANCELLED
                del self.store.pending_orders[order.id]
                continue
            if order.order_type == OrderType.LIMIT:
                order = self.fills.try_fill_limit(order, candle)
            elif order.order_type == OrderType.STOP:
                order = self.fills.try_fill_stop(order, candle)
            if order.status == OrderStatus.FILLED:
                del self.store.pending_orders[order.id]
                self._apply_fill(acct, order)

        # Risk sweep — equity-based, so unrealized losses count
        for acct in self.store.accounts.values():
            if not acct.is_active:
                continue
            unrealized = self._unrealized(acct.id)
            breach = self.risk.check(acct, unrealized, now)
            if breach:
                self._force_close_all(acct, reason=breach)

    # -- fills & positions ---------------------------------------------------------

    def _apply_fill(self, acct: Account, order: Order) -> None:
        self.store.fills.append(order)
        day = utc_day(order.filled_at or time.time())
        acct.trading_days.add(day)

        open_pos = [
            p for p in self.store.open_positions(acct.id)
            if p.symbol == order.symbol
        ]
        opposite = [p for p in open_pos if p.side != order.side]

        remaining = order.qty
        # Net against opposing positions first (close/reduce)
        for pos in opposite:
            if remaining <= 0:
                break
            close_qty = min(pos.qty, remaining)
            self._close_portion(acct, pos, close_qty, order.fill_price, day)
            remaining -= close_qty

        # Any remainder opens/adds in the order's direction
        if remaining > 0 and not order.reduce_only:
            pos = Position(
                account_id=acct.id,
                symbol=order.symbol,
                side=order.side,
                qty=remaining,
                entry_price=order.fill_price,
                leverage=acct.config.max_leverage,
            )
            self.store.positions[pos.id] = pos

        # Pass check after realized changes
        self.risk.evaluate_pass(acct)

    def _close_portion(
        self, acct: Account, pos: Position, qty: Decimal, price: Decimal, day: str
    ) -> None:
        direction = Decimal("1") if pos.side == Side.LONG else Decimal("-1")
        pnl = (price - pos.entry_price) * qty * direction
        acct.balance += pnl
        acct.daily_realized[day] = acct.daily_realized.get(day, Decimal("0")) + pnl

        if qty >= pos.qty:
            pos.closed_at = time.time()
            pos.exit_price = price
            pos.realized_pnl = (pos.realized_pnl or Decimal("0")) + pnl
        else:
            pos.qty -= qty
            pos.realized_pnl = (pos.realized_pnl or Decimal("0")) + pnl

    def _force_close_all(self, acct: Account, reason: BreachReason) -> None:
        """On breach: flatten everything at current marks, cancel orders."""
        for pos in self.store.open_positions(acct.id):
            mark = self.marks.get(pos.symbol, pos.entry_price)
            self._close_portion(acct, pos, pos.qty, mark, utc_day())
        for order in self.store.account_pending(acct.id):
            order.status = OrderStatus.CANCELLED
            self.store.pending_orders.pop(order.id, None)

    # -- views ----------------------------------------------------------------

    def _unrealized(self, account_id: str) -> Decimal:
        total = Decimal("0")
        for pos in self.store.open_positions(account_id):
            mark = self.marks.get(pos.symbol)
            if mark is not None:
                total += pos.unrealized_pnl(mark)
        return total

    def account_state(self, account_id: str) -> dict:
        acct = self.store.accounts[account_id]
        unrealized = self._unrealized(account_id)
        eq = self.risk.equity(acct, unrealized)
        start = acct.config.starting_balance
        return {
            "account_id": acct.id,
            "phase": acct.phase.value,
            "balance": str(acct.balance),
            "equity": str(eq),
            "unrealized_pnl": str(unrealized),
            "profit_target_equity": str(start * (Decimal("1") + acct.config.target_for(acct.phase))),
            "daily_loss_floor": str(acct.day_start_equity - start * acct.config.daily_loss_limit),
            "max_drawdown_floor": str(start * (Decimal("1") - acct.config.max_drawdown)),
            "high_water_mark": str(acct.equity_high_water_mark),
            "trading_days": sorted(acct.trading_days),
            "breached": acct.breached,
            "breach_reason": acct.breach_reason.value if acct.breach_reason else None,
            "open_positions": len(self.store.open_positions(account_id)),
        }
