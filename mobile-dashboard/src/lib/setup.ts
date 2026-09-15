/**
 * Derived metrics and presentation helpers for a pending setup.
 *
 * Everything here is pure: given the frozen webhook fields (entry, stop, TP1,
 * TP2, risk_pct) it produces the numbers the confirmation card shows, so the
 * card never needs an extra backend round-trip inside the 60-second window.
 */

import type { PendingSetup, Regime, SignalStatus, Venue } from '../api/types';

/** Account equity fallback when the backend payload omits `account_equity`. */
export const FALLBACK_EQUITY: number = Number(import.meta.env.VITE_ACCOUNT_EQUITY ?? '100000');

export interface SetupMetrics {
  /** Absolute distance from entry to stop, in price units. */
  riskDistance: number;
  /** Reward:risk of take_profit_1. */
  rMultipleTp1: number;
  /** Reward:risk of take_profit_2. */
  rMultipleTp2: number;
  /** Percent of equity at risk, straight from the payload. */
  riskPct: number;
  /** Equity used for the dollar figure. */
  equity: number;
  /** riskPct% of equity — the dollars on the line if the stop is hit. */
  dollarRisk: number;
  /** Dollar profit at TP1 / TP2 assuming the whole position runs. */
  dollarTargetTp1: number;
  dollarTargetTp2: number;
  /** True when the stop sits on the wrong side of entry for the action. */
  inverted: boolean;
}

/**
 * Compute the numbers the card renders.
 *
 * Note the R-multiples are computed from price geometry, not from the DSL's
 * declared ratios, so a malformed alert shows its real shape rather than the
 * shape it claims.
 */
export function computeSetupMetrics(setup: PendingSetup, equityOverride?: number): SetupMetrics {
  const entry = setup.limit_price;
  const stop = setup.stop_loss;
  const riskDistance = Math.abs(entry - stop);

  const safeDistance = riskDistance > 0 ? riskDistance : Number.NaN;
  const rMultipleTp1 = Math.abs(setup.take_profit_1 - entry) / safeDistance;
  const rMultipleTp2 = Math.abs(setup.take_profit_2 - entry) / safeDistance;

  const riskPct = setup.risk_pct;
  const equity =
    equityOverride ??
    (typeof setup.account_equity === 'number' && setup.account_equity > 0
      ? setup.account_equity
      : FALLBACK_EQUITY);
  const dollarRisk = (equity * riskPct) / 100;

  const inverted = setup.action === 'BUY' ? stop >= entry : stop <= entry;

  return {
    riskDistance,
    rMultipleTp1,
    rMultipleTp2,
    riskPct,
    equity,
    dollarRisk,
    dollarTargetTp1: dollarRisk * (Number.isFinite(rMultipleTp1) ? rMultipleTp1 : 0),
    dollarTargetTp2: dollarRisk * (Number.isFinite(rMultipleTp2) ? rMultipleTp2 : 0),
    inverted,
  };
}

// ── Formatting ──────────────────────────────────────────────────────────────

/** Price formatter that keeps tick precision without trailing noise. */
export function formatPrice(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const decimals = Math.abs(value) >= 1000 ? 2 : Math.abs(value) >= 1 ? 2 : 5;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatMoney(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `$${Math.round(value).toLocaleString()}`;
}

export function formatR(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(2)}R` : '—';
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${value.toFixed(digits)}%`;
}

const REGIME_LABELS: Record<Regime, string> = {
  BULL_TREND_EXPANSION: 'Bull Expansion',
  BEAR_TREND_EXPANSION: 'Bear Expansion',
  RANGE_COMPRESSION: 'Range Compression',
  VOLATILITY_CHOP: 'Volatility Chop',
};

export function regimeLabel(regime: Regime): string {
  return REGIME_LABELS[regime] ?? String(regime).replace(/_/g, ' ');
}

/** CSS modifier suffix used by `styles/regime.css`. */
const REGIME_TOKENS: Record<Regime, string> = {
  BULL_TREND_EXPANSION: 'bull',
  BEAR_TREND_EXPANSION: 'bear',
  RANGE_COMPRESSION: 'range',
  VOLATILITY_CHOP: 'chop',
};

export function regimeToken(regime: Regime): string {
  return REGIME_TOKENS[regime] ?? 'unknown';
}

const STATUS_LABELS: Record<SignalStatus, string> = {
  PENDING_APPROVAL: 'Awaiting you',
  CONFIRMED: 'Confirmed',
  ROUTED: 'Routed',
  FILLED: 'Filled',
  REJECTED: 'Rejected',
  REJECTED_RISK: 'Blocked by risk',
  EXPIRED: 'Expired',
};

export function statusLabel(status: SignalStatus): string {
  return STATUS_LABELS[status] ?? String(status);
}

export function venueLabel(venue: Venue): string {
  return venue === 'TRADOVATE' ? 'Tradovate · CME' : 'Kraken · Margin';
}

/** Milliseconds until `expires_at`, clamped at zero. */
export function msUntil(isoTimestamp: string, now: number = Date.now()): number {
  const target = Date.parse(isoTimestamp);
  if (!Number.isFinite(target)) return 0;
  return Math.max(0, target - now);
}
