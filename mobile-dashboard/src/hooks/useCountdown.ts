/**
 * Live countdown against a setup's `expires_at`.
 *
 * Drives the 60-second ring on `PendingSetupCard`. It is entirely local: once
 * the deadline passes the card retires itself with ZERO further backend calls,
 * exactly as §5 requires (expiry is a Redis TTL event, not a client action).
 *
 * Ticks on `requestAnimationFrame` so the ring is smooth, and re-syncs on
 * visibility change because a backgrounded phone freezes rAF and would
 * otherwise resume showing a stale number.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface Countdown {
  /** Whole seconds left, rounded up: 60 → 1. */
  secondsRemaining: number;
  /** Raw milliseconds left, clamped at 0. */
  msRemaining: number;
  /** 1 at the start of the window, 0 at expiry — the ring's sweep. */
  fraction: number;
  /** True once the deadline has passed. */
  expired: boolean;
  /** Under 10 seconds — the UI turns urgent. */
  urgent: boolean;
}

function computeMs(deadline: number): number {
  return Math.max(0, deadline - Date.now());
}

/**
 * @param expiresAt   ISO-8601 deadline from the setup payload.
 * @param windowMs    Full width of the window, for the ring sweep. Default 60s.
 * @param onExpire    Fired exactly once when the countdown crosses zero.
 */
export function useCountdown(
  expiresAt: string | null | undefined,
  windowMs = 60_000,
  onExpire?: () => void,
): Countdown {
  const deadline = expiresAt ? Date.parse(expiresAt) : Number.NaN;
  const hasDeadline = Number.isFinite(deadline);

  const [msRemaining, setMsRemaining] = useState<number>(() =>
    hasDeadline ? computeMs(deadline) : 0,
  );

  const firedRef = useRef(false);
  const onExpireRef = useRef<(() => void) | undefined>(onExpire);
  onExpireRef.current = onExpire;

  // A new deadline restarts the one-shot expiry guard.
  useEffect(() => {
    firedRef.current = false;
    setMsRemaining(hasDeadline ? computeMs(deadline) : 0);
  }, [deadline, hasDeadline]);

  const tick = useCallback(() => {
    if (!hasDeadline) return;
    const remaining = computeMs(deadline);
    setMsRemaining(remaining);
    if (remaining <= 0 && !firedRef.current) {
      firedRef.current = true;
      onExpireRef.current?.();
    }
  }, [deadline, hasDeadline]);

  useEffect(() => {
    if (!hasDeadline) return;
    let frame = 0;
    let stopped = false;

    const loop = (): void => {
      if (stopped) return;
      tick();
      if (computeMs(deadline) > 0) {
        frame = requestAnimationFrame(loop);
      }
    };
    frame = requestAnimationFrame(loop);

    // rAF is suspended while the tab/app is hidden; resync and restart on return.
    const onVisibility = (): void => {
      if (document.visibilityState !== 'visible' || stopped) return;
      tick();
      cancelAnimationFrame(frame);
      if (computeMs(deadline) > 0) frame = requestAnimationFrame(loop);
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [deadline, hasDeadline, tick]);

  const clampedWindow = windowMs > 0 ? windowMs : 60_000;

  return {
    msRemaining,
    secondsRemaining: Math.ceil(msRemaining / 1000),
    fraction: Math.min(1, Math.max(0, msRemaining / clampedWindow)),
    expired: hasDeadline ? msRemaining <= 0 : false,
    urgent: msRemaining > 0 && msRemaining <= 10_000,
  };
}
