-- ATLAS audit ledger — mounted at /docker-entrypoint-initdb.d by docker-compose.
-- Mirrors backend/app/models/tables.py exactly.

BEGIN;

-- ── signals : every validated TradingView alert ────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           VARCHAR(64)   NOT NULL UNIQUE,
    symbol              VARCHAR(32)   NOT NULL,
    venue               VARCHAR(16)   NOT NULL,
    action              VARCHAR(8)    NOT NULL,
    regime              VARCHAR(32)   NOT NULL,
    limit_price         NUMERIC(20, 8) NOT NULL,
    stop_loss           NUMERIC(20, 8) NOT NULL,
    take_profit_1       NUMERIC(20, 8) NOT NULL,
    take_profit_2       NUMERIC(20, 8) NOT NULL,
    risk_pct            NUMERIC(8, 4)  NOT NULL,
    status              VARCHAR(24)   NOT NULL DEFAULT 'PENDING_APPROVAL',
    qty                 NUMERIC(20, 8) NOT NULL DEFAULT 0,
    risk_amount         NUMERIC(20, 8) NOT NULL DEFAULT 0,
    risk_approved       BOOLEAN       NOT NULL DEFAULT TRUE,
    risk_reason         TEXT,
    strategy_id         VARCHAR(64),
    source_ip           VARCHAR(64),
    auth_method         VARCHAR(16),
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signals_created_at ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_status     ON signals (status);
CREATE INDEX IF NOT EXISTS ix_signals_symbol     ON signals (symbol);

-- ── confirmations : the HITL tap (or the TTL expiry) ───────────────────────
CREATE TABLE IF NOT EXISTS confirmations (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           VARCHAR(64)   NOT NULL REFERENCES signals (signal_id) ON DELETE CASCADE,
    decision            VARCHAR(24)   NOT NULL,
    venue               VARCHAR(16),
    actor               VARCHAR(64)   NOT NULL DEFAULT 'mobile',
    previous_status     VARCHAR(24)   NOT NULL,
    new_status          VARCHAR(24)   NOT NULL,
    reason              TEXT,
    latency_ms          INTEGER,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_confirmations_signal_id ON confirmations (signal_id);

-- ── orders : one row per broker submission ─────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           VARCHAR(64)   NOT NULL REFERENCES signals (signal_id) ON DELETE CASCADE,
    venue               VARCHAR(16)   NOT NULL,
    broker_order_id     VARCHAR(128),
    symbol              VARCHAR(32)   NOT NULL,
    action              VARCHAR(8)    NOT NULL,
    order_type          VARCHAR(16)   NOT NULL DEFAULT 'Limit',
    qty                 NUMERIC(20, 8) NOT NULL,
    limit_price         NUMERIC(20, 8),
    stop_loss           NUMERIC(20, 8),
    take_profit         NUMERIC(20, 8),
    leverage            INTEGER,
    status              VARCHAR(24)   NOT NULL DEFAULT 'ROUTED',
    is_mock             BOOLEAN       NOT NULL DEFAULT TRUE,
    request_payload     JSONB,
    response_payload    JSONB,
    error               TEXT,
    submitted_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_orders_signal_id ON orders (signal_id);
CREATE INDEX IF NOT EXISTS ix_orders_venue     ON orders (venue);

-- ── fills : execution reports ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fills (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT        REFERENCES orders (id) ON DELETE CASCADE,
    signal_id           VARCHAR(64),
    venue               VARCHAR(16)   NOT NULL,
    broker_fill_id      VARCHAR(128),
    symbol              VARCHAR(32)   NOT NULL,
    action              VARCHAR(8)    NOT NULL,
    qty                 NUMERIC(20, 8) NOT NULL,
    price               NUMERIC(20, 8) NOT NULL,
    fee                 NUMERIC(20, 8) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(20, 8),
    is_mock             BOOLEAN       NOT NULL DEFAULT TRUE,
    filled_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_fills_order_id  ON fills (order_id);
CREATE INDEX IF NOT EXISTS ix_fills_signal_id ON fills (signal_id);

-- ── agent_runs : LAYER 5 research audit trail ──────────────────────────────
CREATE TABLE IF NOT EXISTS agent_runs (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              VARCHAR(64)   NOT NULL UNIQUE,
    agent               VARCHAR(32)   NOT NULL,
    status              VARCHAR(24)   NOT NULL DEFAULT 'COMPLETED',
    regime              VARCHAR(32),
    symbol              VARCHAR(32),
    timeframe           VARCHAR(16),
    candidates_created  INTEGER       NOT NULL DEFAULT 0,
    candidates_passed   INTEGER       NOT NULL DEFAULT 0,
    best_dsr            NUMERIC(12, 6),
    duration_ms         INTEGER,
    payload             JSONB,
    error               TEXT,
    started_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_agent_runs_agent      ON agent_runs (agent);
CREATE INDEX IF NOT EXISTS ix_agent_runs_started_at ON agent_runs (started_at DESC);

-- ── daily_pnl : the drawdown lockout ledger ────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_pnl (
    id                  BIGSERIAL PRIMARY KEY,
    trade_date          DATE          NOT NULL,
    venue               VARCHAR(16)   NOT NULL DEFAULT 'ALL',
    starting_equity     NUMERIC(20, 8) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(20, 8) NOT NULL DEFAULT 0,
    unrealized_pnl      NUMERIC(20, 8) NOT NULL DEFAULT 0,
    drawdown_pct        NUMERIC(10, 4) NOT NULL DEFAULT 0,
    trade_count         INTEGER       NOT NULL DEFAULT 0,
    win_count           INTEGER       NOT NULL DEFAULT 0,
    lockout_engaged     BOOLEAN       NOT NULL DEFAULT FALSE,
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_daily_pnl_date_venue UNIQUE (trade_date, venue)
);

CREATE INDEX IF NOT EXISTS ix_daily_pnl_trade_date ON daily_pnl (trade_date DESC);

COMMIT;
