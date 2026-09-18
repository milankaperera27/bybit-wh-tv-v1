/**
 * Owns the live pending-setup roster.
 *
 * Three inputs are merged:
 *   1. `GET /api/v1/confirmation/pending` on mount / foreground / manual refresh
 *   2. `WS /ws/dashboard` push events (new setup, resolution, regime, kill switch)
 *   3. purely local expiry — a card whose `expires_at` has passed is dropped
 *      client-side with no request, matching the Redis TTL semantics of §5.
 *
 * It also owns the one-tap mutations so double-submit protection lives in one
 * place: a signal id present in `inFlight` cannot be actioned again.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, confirmSetup, fetchPendingSetups, rejectSetup } from '../api/client';
import { createDashboardSocket } from '../api/socket';
import type {
  DashboardEvent,
  KillSwitchStatus,
  OrderResult,
  PendingSetup,
  RegimeSnapshot,
  SocketState,
  Venue,
} from '../api/types';
import { msUntil } from '../lib/setup';

/** Terminal outcome of a 1-tap action, as the UI needs to narrate it. */
export type ActionOutcome =
  | { kind: 'ok'; result: OrderResult }
  | { kind: 'expired'; signalId: string; detail: string | null }
  | { kind: 'error'; signalId: string; message: string };

export interface PendingSetupsState {
  setups: PendingSetup[];
  /** Latest regime snapshot seen on the socket, for `RegimeRadar`. */
  regime: RegimeSnapshot | null;
  /** Latest kill-switch status seen on the socket; REST remains authoritative. */
  killSwitch: KillSwitchStatus | null;
  socketState: SocketState;
  loading: boolean;
  /** REST-level failure text, null when healthy. */
  error: string | null;
  /** ms epoch of the last successful REST or socket update. */
  lastUpdated: number | null;
  /** Signal ids with a request in flight — used to disable buttons. */
  inFlight: ReadonlySet<string>;
  refresh: () => Promise<void>;
  confirm: (signalId: string, venue: Venue) => Promise<ActionOutcome>;
  reject: (signalId: string, reason?: string) => Promise<ActionOutcome>;
  /** Drop a card locally (expiry, or after an optimistic resolution settles). */
  retire: (signalId: string) => void;
}

function sortByUrgency(setups: PendingSetup[]): PendingSetup[] {
  return [...setups].sort((a, b) => Date.parse(a.expires_at) - Date.parse(b.expires_at));
}

function isLive(setup: PendingSetup): boolean {
  return setup.status === 'PENDING_APPROVAL' && msUntil(setup.expires_at) > 0;
}

export function usePendingSetups(): PendingSetupsState {
  const [setups, setSetups] = useState<PendingSetup[]>([]);
  const [regime, setRegime] = useState<RegimeSnapshot | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitchStatus | null>(null);
  const [socketState, setSocketState] = useState<SocketState>('connecting');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [inFlight, setInFlight] = useState<ReadonlySet<string>>(() => new Set<string>());

  const mountedRef = useRef(true);
  /** Guards against a second tap racing the first before state re-renders. */
  const inFlightRef = useRef<Set<string>>(new Set<string>());

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const retire = useCallback((signalId: string) => {
    setSetups((current) => current.filter((setup) => setup.signal_id !== signalId));
  }, []);

  const upsert = useCallback((incoming: PendingSetup) => {
    setSetups((current) => {
      if (!isLive(incoming)) {
        return current.filter((setup) => setup.signal_id !== incoming.signal_id);
      }
      const index = current.findIndex((setup) => setup.signal_id === incoming.signal_id);
      if (index === -1) return sortByUrgency([...current, incoming]);
      const next = [...current];
      next[index] = incoming;
      return sortByUrgency(next);
    });
    setLastUpdated(Date.now());
  }, []);

  const refresh = useCallback(async () => {
    try {
      const fetched = await fetchPendingSetups();
      if (!mountedRef.current) return;
      setSetups(sortByUrgency(fetched.filter(isLive)));
      setError(null);
      setLastUpdated(Date.now());
    } catch (cause) {
      if (!mountedRef.current) return;
      const message =
        cause instanceof ApiError
          ? cause.isOffline
            ? 'Backend unreachable'
            : `Backend error ${cause.status}`
          : 'Unexpected error loading setups';
      setError(message);
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  // ── Initial load + foreground resync ──────────────────────────────────────
  useEffect(() => {
    void refresh();
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('online', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('online', onVisibility);
    };
  }, [refresh]);

  // ── Live stream ───────────────────────────────────────────────────────────
  useEffect(() => {
    const socket = createDashboardSocket({
      onStateChange: (state) => {
        if (mountedRef.current) setSocketState(state);
      },
      onEvent: (event: DashboardEvent) => {
        if (!mountedRef.current) return;
        switch (event.type) {
          case 'snapshot':
            setSetups(sortByUrgency(event.setups.filter(isLive)));
            if (event.regime !== null) setRegime(event.regime);
            if (event.kill_switch !== null) setKillSwitch(event.kill_switch);
            setError(null);
            setLastUpdated(Date.now());
            break;
          case 'setup_pending':
            upsert(event.setup);
            break;
          case 'setup_resolved':
            retire(event.signal_id);
            setLastUpdated(Date.now());
            break;
          case 'regime':
            setRegime(event.regime);
            setLastUpdated(Date.now());
            break;
          case 'kill_switch':
            setKillSwitch(event.kill_switch);
            break;
          case 'heartbeat':
            setLastUpdated(Date.now());
            break;
          default:
            break;
        }
      },
    });
    socket.connect();
    return () => socket.close();
  }, [retire, upsert]);

  // ── Local expiry sweep — no network involved ──────────────────────────────
  useEffect(() => {
    const timer = setInterval(() => {
      setSetups((current) => {
        const live = current.filter((setup) => msUntil(setup.expires_at) > 0);
        return live.length === current.length ? current : live;
      });
    }, 1_000);
    return () => clearInterval(timer);
  }, []);

  const beginAction = useCallback((signalId: string): boolean => {
    if (inFlightRef.current.has(signalId)) return false;
    inFlightRef.current.add(signalId);
    setInFlight(new Set(inFlightRef.current));
    return true;
  }, []);

  const endAction = useCallback((signalId: string): void => {
    inFlightRef.current.delete(signalId);
    if (mountedRef.current) setInFlight(new Set(inFlightRef.current));
  }, []);

  const runAction = useCallback(
    async (
      signalId: string,
      call: () => Promise<
        { kind: 'ok'; data: OrderResult } | { kind: 'expired'; signalId: string; detail: string | null }
      >,
    ): Promise<ActionOutcome> => {
      if (!beginAction(signalId)) {
        return { kind: 'error', signalId, message: 'Already submitting' };
      }
      try {
        const outcome = await call();
        // Either way the setup is finished — take the card out optimistically.
        retire(signalId);
        if (outcome.kind === 'expired') {
          return { kind: 'expired', signalId, detail: outcome.detail };
        }
        return { kind: 'ok', result: outcome.data };
      } catch (cause) {
        const message =
          cause instanceof ApiError
            ? cause.isOffline
              ? 'Backend unreachable — not submitted'
              : (cause.detail ?? `Backend error ${cause.status}`)
            : 'Unexpected error';
        // The call failed, so the setup may still be live: pull the truth back.
        void refresh();
        return { kind: 'error', signalId, message };
      } finally {
        endAction(signalId);
      }
    },
    [beginAction, endAction, refresh, retire],
  );

  const confirm = useCallback(
    (signalId: string, venue: Venue) => runAction(signalId, () => confirmSetup(signalId, venue)),
    [runAction],
  );

  const reject = useCallback(
    (signalId: string, reason?: string) => runAction(signalId, () => rejectSetup(signalId, reason)),
    [runAction],
  );

  return useMemo(
    () => ({
      setups,
      regime,
      killSwitch,
      socketState,
      loading,
      error,
      lastUpdated,
      inFlight,
      refresh,
      confirm,
      reject,
      retire,
    }),
    [
      setups,
      regime,
      killSwitch,
      socketState,
      loading,
      error,
      lastUpdated,
      inFlight,
      refresh,
      confirm,
      reject,
      retire,
    ],
  );
}
