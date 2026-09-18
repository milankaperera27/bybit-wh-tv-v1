# ATLAS — Autonomous Multi-Agent Trading System

Cross-asset algorithmic trading platform: **CME futures via Tradovate** and
**crypto margin via Kraken**, driven by a Pine Script v6 signal suite, gated by a
**60-second human-in-the-loop 1-tap confirmation** on an installable Android PWA.

Implements `docs/autonomous_trading_system_spec.md` (ATLAS architecture, 8pp).
The build contract and dependency order live in
**[`IMPLEMENTATION_ARTIFACT.md`](./IMPLEMENTATION_ARTIFACT.md)** — read that first.

---

## Execution flow

```
 Multi-agent research  ─▶  Pine Script v6 suite  ─▶  FastAPI middleware
 (regime / ideation /       (SMC + regime +           (HMAC auth, risk engine,
  validation / allocator)    webhook emitter)          Redis 60s TTL hold)
                                                              │
                                                    FCM high-priority push
                                                              ▼
                                              Android PWA — 1-tap confirmation
                                          [Confirm Tradovate][Route Kraken][Reject]
                                                              │
                                        ┌─────────────────────┴─────────────────────┐
                                        ▼                                           ▼
                            Tradovate CME gateway                        Kraken margin gateway
                          OAuth2 bearer, bracket orders                HMAC-SHA512, leveraged add
                            MES MNQ ES NQ CL GC                            BTC/USD, ETH/USD
```

No order reaches a broker without an explicit human tap inside the 60-second
window. Expiry or rejection means **zero broker interaction**.

## Repository layout

```
atlas-trading-system/
├── IMPLEMENTATION_ARTIFACT.md   Build contract: dependency order + frozen interfaces
├── docker-compose.yml           Redis + PostgreSQL + FastAPI stack
├── .env.example                 API keys (Tradovate, Kraken, FCM) and guardrails
├── docs/                        Source specification
├── backend/                     FastAPI gateway, brokers, agents, push engine, tests
├── indicators/                  Pine Script v6 modular SMC + regime suite
└── mobile-dashboard/            Capacitor + React PWA (HITL confirmation cards)
```

## Quick start

```bash
cp .env.example .env            # fill in Tradovate / Kraken / FCM credentials
docker compose up -d redis postgres
docker compose up --build backend
curl localhost:8000/api/v1/health
```

Backend tests (no infrastructure required — brokers run in mock mode):

```bash
cd backend && pip install -r requirements.txt && python -m pytest tests -q
```

Mobile dashboard:

```bash
cd mobile-dashboard && npm install && npm run dev      # PWA in the browser
npm run build && npx cap add android && npx cap sync   # Android package
```

Indicators: paste each `indicators/*.pine` into the TradingView Pine editor,
set **Webhook Token** to `ATLAS_WEBHOOK_TOKEN`, and point the alert webhook at
`https://<host>/api/v1/webhooks/tradingview`. See `indicators/README.md`.

## Safety posture

| Guardrail | Where |
|---|---|
| Webhook HMAC-SHA256 + shared-token auth | `backend/app/core/security.py` |
| Per-trade risk %, max contracts, daily drawdown lockout | `backend/app/core/risk_engine.py` |
| 60-second TTL confirmation hold | Redis, `ATLAS_CONFIRMATION_TTL_SECONDS` |
| Mandatory human tap before routing | `backend/app/api/v1/confirmation.py` |
| Universal kill switch + flatten-all | `backend/app/api/v1/emergency.py` |
| Mock broker mode (signs payloads, no network) | `ATLAS_MOCK_BROKERS=true` |

> **Trading risk.** Leveraged futures and margin crypto can lose more than the
> deposited capital. Run against Tradovate demo and Kraken paper credentials with
> `ATLAS_MOCK_BROKERS=true` until you have independently validated every payload
> path. Nothing here is financial advice.
