/**
 * Emergency state: the global kill switch and the daily-drawdown lockout.
 *
 * Polls `GET /api/v1/emergency/status` on a slow cadence and accepts pushed
 * updates from the dashboard socket. The engage path is deliberately two calls:
 *
 *     POST /api/v1/emergency/kill      block all further routing, then
 *     POST /api/v1/emergency/flatten   cancel working orders + flatten venues
 *
 * in that order, so nothing new can be routed while the flatten is in flight.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, engageKillSwitch, fetchEmergencyStatus, flattenAll, releaseKillSwitch } from '../api/client';
import type { FlattenResult, KillSwitchStatus } from '../api/types';

const POLL_INTERVAL_MS = 15_000;

export type KillOutcome =
  | { kind: 'ok'; status: KillSwitchStatus; flatten: FlattenResult | null }
  | { kind: 'error'; message: string; killed: boolean };

export interface KillSwitchState {
  status: KillSwitchStatus | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  /** True while the kill switch is engaged OR the drawdown lockout is active. */
  halted: boolean;
  refresh: () => Promise<void>;
  /** Engage, then flatten everything. */
  engage: (reason?: string) => Promise<KillOutcome>;
  /** Release the kill switch — does not re-open any position. */
  release: () => Promise<KillOutcome>;
  /** Accept a status pushed over the dashboard socket. */
  applyPushed: (status: KillSwitchStatus) => void;
}

function describe(cause: unknown, fallback: string): string {
  if (cause instanceof ApiError) {
    if (cause.isOffline) return 'Backend unreachable';
    return cause.detail ?? `Backend error ${cause.status}`;
  }
  return fallback;
}

export function useKillSwitch(): KillSwitchState {
  const [status, setStatus] = useState<KillSwitchStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  /** Double-submit guard that does not wait for a re-render. */
  const busyRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchEmergencyStatus();
      if (!mountedRef.current) return;
      setStatus(next);
      setError(null);
    } catch (cause) {
      if (!mountedRef.current) return;
      setError(describe(cause, 'Could not read emergency status'));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refresh]);

  const applyPushed = useCallback((pushed: KillSwitchStatus) => {
    if (mountedRef.current) setStatus(pushed);
  }, []);

  const engage = useCallback(async (reason?: string): Promise<KillOutcome> => {
    if (busyRef.current) return { kind: 'error', message: 'Already in progress', killed: false };
    busyRef.current = true;
    setBusy(true);
    let killed = false;
    try {
      const engaged = await engageKillSwitch(reason ?? 'Manual kill switch from mobile dashboard');
      killed = true;
      if (mountedRef.current) setStatus(engaged);

      // Kill first, flatten second: no new routing can slip through the gap.
      const flatten = await flattenAll();
      const settled = await fetchEmergencyStatus().catch(() => engaged);
      if (mountedRef.current) {
        setStatus(settled);
        setError(null);
      }
      return { kind: 'ok', status: settled, flatten };
    } catch (cause) {
      const message = describe(cause, 'Emergency action failed');
      if (mountedRef.current) setError(message);
      void refresh();
      return { kind: 'error', message, killed };
    } finally {
      busyRef.current = false;
      if (mountedRef.current) setBusy(false);
    }
  }, [refresh]);

  const release = useCallback(async (): Promise<KillOutcome> => {
    if (busyRef.current) return { kind: 'error', message: 'Already in progress', killed: false };
    busyRef.current = true;
    setBusy(true);
    try {
      const released = await releaseKillSwitch();
      if (mountedRef.current) {
        setStatus(released);
        setError(null);
      }
      return { kind: 'ok', status: released, flatten: null };
    } catch (cause) {
      const message = describe(cause, 'Could not release the kill switch');
      if (mountedRef.current) setError(message);
      return { kind: 'error', message, killed: false };
    } finally {
      busyRef.current = false;
      if (mountedRef.current) setBusy(false);
    }
  }, []);

  const halted = status !== null && (status.engaged || status.lockout);

  return useMemo(
    () => ({ status, loading, busy, error, halted, refresh, engage, release, applyPushed }),
    [status, loading, busy, error, halted, refresh, engage, release, applyPushed],
  );
}
