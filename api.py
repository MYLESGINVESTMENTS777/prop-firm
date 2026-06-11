"""
Prop firm engine API. Deploys to Railway exactly like your copy-trading
bot and Myles G AI Bot: uvicorn entrypoint, env-var config, Postgres
swap-in via the Store interface.

Run locally:  uvicorn api:app --reload
Railway:      uvicorn api:app --host 0.0.0.0 --port $PORT

Feed wiring: run a worker that subscribes to Binance/Bybit websocket
klines and POSTs each closed 1m candle to /internal/candle (or call
engine.on_candle in-process). You already have this pattern from the
Kiyotaka alert bot.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from models import (
    Account, ChallengeConfig, ChallengePhase, Order, OrderType,
    OrderStatus, Position, Side, BreachReason,
)
from fill_engine import Candle, FillEngine, FillParams
from risk_engine import RiskEngine
from engine import TradingEngine

app = FastAPI(title="PropFirm Engine", version="0.1.0")
engine = TradingEngine()

ADMIN_KEY = os.environ.get("ADMIN_API_KEY", "dev-admin-key")
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "dev-internal-key")

# Product catalog — in prod this comes from Postgres / Whop product mapping
PRODUCTS: dict[str, ChallengeConfig] = {
    "25k": ChallengeConfig(name="25K 2-Step", starting_balance=Decimal("25000")),
    "50k": ChallengeConfig(name="50K 2-Step", starting_balance=Decimal("50000")),
    "100k": ChallengeConfig(name="100K 2-Step", starting_balance=Decimal("100000")),
}


# -- schemas -----------------------------------------------------------------

class CreateAccountReq(BaseModel):
    user_id: str
    product: str = Field(description="Product key, e.g. '50k'")


class PlaceOrderReq(BaseModel):
    account_id: str
    symbol: str = "BTC/USDT"
    side: Side
    order_type: OrderType = OrderType.MARKET
    qty: Decimal
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False


class CandleReq(BaseModel):
    symbol: str
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


# -- trader endpoints -----------------------------------------------------------

@app.post("/accounts")
def create_account(req: CreateAccountReq):
    config = PRODUCTS.get(req.product)
    if config is None:
        raise HTTPException(404, f"unknown product '{req.product}'")
    acct = engine.create_account(req.user_id, config)
    return {"account_id": acct.id, "product": config.name, "phase": acct.phase.value}


@app.get("/accounts/{account_id}")
def account_state(account_id: str):
    if account_id not in engine.store.accounts:
        raise HTTPException(404, "account not found")
    return engine.account_state(account_id)


@app.post("/orders")
def place_order(req: PlaceOrderReq):
    if req.account_id not in engine.store.accounts:
        raise HTTPException(404, "account not found")
    if req.order_type == OrderType.LIMIT and req.limit_price is None:
        raise HTTPException(422, "limit_price required for limit orders")
    if req.order_type == OrderType.STOP and req.stop_price is None:
        raise HTTPException(422, "stop_price required for stop orders")

    order = Order(
        account_id=req.account_id, symbol=req.symbol, side=req.side,
        order_type=req.order_type, qty=req.qty,
        limit_price=req.limit_price, stop_price=req.stop_price,
        reduce_only=req.reduce_only,
    )
    order = engine.place_order(order)
    return {
        "order_id": order.id,
        "status": order.status.value,
        "fill_price": str(order.fill_price) if order.fill_price else None,
        "reject_reason": order.reject_reason,
    }


@app.delete("/orders/{order_id}")
def cancel_order(order_id: str):
    if not engine.cancel_order(order_id):
        raise HTTPException(404, "order not found or not pending")
    return {"cancelled": order_id}


# -- internal: feed ingest ------------------------------------------------------

@app.post("/internal/candle")
def ingest_candle(req: CandleReq, x_internal_key: str = Header(default="")):
    if x_internal_key != INTERNAL_KEY:
        raise HTTPException(401, "bad internal key")
    engine.on_candle(req.symbol, Candle(
        ts=req.ts, open=req.open, high=req.high,
        low=req.low, close=req.close, volume=req.volume,
    ))
    return {"ok": True, "mark": str(engine.marks[req.symbol])}


# -- admin --------------------------------------------------------------------

@app.get("/admin/accounts")
def admin_accounts(x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(401, "bad admin key")
    return [engine.account_state(aid) for aid in engine.store.accounts]


@app.get("/health")
def health():
    return {"status": "ok", "accounts": len(engine.store.accounts)}
