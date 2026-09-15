/**
 * KillSwitchButton — emergency liquidation trigger.
 *
 * Two deliberate barriers, because an accidental brush of a phone screen must
 * never flatten a book:
 *
 *   1. PRESS AND HOLD for 2 seconds to ARM. Releasing early cancels; a fill ring
 *      and escalating haptics show the progress.
 *   2. Once armed, a separate CONFIRM tap actually fires, and the armed state
 *      itself times out after 6 seconds.
 *
 * Firing calls `POST /api/v1/emergency/kill` first (blocks all further routing)
 * and only then `POST /api/v1/emergency/flatten` (cancel working orders +
 * flatten every venue), so nothing new can slip through while the flatten runs.
 * Engaged/released state is read from `GET /api/v1/emergency/status`.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { KillSwitchStatus } from '../api/types';
import type { KillOutcome } from '../hooks/useKillSwitch';
import { armFeedback, commitFeedback, successFeedback, warnFeedback } from '../lib/haptics';
import { useToast } from './Toast';

/** Hold duration required to arm. */
const ARM_HOLD_MS = 2_000;
/** Armed state auto-disarms after this long without a confirm. */
const ARMED_TIMEOUT_MS = 6_000;
const HOLD_TICK_MS = 50;

export interface KillSwitchButtonProps {
  status: KillSwitchStatus | null;
  busy: boolean;
  onEngage: (reason?: string) => Promise<KillOutcome>;
  onRelease: () => Promise<KillOutcome>;
}

export function KillSwitchButton({ status, busy, onEngage, onRelease }: KillSwitchButtonProps): JSX.Element {
  const toast = useToast();

  const [holdProgress, setHoldProgress] = useState(0);
  const [armed, setArmed] = useState(false);

  const holdTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const armedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hapticStep = useRef(0);

  const engaged = status?.engaged === true;

  const clearHold = useCallback(() => {
    if (holdTimer.current !== null) {
      clearInterval(holdTimer.current);
      holdTimer.current = null;
    }
    hapticStep.current = 0;
    setHoldProgress(0);
  }, []);

  const disarm = useCallback(() => {
    if (armedTimer.current !== null) {
      clearTimeout(armedTimer.current);
      armedTimer.current = null;
    }
    setArmed(false);
  }, []);

  useEffect(() => {
    return () => {
      if (holdTimer.current !== null) clearInterval(holdTimer.current);
      if (armedTimer.current !== null) clearTimeout(armedTimer.current);
    };
  }, []);

  const arm = useCallback(() => {
    clearHold();
    setArmed(true);
    void commitFeedback();
    armedTimer.current = setTimeout(() => {
      setArmed(false);
      armedTimer.current = null;
    }, ARMED_TIMEOUT_MS);
  }, [clearHold]);

  const beginHold = useCallback(() => {
    if (busy || engaged || armed || holdTimer.current !== null) return;
    const startedAt = Date.now();
    holdTimer.current = setInterval(() => {
      const progress = Math.min(1, (Date.now() - startedAt) / ARM_HOLD_MS);
      setHoldProgress(progress);
      // Four escalating pulses on the way to armed.
      const step = Math.floor(progress * 4);
      if (step > hapticStep.current) {
        hapticStep.current = step;
        void armFeedback();
      }
      if (progress >= 1) arm();
    }, HOLD_TICK_MS);
  }, [arm, armed, busy, engaged]);

  const cancelHold = useCallback(() => {
    if (holdTimer.current === null) return;
    clearHold();
  }, [clearHold]);

  const fire = useCallback(async () => {
    disarm();
    void commitFeedback();
    const outcome = await onEngage('Manual kill switch — mobile dashboard');
    if (outcome.kind === 'ok') {
      void successFeedback();
      const flatten = outcome.flatten;
      toast.push({
        tone: 'danger',
        title: 'KILL SWITCH ENGAGED',
        body:
          flatten !== null
            ? `${flatten.cancelled_orders} orders cancelled · ${flatten.flattened_positions} positions flattened`
            : 'Routing blocked.',
        ttlMs: 8_000,
      });
      return;
    }
    void warnFeedback();
    toast.push({
      tone: 'danger',
      title: outcome.killed ? 'Killed, but flatten FAILED' : 'Kill switch FAILED',
      body: outcome.killed
        ? `${outcome.message} — flatten positions manually at the venue.`
        : outcome.message,
      ttlMs: 0,
    });
  }, [disarm, onEngage, toast]);

  const release = useCallback(async () => {
    const outcome = await onRelease();
    if (outcome.kind === 'ok') {
      void successFeedback();
      toast.push({ tone: 'success', title: 'Kill switch released', body: 'Routing re-enabled.' });
      return;
    }
    void warnFeedback();
    toast.push({ tone: 'danger', title: 'Release failed', body: outcome.message });
  }, [onRelease, toast]);

  // ── Engaged state: offer the release instead ──────────────────────────────
  if (engaged) {
    return (
      <section className="killswitch killswitch--engaged" aria-live="assertive">
        <div className="killswitch__banner">
          <span className="killswitch__siren" aria-hidden="true" />
          <div>
            <strong className="killswitch__headline">KILL SWITCH ENGAGED</strong>
            <span className="killswitch__detail">
              {status?.reason ?? 'All routing blocked.'}
              {status?.engaged_at !== null && status?.engaged_at !== undefined
                ? ` · ${new Date(status.engaged_at).toLocaleTimeString()}`
                : ''}
            </span>
          </div>
        </div>
        <button type="button" className="killswitch__release" onClick={() => void release()} disabled={busy}>
          {busy ? 'Working…' : 'Release kill switch'}
        </button>
      </section>
    );
  }

  // ── Armed state: one confirm tap away ─────────────────────────────────────
  if (armed) {
    return (
      <section className="killswitch killswitch--armed" aria-live="assertive">
        <div className="killswitch__armed-row">
          <button type="button" className="killswitch__confirm" onClick={() => void fire()} disabled={busy}>
            {busy ? 'FLATTENING…' : 'CONFIRM — KILL & FLATTEN ALL'}
          </button>
          <button type="button" className="killswitch__cancel" onClick={disarm} disabled={busy}>
            Cancel
          </button>
        </div>
        <span className="killswitch__hint">Armed. Auto-disarms in {ARMED_TIMEOUT_MS / 1000}s.</span>
      </section>
    );
  }

  // ── Idle: press and hold to arm ───────────────────────────────────────────
  return (
    <section className="killswitch">
      <button
        type="button"
        className={`killswitch__hold ${holdProgress > 0 ? 'is-holding' : ''}`}
        onPointerDown={beginHold}
        onPointerUp={cancelHold}
        onPointerLeave={cancelHold}
        onPointerCancel={cancelHold}
        onContextMenu={(event) => event.preventDefault()}
        disabled={busy}
        aria-label="Emergency kill switch — press and hold two seconds to arm"
      >
        <span className="killswitch__fill" style={{ width: `${(holdProgress * 100).toFixed(1)}%` }} aria-hidden="true" />
        <span className="killswitch__hold-label">
          {holdProgress > 0 ? `ARMING ${(holdProgress * 100).toFixed(0)}%` : 'HOLD 2s — EMERGENCY KILL'}
        </span>
      </button>
      {status?.lockout === true && (
        <span className="killswitch__hint killswitch__hint--warn">
          Daily drawdown lockout already active ({status.daily_drawdown_pct.toFixed(2)}% of{' '}
          {status.daily_drawdown_limit_pct.toFixed(1)}%).
        </span>
      )}
    </section>
  );
}

export default KillSwitchButton;
