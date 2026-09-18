/**
 * Persistent top bar: connection health, backend liveness, drawdown headroom
 * and the count of setups currently demanding a decision.
 *
 * Sits under the safe-area inset so it clears the notch on a phone.
 */

import { useEffect, useState } from 'react';

import { fetchHealth } from '../api/client';
import type { HealthStatus, KillSwitchStatus, SocketState } from '../api/types';
import { formatPct } from '../lib/setup';

const HEALTH_POLL_MS = 30_000;

export interface StatusBarProps {
  socketState: SocketState;
  pendingCount: number;
  killSwitch: KillSwitchStatus | null;
  lastUpdated: number | null;
  error: string | null;
  onRefresh: () => void;
}

const SOCKET_COPY: Record<SocketState, { label: string; token: string }> = {
  connecting: { label: 'Connecting', token: 'warn' },
  open: { label: 'Live', token: 'ok' },
  reconnecting: { label: 'Reconnecting', token: 'warn' },
  closed: { label: 'Offline', token: 'bad' },
};

function relativeTime(timestamp: number | null): string {
  if (timestamp === null) return 'never';
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 3) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes}m ago` : `${Math.round(minutes / 60)}h ago`;
}

export function StatusBar({
  socketState,
  pendingCount,
  killSwitch,
  lastUpdated,
  error,
  onRefresh,
}: StatusBarProps): JSX.Element {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [, forceTick] = useState(0);

  // Poll liveness independently of the stream so a dead backend is obvious even
  // when the socket is merely idle.
  useEffect(() => {
    let cancelled = false;
    const poll = async (): Promise<void> => {
      try {
        const next = await fetchHealth();
        if (!cancelled) setHealth(next);
      } catch {
        if (!cancelled) setHealth({ status: 'down' });
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  // Keep the "x ago" label honest without re-rendering the whole tree.
  useEffect(() => {
    const timer = setInterval(() => forceTick((n) => n + 1), 5_000);
    return () => clearInterval(timer);
  }, []);

  const socket = SOCKET_COPY[socketState];
  const healthToken =
    health === null ? 'warn' : health.status === 'ok' ? 'ok' : health.status === 'degraded' ? 'warn' : 'bad';

  const drawdown = killSwitch?.daily_drawdown_pct ?? null;
  const drawdownLimit = killSwitch?.daily_drawdown_limit_pct ?? null;
  const drawdownToken =
    drawdown === null || drawdownLimit === null || drawdownLimit <= 0
      ? 'idle'
      : drawdown >= drawdownLimit
        ? 'bad'
        : drawdown >= drawdownLimit * 0.6
          ? 'warn'
          : 'ok';

  return (
    <header className="statusbar">
      <div className="statusbar__row">
        <div className="statusbar__brand">
          <span className="statusbar__mark" aria-hidden="true">
            ▲
          </span>
          <span className="statusbar__name">ATLAS</span>
        </div>

        <div className="statusbar__chips">
          <span className={`chip chip--${socket.token}`} title={`Dashboard stream: ${socket.label}`}>
            <span className={`chip__dot chip__dot--${socket.token}`} aria-hidden="true" />
            {socket.label}
          </span>
          <span className={`chip chip--${healthToken}`} title="Backend liveness">
            API {health?.status ?? '…'}
          </span>
          {health?.mock_brokers === true && (
            <span className="chip chip--warn" title="ATLAS_MOCK_BROKERS=true — no real orders">
              PAPER
            </span>
          )}
        </div>

        <button type="button" className="statusbar__refresh" onClick={onRefresh} aria-label="Refresh setups">
          ⟳
        </button>
      </div>

      <div className="statusbar__row statusbar__row--meta">
        <span className="statusbar__meta">
          <strong>{pendingCount}</strong> pending
        </span>
        <span className={`statusbar__meta statusbar__meta--${drawdownToken}`}>
          DD {formatPct(drawdown, 2)}
          {drawdownLimit !== null ? ` / ${formatPct(drawdownLimit, 1)}` : ''}
        </span>
        {killSwitch?.lockout === true && <span className="statusbar__meta statusbar__meta--bad">LOCKOUT</span>}
        {killSwitch?.engaged === true && <span className="statusbar__meta statusbar__meta--bad">KILLED</span>}
        <span className="statusbar__meta statusbar__meta--muted">{relativeTime(lastUpdated)}</span>
      </div>

      {error !== null && <div className="statusbar__error">{error}</div>}
    </header>
  );
}

export default StatusBar;
