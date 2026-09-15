/**
 * TypeScript mirrors of the backend pydantic schemas.
 *
 * Source of truth: IMPLEMENTATION_ARTIFACT.md
 *   §3 FROZEN CONTRACT A — TradingView webhook payload
 *   §4 FROZEN CONTRACT B — Backend REST surface
 *   §5 State machine
 *
 * Nothing in this file may widen to `any`.
 */

// ── Enumerations (FROZEN CONTRACT A) ────────────────────────────────────────

/** Execution venue. `venue` ∈ TRADOVATE | KRAKEN. */
export type Venue = 'TRADOVATE' | 'KRAKEN';

/** Order side. `action` ∈ BUY | SELL. */
export type Action = 'BUY' | 'SELL';

/** 4-state market regime emitted by Atlas_Regime_Filter / the regime agent. */
export type Regime =
  | 'BULL_TREND_EXPANSION'
  | 'BEAR_TREND_EXPANSION'
  | 'RANGE_COMPRESSION'
  | 'VOLATILITY_CHOP';

/**
 * HITL state machine (§5).
 *
 *   PENDING_APPROVAL ──confirm──▶ CONFIRMED ──risk ok──▶ ROUTED ──fill──▶ FILLED
 *                     ──reject───▶ REJECTED
 *                     ──60s TTL──▶ EXPIRED           (zero broker interaction)
 *                                  CONFIRMED ──risk breach──▶ REJECTED_RISK
 */
export type SignalStatus =
  | 'PENDING_APPROVAL'
  | 'CONFIRMED'
  | 'ROUTED'
  | 'FILLED'
  | 'REJECTED'
  | 'REJECTED_RISK'
  | 'EXPIRED';

export const REGIMES: readonly Regime[] = [
  'BULL_TREND_EXPANSION',
  'BEAR_TREND_EXPANSION',
  'RANGE_COMPRESSION',
  'VOLATILITY_CHOP',
] as const;

/** Statuses that mean the setup is finished and must leave the pending list. */
export const TERMINAL_STATUSES: readonly SignalStatus[] = [
  'ROUTED',
  'FILLED',
  'REJECTED',
  'REJECTED_RISK',
  'EXPIRED',
] as const;

export function isTerminalStatus(status: SignalStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

// ── Confirmation surface ────────────────────────────────────────────────────

/**
 * A validated TradingView alert held in Redis under a 60-second TTL, awaiting a
 * 1-tap human decision.
 *
 * Returned by `GET /api/v1/confirmation/pending` and
 * `GET /api/v1/confirmation/{signal_id}`.
 */
export interface PendingSetup {
  /** Redis key / audit-ledger primary key. */
  signal_id: string;
  symbol: string;
  venue: Venue;
  action: Action;
  limit_price: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  regime: Regime;
  /** Percent of account equity risked on this trade, e.g. 1.0 = 1%. */
  risk_pct: number;
  status: SignalStatus;
  /** ISO-8601 UTC. */
  created_at: string;
  /** ISO-8601 UTC — `created_at` + the 60s Redis TTL. */
  expires_at: string;

  /** Position size the risk engine sized, when the backend has computed it. */
  quantity?: number | null;
  /** Account equity snapshot used for sizing; drives the dollar-risk readout. */
  account_equity?: number | null;
  /** Strategy that produced the alert, when the roster tagged it. */
  strategy_id?: string | null;
}

/** Body of `POST /api/v1/confirmation/{signal_id}/confirm`. */
export interface ConfirmRequest {
  venue: Venue;
}

/** Optional body of `POST /api/v1/confirmation/{signal_id}/reject`. */
export interface RejectRequest {
  reason?: string;
}

/**
 * Result of a routing decision — the broker round-trip outcome, or the
 * terminal state the signal was moved to.
 */
export interface OrderResult {
  signal_id: string;
  status: SignalStatus;
  venue: Venue;
  /** Broker-assigned id (Tradovate orderId / Kraken txid), null when not routed. */
  broker_order_id: string | null;
  filled_qty: number | null;
  avg_fill_price: number | null;
  /** Human-readable outcome, e.g. a risk-engine rejection reason. */
  message: string | null;
  /** Deterministic mock response when ATLAS_MOCK_BROKERS=true. */
  mock?: boolean;
}

// ── Emergency surface ───────────────────────────────────────────────────────

/** `GET /api/v1/emergency/status`. */
export interface KillSwitchStatus {
  /** True while the global kill switch is engaged; all routing is blocked. */
  engaged: boolean;
  /** ISO-8601 UTC, null when released. */
  engaged_at: string | null;
  reason: string | null;
  /** Realised drawdown today, as a positive percent. */
  daily_drawdown_pct: number;
  /** Configured lockout threshold, as a positive percent. */
  daily_drawdown_limit_pct: number;
  /** True when the drawdown lockout has independently halted trading. */
  lockout: boolean;
  open_positions?: number | null;
}

/** Result of `POST /api/v1/emergency/flatten`. */
export interface FlattenResult {
  cancelled_orders: number;
  flattened_positions: number;
  venues: Venue[];
  errors: string[];
}

// ── Health ──────────────────────────────────────────────────────────────────

export interface HealthStatus {
  status: 'ok' | 'degraded' | 'down';
  version?: string;
  redis?: boolean;
  database?: boolean;
  mock_brokers?: boolean;
}

// ── Live dashboard stream (`WS /ws/dashboard`) ──────────────────────────────

export type Timeframe = '5m' | '1h' | 'D';

/** One timeframe cell of the regime radar. */
export interface RegimeCell {
  timeframe: Timeframe;
  regime: Regime;
  /** Average Directional Index. */
  adx: number;
  /** Average True Range, in instrument price units. */
  atr: number;
  /** ATR expressed as a percentile of its own lookback distribution, 0..100. */
  atr_percentile?: number | null;
  /** Cumulative volume delta slope, when the order-flow feed is present. */
  cvd_slope?: number | null;
  /** ISO-8601 UTC of the bar this cell was computed from. */
  updated_at: string;
}

/** Full regime snapshot across the three mandated timeframes. */
export interface RegimeSnapshot {
  symbol: string;
  cells: RegimeCell[];
  updated_at: string;
}

/** Discriminated envelope pushed over `/ws/dashboard`. */
export type DashboardEvent =
  | { type: 'snapshot'; setups: PendingSetup[]; regime: RegimeSnapshot | null; kill_switch: KillSwitchStatus | null }
  | { type: 'setup_pending'; setup: PendingSetup }
  | { type: 'setup_resolved'; signal_id: string; status: SignalStatus }
  | { type: 'regime'; regime: RegimeSnapshot }
  | { type: 'kill_switch'; kill_switch: KillSwitchStatus }
  | { type: 'heartbeat'; ts: string };

export type DashboardEventType = DashboardEvent['type'];

/** Connection state surfaced by the socket client to the status bar. */
export type SocketState = 'connecting' | 'open' | 'reconnecting' | 'closed';

// ── Push registration (outside FROZEN CONTRACT B) ───────────────────────────

export type PushPlatform = 'android' | 'web';

export interface DeviceRegistration {
  token: string;
  platform: PushPlatform;
  app_id: string;
}
