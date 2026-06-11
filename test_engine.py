"""
Engine test suite. Run: pytest tests/ -v

Covers the scenarios that matter commercially:
1. Wick-touch limit orders do NOT fill (anti wick-farming).
2. Penetrated limits DO fill.
3. Market orders pay spread (long fills above mark, short below).
4. Daily loss breach triggers on UNREALIZED equity, force-flattens.
5. Max drawdown breach.
6. Leverage cap rejection.
7. Order spam rate-limiting.
8. Full happy path: phase 1 -> phase 2 -> funded.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from decimal import Decimal as D

from models import (
    Account, ChallengeConfig, ChallengePhase, Order, OrderType,
    OrderStatus, Position, Side, BreachReason,
)
from fill_engine import Candle, FillEngine, FillParams
from risk_engine import RiskEngine
from engine import TradingEngine

SYM = "BTC/USDT"
DAY_MS = 86_400_000


def cfg(**kw) -> ChallengeConfig:
    defaults = dict(
        name="50K 2-Step",
        starting_balance=D("50000"),
        phase1_profit_target=D("0.10"),
        phase2_profit_target=D("0.05"),
        daily_loss_limit=D("0.04"),
        max_drawdown=D("0.08"),
        min_trading_days_phase1=1,
        min_trading_days_phase2=1,
        consistency_cap=None,
        max_leverage=D("10"),
    )
    defaults.update(kw)
    return ChallengeConfig(**defaults)


def candle(ts, o, h, l, c) -> Candle:
    return Candle(ts=ts, open=D(o), high=D(h), low=D(l), close=D(c), volume=D("100"))


def make(account_cfg=None):
    eng = TradingEngine()
    acct = eng.create_account("myles", account_cfg or cfg())
    eng.on_candle(SYM, candle(1_700_000_000_000, "60000", "60100", "59900", "60000"))
    return eng, acct


def mkt(acct, side, qty, reduce_only=False):
    return Order(
        account_id=acct.id, symbol=SYM, side=side,
        order_type=OrderType.MARKET, qty=D(qty), reduce_only=reduce_only,
    )


# ---------------------------------------------------------------------------

def test_wick_touch_does_not_fill_limit():
    eng, acct = make()
    o = Order(account_id=acct.id, symbol=SYM, side=Side.LONG,
              order_type=OrderType.LIMIT, qty=D("0.1"), limit_price=D("59000"))
    eng.place_order(o)
    # Candle low EXACTLY touches 59000 — must NOT fill (queue realism)
    eng.on_candle(SYM, candle(1_700_000_060_000, "60000", "60050", "59000", "59800"))
    assert o.status == OrderStatus.PENDING

def test_penetrated_limit_fills():
    eng, acct = make()
    o = Order(account_id=acct.id, symbol=SYM, side=Side.LONG,
              order_type=OrderType.LIMIT, qty=D("0.1"), limit_price=D("59000"))
    eng.place_order(o)
    # Low goes well through the limit -> fills at limit price
    eng.on_candle(SYM, candle(1_700_000_060_000, "60000", "60050", "58900", "59300"))
    assert o.status == OrderStatus.FILLED
    assert o.fill_price == D("59000")

def test_market_order_pays_spread():
    eng, acct = make()
    o = eng.place_order(mkt(acct, Side.LONG, "0.1"))
    assert o.status == OrderStatus.FILLED
    assert o.fill_price > D("60000")  # long pays up
    o2 = eng.place_order(mkt(acct, Side.SHORT, "0.1", reduce_only=True))
    assert o2.fill_price < D("60000")  # short receives down

def test_daily_loss_breach_on_unrealized_equity():
    eng, acct = make()
    # 5 BTC long at ~60k = 300k notional on 50k equity (6x, under 10x cap)
    eng.place_order(mkt(acct, Side.LONG, "5"))
    # Price drops 500 -> unrealized -2500 > 2000 daily limit (4% of 50k)
    eng.on_candle(SYM, candle(1_700_000_120_000, "60000", "60000", "59400", "59450"))
    assert acct.breached
    assert acct.breach_reason == BreachReason.DAILY_LOSS
    assert acct.phase == ChallengePhase.FAILED
    # Force-flatten happened
    assert len(eng.store.open_positions(acct.id)) == 0

def test_max_drawdown_breach():
    eng, acct = make(cfg(daily_loss_limit=D("0.50"), max_drawdown=D("0.08")))
    eng.place_order(mkt(acct, Side.LONG, "5"))
    # Drop big: -4100/50k > 8%
    eng.on_candle(SYM, candle(1_700_000_120_000, "60000", "60000", "59100", "59150"))
    assert acct.breached
    assert acct.breach_reason == BreachReason.MAX_DRAWDOWN

def test_leverage_cap_rejected():
    eng, acct = make()
    # 10 BTC * 60k = 600k notional on 50k equity = 12x > 10x
    o = eng.place_order(mkt(acct, Side.LONG, "10"))
    assert o.status == OrderStatus.REJECTED
    assert "max_leverage" in o.reject_reason

def test_rate_limiter_blocks_spam():
    eng, acct = make()
    params = FillParams()
    statuses = []
    for _ in range(params.max_orders_per_window + 3):
        o = eng.place_order(mkt(acct, Side.LONG, "0.01"))
        statuses.append(o.status)
    assert OrderStatus.REJECTED in statuses

def test_full_pass_phase1_to_funded():
    eng, acct = make(cfg())
    # Phase 1: long 2 BTC, ride +2600/BTC -> +5200 realized > 10% of 50k
    eng.place_order(mkt(acct, Side.LONG, "2"))
    eng.on_candle(SYM, candle(1_700_000_000_000 + DAY_MS, "62700", "62800", "62600", "62700"))
    eng.place_order(mkt(acct, Side.SHORT, "2", reduce_only=True))
    assert acct.phase == ChallengePhase.PHASE_2
    assert acct.balance == D("50000")  # phase 2 reset

    # Phase 2 needs +5%: long 2 BTC, +1350/BTC -> +2700 > 2500
    eng.on_candle(SYM, candle(1_700_000_000_000 + 2 * DAY_MS, "60000", "60100", "59900", "60000"))
    eng.place_order(mkt(acct, Side.LONG, "2"))
    eng.on_candle(SYM, candle(1_700_000_000_000 + 3 * DAY_MS, "61400", "61500", "61300", "61400"))
    eng.place_order(mkt(acct, Side.SHORT, "2", reduce_only=True))
    assert acct.phase == ChallengePhase.FUNDED

def test_breached_account_rejects_new_orders():
    eng, acct = make()
    eng.place_order(mkt(acct, Side.LONG, "5"))
    eng.on_candle(SYM, candle(1_700_000_120_000, "60000", "60000", "59000", "59050"))
    assert acct.breached
    o = eng.place_order(mkt(acct, Side.LONG, "0.1"))
    assert o.status == OrderStatus.REJECTED
