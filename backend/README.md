# ATLAS Backend — FastAPI Execution Middleware

Layers 1–5 and 8 of [`../IMPLEMENTATION_ARTIFACT.md`](../IMPLEMENTATION_ARTIFACT.md).
Ingests TradingView alerts, holds them for human approval, and routes approved
orders to Tradovate (CME) or Kraken (margin crypto).

## Layout

```
app/
├── main.py              app factory, lifespan, WebSocket hub (/ws/dashboard)
├── core/
│   ├── config.py        pydantic-settings — single source of env truth
│   ├── security.py      HMAC-SHA256 webhook verify + constant-time token compare
│   ├── redis_client.py  async pool, 60s TTL helpers, kill-switch flag
│   ├── db.py            async SQLAlchemy engine/session
│   ├── ledger.py        audit ledger writes (degrades when Postgres is absent)
│   ├── risk_engine.py   sizing, contract clamp, drawdown lockout
│   └── state_machine.py PENDING_APPROVAL → CONFIRMED → ROUTED → FILLED …
├── brokers/
│   ├── tradovate_client.py  OAuth2 bearer + /order/placeorder bracket
│   └── kraken_client.py     HMAC-SHA512 + /0/private/AddOrder with leverage
├── push/fcm.py          FCM HTTP v1 actionable notifications
├── api/v1/              webhooks · confirmation · emergency · health
├── agents/              regime · ideation · backtest · orchestrator
└── models/              schemas · strategy_dsl · tables
```

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Or the full stack from the repo root: `docker compose up --build`.

## Test

No Docker, Redis, PostgreSQL or network required — Redis is replaced by an
in-memory stub and `ATLAS_MOCK_BROKERS=true` is forced in `tests/conftest.py`.

```bash
cd backend && python -m pytest tests -q
```

| Suite | Gate | Covers |
|---|---|---|
| `test_kraken_signature.py` | §6.2 | Canonical Kraken reference vector, byte-for-byte; AddOrder margin fields |
| `test_tradovate_bracket.py` | §6.1 | Spec §4.1 key set, `Buy`/`Sell` casing, `isAutomated`, nested bracket, Bearer header |
| `test_webhook_flow.py` | §6.3 | HMAC + token auth, tampered-body reject, TTL hold, **zero broker calls on expiry** |
| `test_risk_engine.py` | §6.4 | Tick geometry, floor-never-round sizing, clamps, drawdown lockout, guardrail ordering |

## Mock mode

`ATLAS_MOCK_BROKERS=true` makes both broker clients build **and sign** real
payloads, then short-circuit the HTTP call and return a deterministic mock
response. The signing path is genuinely exercised — that is what the acceptance
tests assert against. Set it to `false` only against demo/paper credentials
first.

## Endpoints

See §4 of the implementation artifact for the frozen REST contract. Interactive
docs at `/docs` once running.
