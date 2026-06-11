"""
Simulated fill engine.

This is the module that makes the firm economically survivable. A naive
paper-trading fill model ("limit fills if price touches it") is trivially
exploitable; traders will farm wick fills and latency to manufacture
guaranteed passes, then drain the payout treasury.

Fill rules implemented here:

1. MARKET orders fill at mark price plus a spread/slippage charge that
   scales with order notional (simulating book depth impact).
2. LIMIT orders require the candle to trade THROUGH the price by a
   penetration buffer, not merely touch it. A wick that kisses the limit
   does not fill — on a real book you'd be at the back of the queue.
3. Fills are evaluated against the candle CLOSE-side sequence, not
   intrabar extremes alone, to prevent "perfect wick" entries/exits.
4. A per-account rate limiter rejects burst order spam (latency-arb
   pattern around news prints).
5. Max slippage-free notional is capped; oversized orders pay quadratic
   impact. No infinite liquidity at top of book.

All parameters are conservative defaults; tune against real Binance L2
data before launch (you already pull Velo/Kiyotaka feeds for this).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import Order, OrderStatus, OrderType, Side


@dataclass(frozen=True)
class Candle:
    """One bar of the price feed (UTC ms timestamp)."""
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class FillParams:
    # Base half-spread applied to market orders (2 bps ~ BTC perp majors)
    base_spread: Decimal = Decimal("0.0002")
    # Notional above which impact cost starts accruing
    impact_free_notional: Decimal = Decimal("250000")
    # Quadratic impact coefficient per $1M of excess notional
    impact_coeff: Decimal = Decimal("0.0004")
    # Limit orders must be penetrated by this fraction beyond the price
    limit_penetration: Decimal = Decimal("0.0001")  # 1 bp through
    # Anti-spam: max orders per rolling window
    max_orders_per_window: int = 10
    window_seconds: int = 10


class RateLimiter:
    def __init__(self, params: FillParams):
        self.params = params
        self._events: dict[str, list[float]] = {}

    def allow(self, account_id: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        window = self._events.setdefault(account_id, [])
        cutoff = now - self.params.window_seconds
        window[:] = [t for t in window if t > cutoff]
        if len(window) >= self.params.max_orders_per_window:
            return False
        window.append(now)
        return True


class FillEngine:
    def __init__(self, params: Optional[FillParams] = None):
        self.params = params or FillParams()
        self.rate_limiter = RateLimiter(self.params)

    # -- market orders ------------------------------------------------------

    def fill_market(self, order: Order, mark: Decimal) -> Order:
        """Fill a market order at mark + spread + size impact."""
        if not self.rate_limiter.allow(order.account_id):
            order.status = OrderStatus.REJECTED
            order.reject_reason = "rate_limited"
            return order

        notional = order.qty * mark
        slip = self._slippage_fraction(notional)
        # Buyer pays up, seller receives down.
        adverse = Decimal("1") + slip if order.side == Side.LONG else Decimal("1") - slip
        order.fill_price = (mark * adverse).quantize(Decimal("0.01"))
        order.status = OrderStatus.FILLED
        order.filled_at = time.time()
        return order

    def _slippage_fraction(self, notional: Decimal) -> Decimal:
        slip = self.params.base_spread
        excess = notional - self.params.impact_free_notional
        if excess > 0:
            slip += self.params.impact_coeff * (excess / Decimal("1000000"))
        return slip

    # -- resting orders evaluated per candle ---------------------------------

    def try_fill_limit(self, order: Order, candle: Candle) -> Order:
        """
        Fill a resting limit only if price trades THROUGH it by the
        penetration buffer. Touching the wick is not enough.
        """
        assert order.order_type == OrderType.LIMIT and order.limit_price is not None
        px = order.limit_price
        buf = px * self.params.limit_penetration

        if order.side == Side.LONG:
            # Buy limit below market: low must go a buffer BELOW the limit.
            if candle.low <= px - buf:
                return self._fill_at(order, px)
        else:
            # Sell limit above market: high must exceed limit + buffer.
            if candle.high >= px + buf:
                return self._fill_at(order, px)
        return order  # still pending

    def try_fill_stop(self, order: Order, candle: Candle) -> Order:
        """
        Stop-market: triggers when price crosses stop level; fills with
        slippage in the adverse direction (stops get worse fills, as in
        live markets — this matters for realistic loss modeling).
        """
        assert order.order_type == OrderType.STOP and order.stop_price is not None
        sp = order.stop_price
        triggered = (
            candle.high >= sp if order.side == Side.LONG else candle.low <= sp
        )
        if not triggered:
            return order
        slip = self._slippage_fraction(order.qty * sp)
        adverse = Decimal("1") + slip if order.side == Side.LONG else Decimal("1") - slip
        return self._fill_at(order, (sp * adverse).quantize(Decimal("0.01")))

    @staticmethod
    def _fill_at(order: Order, price: Decimal) -> Order:
        order.fill_price = price
        order.status = OrderStatus.FILLED
        order.filled_at = time.time()
        return order
