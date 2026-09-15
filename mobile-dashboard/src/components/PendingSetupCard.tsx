/**
 * PendingSetupCard — the HITL 1-tap confirmation widget (spec §5).
 *
 * Everything a trader needs to make a 60-second decision, on one thumb-reachable
 * card:
 *
 *   • symbol, action badge, regime chip, venue the alert asked for
 *   • entry / stop / TP1 / TP2, with the R-multiple computed from the geometry
 *   • risk % and the dollar figure that percentage actually represents
 *   • a live countdown ring driven off `expires_at`
 *   • three large one-tap buttons, each firing exactly one request
 *
 * Expiry is purely local. When the ring hits zero the card retires itself with
 * ZERO further calls — the backend already dropped the Redis key, and §5 is
 * explicit that an expiry involves no broker interaction.
 *
 * Double-submit protection is layered:
 *   1. `submittedRef` rejects a second tap in the same frame,
 *   2. `pending` disables all three buttons while a request is in flight,
 *   3. the parent hook refuses a duplicate signal id in `inFlight`.
 */

import { useCallback, useMemo, useRef, useState } from 'react';

import type { PendingSetup, Venue } from '../api/types';
import { useCountdown } from '../hooks/useCountdown';
import type { ActionOutcome } from '../hooks/usePendingSetups';
import { commitFeedback, successFeedback, tapFeedback, warnFeedback } from '../lib/haptics';
import {
  computeSetupMetrics,
  formatMoney,
  formatPrice,
  formatR,
  regimeLabel,
  regimeToken,
  venueLabel,
} from '../lib/setup';
import { useToast } from './Toast';

/** Width of the confirmation window (Redis SETEX 60). */
const WINDOW_MS = 60_000;

const RING_SIZE = 64;
const RING_STROKE = 5;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

type PressedAction = 'tradovate' | 'kraken' | 'reject' | null;

export interface PendingSetupCardProps {
  setup: PendingSetup;
  /** True while this card's request is in flight (owned by usePendingSetups). */
  pending: boolean;
  onConfirm: (signalId: string, venue: Venue) => Promise<ActionOutcome>;
  onReject: (signalId: string) => Promise<ActionOutcome>;
  /** Remove the card locally — used on expiry and after a settled action. */
  onRetire: (signalId: string) => void;
}

export function PendingSetupCard({
  setup,
  pending,
  onConfirm,
  onReject,
  onRetire,
}: PendingSetupCardProps): JSX.Element | null {
  const toast = useToast();

  /** Optimistic local state so the card reacts before the network answers. */
  const [pressed, setPressed] = useState<PressedAction>(null);
  const [settled, setSettled] = useState<'sent' | 'expired' | null>(null);
  /** Synchronous guard — a double tap cannot outrun a state update. */
  const submittedRef = useRef(false);

  const handleExpire = useCallback(() => {
    // No request. The Redis TTL already did this server-side.
    if (submittedRef.current) return;
    setSettled('expired');
    void warnFeedback();
    window.setTimeout(() => onRetire(setup.signal_id), 900);
  }, [onRetire, setup.signal_id]);

  const countdown = useCountdown(setup.expires_at, WINDOW_MS, handleExpire);

  const metrics = useMemo(() => computeSetupMetrics(setup), [setup]);

  const disabled = pending || submittedRef.current || settled !== null || countdown.expired;

  const act = useCallback(
    async (action: Exclude<PressedAction, null>) => {
      if (submittedRef.current || countdown.expired) return;
      submittedRef.current = true;
      setPressed(action);
      setSettled('sent');
      void (action === 'reject' ? tapFeedback() : commitFeedback());

      const outcome: ActionOutcome =
        action === 'reject'
          ? await onReject(setup.signal_id)
          : await onConfirm(setup.signal_id, action === 'kraken' ? 'KRAKEN' : 'TRADOVATE');

      if (outcome.kind === 'ok') {
        void successFeedback();
        const result = outcome.result;
        toast.push({
          tone: action === 'reject' ? 'info' : 'success',
          title:
            action === 'reject'
              ? `Rejected ${setup.symbol}`
              : `${setup.symbol} → ${action === 'kraken' ? 'Kraken' : 'Tradovate'}`,
          body:
            result.message ??
            (result.broker_order_id !== null ? `Order ${result.broker_order_id}` : result.status),
        });
        return;
      }

      if (outcome.kind === 'expired') {
        // Not a failure: the 60s window simply closed first.
        setSettled('expired');
        void warnFeedback();
        toast.push({
          tone: 'warning',
          title: `${setup.symbol} expired`,
          body: outcome.detail ?? 'The 60-second window closed before the tap landed.',
        });
        return;
      }

      // Genuine failure — release the guard so the trader can retry.
      submittedRef.current = false;
      setPressed(null);
      setSettled(null);
      void warnFeedback();
      toast.push({ tone: 'danger', title: `${setup.symbol} not submitted`, body: outcome.message });
    },
    [countdown.expired, onConfirm, onReject, setup.signal_id, setup.symbol, toast],
  );

  const ringOffset = RING_CIRCUMFERENCE * (1 - countdown.fraction);
  const ringToken = countdown.urgent ? 'urgent' : countdown.fraction > 0.5 ? 'calm' : 'warm';
  const actionToken = setup.action === 'BUY' ? 'buy' : 'sell';

  const cardClasses = [
    'setup-card',
    `setup-card--${actionToken}`,
    countdown.urgent ? 'setup-card--urgent' : '',
    settled === 'expired' ? 'setup-card--expired' : '',
    settled === 'sent' ? 'setup-card--sent' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <article className={cardClasses} aria-busy={pending}>
      <header className="setup-card__head">
        <div className="setup-card__identity">
          <div className="setup-card__symbol-row">
            <span className={`badge badge--${actionToken}`}>{setup.action}</span>
            <h2 className="setup-card__symbol">{setup.symbol}</h2>
          </div>
          <div className="setup-card__tags">
            <span className={`chip chip--regime-${regimeToken(setup.regime)}`}>
              {regimeLabel(setup.regime)}
            </span>
            <span className="chip chip--venue">{venueLabel(setup.venue)}</span>
          </div>
        </div>

        <div className={`ring ring--${ringToken}`} role="timer" aria-label="Seconds remaining">
          <svg width={RING_SIZE} height={RING_SIZE} viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`} aria-hidden="true">
            <circle
              className="ring__track"
              cx={RING_SIZE / 2}
              cy={RING_SIZE / 2}
              r={RING_RADIUS}
              strokeWidth={RING_STROKE}
              fill="none"
            />
            <circle
              className="ring__sweep"
              cx={RING_SIZE / 2}
              cy={RING_SIZE / 2}
              r={RING_RADIUS}
              strokeWidth={RING_STROKE}
              fill="none"
              strokeDasharray={RING_CIRCUMFERENCE}
              strokeDashoffset={ringOffset}
              strokeLinecap="round"
              transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
            />
          </svg>
          <span className="ring__value">{countdown.expired ? '0' : countdown.secondsRemaining}</span>
        </div>
      </header>

      <dl className="setup-card__levels">
        <div className="level">
          <dt>Entry</dt>
          <dd>{formatPrice(setup.limit_price)}</dd>
        </div>
        <div className="level level--stop">
          <dt>Stop</dt>
          <dd>{formatPrice(setup.stop_loss)}</dd>
        </div>
        <div className="level level--target">
          <dt>
            TP1 <span className="level__r">{formatR(metrics.rMultipleTp1)}</span>
          </dt>
          <dd>{formatPrice(setup.take_profit_1)}</dd>
        </div>
        <div className="level level--target">
          <dt>
            TP2 <span className="level__r">{formatR(metrics.rMultipleTp2)}</span>
          </dt>
          <dd>{formatPrice(setup.take_profit_2)}</dd>
        </div>
      </dl>

      <div className="setup-card__risk">
        <span className="risk__pct">{metrics.riskPct.toFixed(2)}% risk</span>
        <span className="risk__sep" aria-hidden="true">
          ·
        </span>
        <span className="risk__dollars">{formatMoney(metrics.dollarRisk)} at stop</span>
        <span className="risk__sep" aria-hidden="true">
          ·
        </span>
        <span className="risk__reward">
          {formatMoney(metrics.dollarTargetTp2)} at TP2
        </span>
        {typeof setup.quantity === 'number' && (
          <>
            <span className="risk__sep" aria-hidden="true">
              ·
            </span>
            <span className="risk__qty">{setup.quantity} qty</span>
          </>
        )}
      </div>

      {metrics.inverted && (
        <p className="setup-card__warning" role="alert">
          Stop sits on the wrong side of entry for a {setup.action}. Verify before routing.
        </p>
      )}

      {settled === 'expired' ? (
        <div className="setup-card__terminal setup-card__terminal--expired">Expired — no order sent</div>
      ) : (
        <div className="setup-card__actions">
          <button
            type="button"
            className={`action action--tradovate ${pressed === 'tradovate' ? 'is-pressed' : ''}`}
            onClick={() => void act('tradovate')}
            disabled={disabled}
          >
            <span className="action__label">Confirm Tradovate</span>
            <span className="action__sub">CME bracket</span>
          </button>

          <button
            type="button"
            className={`action action--kraken ${pressed === 'kraken' ? 'is-pressed' : ''}`}
            onClick={() => void act('kraken')}
            disabled={disabled}
          >
            <span className="action__label">Route Kraken</span>
            <span className="action__sub">Margin</span>
          </button>

          <button
            type="button"
            className={`action action--reject ${pressed === 'reject' ? 'is-pressed' : ''}`}
            onClick={() => void act('reject')}
            disabled={disabled}
          >
            <span className="action__label">Reject</span>
          </button>
        </div>
      )}

      {settled === 'sent' && <div className="setup-card__terminal setup-card__terminal--sent">Submitting…</div>}
    </article>
  );
}

export default PendingSetupCard;
