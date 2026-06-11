# PropFirm Engine v0.1

Simulated crypto prop firm engine. No real execution, no custody, no exchange routing — a realistic simulated fill environment wrapped in challenge/risk logic. Built on the same stack as your other services (Python / FastAPI / Railway / Postgres-ready).

## What's built and tested (9/9 passing)

**Fill engine** (`engine/fill_engine.py`) — the abuse-resistance layer:
- Market orders pay spread + size-scaled impact (no infinite top-of-book liquidity)
- Limit orders require price to trade *through* the level by a penetration buffer — wick touches do not fill (kills wick-farming)
- Stop-markets fill with adverse slippage, like real stops
- Per-account order rate limiting (kills latency-arb spam patterns)

**Risk engine** (`engine/risk_engine.py`):
- Equity-based (balance + unrealized) breach checks on every candle, not just on close
- Daily loss limit (UTC day rollover), static max drawdown, optional trailing drawdown vs. high-water mark
- Profit-target pass evaluation gated by min trading days + consistency cap (no one-lucky-punt passes)

**Orchestrator** (`engine/engine.py`):
- Position ledger with netting, partial closes, realized/unrealized PnL (all Decimal, no float money)
- Gross leverage cap enforcement at order entry
- Breach -> force-flatten + cancel all + lock account
- Phase state machine: PHASE_1 → PHASE_2 → FUNDED / FAILED
- `Store` is in-memory with a repository-shaped interface — swap for Postgres without touching trading logic

**API** (`api.py`):
- Trader: create account, place/cancel orders, account state
- Internal: candle ingest endpoint (point your feed worker at it)
- Admin: list all accounts (key-gated)
- Product catalog stub (25K/50K/100K) ready to map to Whop products

## Run it

```bash
pip install fastapi uvicorn pydantic pytest
pytest tests/ -v                 # prove the engine
uvicorn api:app --reload         # local API
```

## Deploy (Railway — same pattern as your copy bot)

1. New Railway service, `uvicorn api:app --host 0.0.0.0 --port $PORT`
2. Env vars: `ADMIN_API_KEY`, `INTERNAL_API_KEY`
3. Feed worker: subscribe Binance/Bybit 1m kline websocket → POST closed candles to `/internal/candle` (reuse your Kiyotaka bot's ws pattern)
4. Add Railway Postgres; implement `Store` against it (accounts, positions, orders, fills tables)

## Roadmap to launchable

| Phase | What | Status |
|---|---|---|
| 1 | Core engine + risk + API | **done (this)** |
| 2 | Postgres persistence + live Binance feed worker | next |
| 3 | Trader dashboard (TradingView Lightweight Charts + account panel) | |
| 4 | Whop/Stripe checkout → auto account provisioning webhook | |
| 5 | Sumsub KYC at payout, payout request flow + admin review | |
| 6 | Fill-model calibration vs. real L2 data; anti-exploit hardening pass | |
| 7 | Legal/entity structure, ToS, simulated-trading disclosures | **before any sales** |

## Honest gaps (do not launch without)

- Persistence: state is in-memory; restart = wipe. Postgres first.
- Fill params are conservative defaults — calibrate against real Binance depth before money touches this.
- Single-process; fine to thousands of accounts, but no HA story yet.
- Timestamps for fills use server time; derive from feed time in prod.
- No funded-stage payout ledger yet (profit split math is in config, flow isn't built).
- Everything in Roadmap 7. The code is the easy part of the risk.
