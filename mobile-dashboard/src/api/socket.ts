/**
 * Live dashboard WebSocket client — `WS /ws/dashboard` (FROZEN CONTRACT B §4).
 *
 * Feeds two things into the UI:
 *   • regime snapshots for `RegimeRadar`
 *   • pending-setup lifecycle events for `PendingSetupCard`
 *
 * Reconnects with exponential backoff + jitter, capped, and resets the delay on
 * a successful open. A phone drops its socket constantly (doze, cell handover,
 * screen off), so the socket is advisory: `usePendingSetups` still owns a REST
 * refresh path and each card expires itself locally off `expires_at`.
 */

import { resolveWebSocketBase } from './client';
import type {
  DashboardEvent,
  KillSwitchStatus,
  PendingSetup,
  Regime,
  RegimeCell,
  RegimeSnapshot,
  SignalStatus,
  SocketState,
  Timeframe,
} from './types';
import { REGIMES } from './types';

export interface DashboardSocketHandlers {
  onEvent?: (event: DashboardEvent) => void;
  onStateChange?: (state: SocketState) => void;
}

export interface DashboardSocketOptions extends DashboardSocketHandlers {
  /** Path of the stream. Defaults to the contract path. */
  path?: string;
  /** First retry delay in ms. */
  baseDelayMs?: number;
  /** Ceiling for the backoff, in ms. */
  maxDelayMs?: number;
  /** Ping interval to keep intermediaries from idling the socket out. */
  heartbeatMs?: number;
}

const DEFAULTS = {
  path: '/ws/dashboard',
  baseDelayMs: 500,
  maxDelayMs: 30_000,
  heartbeatMs: 25_000,
} as const;

// ── Runtime narrowing (no `any`) ────────────────────────────────────────────

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asRegime(value: unknown): Regime | null {
  const s = asString(value);
  return s !== null && (REGIMES as readonly string[]).includes(s) ? (s as Regime) : null;
}

const TIMEFRAMES: readonly Timeframe[] = ['5m', '1h', 'D'];

function asTimeframe(value: unknown): Timeframe | null {
  const s = asString(value);
  if (s === null) return null;
  if ((TIMEFRAMES as readonly string[]).includes(s)) return s as Timeframe;
  // Tolerate backend spellings.
  const normalised: Record<string, Timeframe> = {
    '5': '5m',
    '5min': '5m',
    '60': '1h',
    '1H': '1h',
    '1D': 'D',
    daily: 'D',
    DAILY: 'D',
    d: 'D',
  };
  return normalised[s] ?? null;
}

function parseRegimeCell(value: unknown): RegimeCell | null {
  const record = asRecord(value);
  if (record === null) return null;
  const timeframe = asTimeframe(record['timeframe']);
  const regime = asRegime(record['regime']);
  if (timeframe === null || regime === null) return null;
  return {
    timeframe,
    regime,
    adx: asNumber(record['adx']) ?? 0,
    atr: asNumber(record['atr']) ?? 0,
    atr_percentile: asNumber(record['atr_percentile']),
    cvd_slope: asNumber(record['cvd_slope']),
    updated_at: asString(record['updated_at']) ?? new Date().toISOString(),
  };
}

function parseRegimeSnapshot(value: unknown): RegimeSnapshot | null {
  const record = asRecord(value);
  if (record === null) return null;
  const rawCells = record['cells'];
  if (!Array.isArray(rawCells)) return null;
  const cells: RegimeCell[] = [];
  for (const raw of rawCells) {
    const cell = parseRegimeCell(raw);
    if (cell !== null) cells.push(cell);
  }
  if (cells.length === 0) return null;
  return {
    symbol: asString(record['symbol']) ?? '—',
    cells,
    updated_at: asString(record['updated_at']) ?? new Date().toISOString(),
  };
}

function parseSetup(value: unknown): PendingSetup | null {
  const record = asRecord(value);
  if (record === null) return null;
  const signalId = asString(record['signal_id']);
  const symbol = asString(record['symbol']);
  if (signalId === null || symbol === null) return null;
  // Field-level trust: the backend owns the schema; we only guarantee the two
  // identifiers the UI keys on are present before handing the object upstream.
  return record as unknown as PendingSetup;
}

function parseKillSwitch(value: unknown): KillSwitchStatus | null {
  const record = asRecord(value);
  if (record === null) return null;
  if (typeof record['engaged'] !== 'boolean') return null;
  return record as unknown as KillSwitchStatus;
}

/** Turn a raw frame into a typed `DashboardEvent`, or null if unrecognised. */
export function parseDashboardEvent(raw: string): DashboardEvent | null {
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  const record = asRecord(decoded);
  if (record === null) return null;

  const type = asString(record['type'] ?? record['event']);
  if (type === null) return null;

  switch (type) {
    case 'snapshot': {
      const rawSetups = record['setups'];
      const setups: PendingSetup[] = [];
      if (Array.isArray(rawSetups)) {
        for (const item of rawSetups) {
          const setup = parseSetup(item);
          if (setup !== null) setups.push(setup);
        }
      }
      return {
        type: 'snapshot',
        setups,
        regime: parseRegimeSnapshot(record['regime']),
        kill_switch: parseKillSwitch(record['kill_switch']),
      };
    }
    case 'setup_pending':
    case 'setup_new': {
      const setup = parseSetup(record['setup'] ?? record['data']);
      return setup === null ? null : { type: 'setup_pending', setup };
    }
    case 'setup_resolved':
    case 'setup_update': {
      const signalId = asString(record['signal_id']);
      const status = asString(record['status']);
      if (signalId === null || status === null) return null;
      return { type: 'setup_resolved', signal_id: signalId, status: status as SignalStatus };
    }
    case 'regime': {
      const regime = parseRegimeSnapshot(record['regime'] ?? record['data']);
      return regime === null ? null : { type: 'regime', regime };
    }
    case 'kill_switch': {
      const killSwitch = parseKillSwitch(record['kill_switch'] ?? record['data']);
      return killSwitch === null ? null : { type: 'kill_switch', kill_switch: killSwitch };
    }
    case 'heartbeat':
    case 'pong':
      return { type: 'heartbeat', ts: asString(record['ts']) ?? new Date().toISOString() };
    default:
      return null;
  }
}

// ── Client ──────────────────────────────────────────────────────────────────

export class DashboardSocket {
  private readonly url: string;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly heartbeatMs: number;
  private readonly handlers: DashboardSocketHandlers;

  private socket: WebSocket | null = null;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private stopped = false;
  private state: SocketState = 'closed';

  constructor(options: DashboardSocketOptions = {}) {
    this.url = `${resolveWebSocketBase()}${options.path ?? DEFAULTS.path}`;
    this.baseDelayMs = options.baseDelayMs ?? DEFAULTS.baseDelayMs;
    this.maxDelayMs = options.maxDelayMs ?? DEFAULTS.maxDelayMs;
    this.heartbeatMs = options.heartbeatMs ?? DEFAULTS.heartbeatMs;
    this.handlers = { onEvent: options.onEvent, onStateChange: options.onStateChange };
  }

  get currentState(): SocketState {
    return this.state;
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  /** Close for good; no further reconnect attempts. */
  close(): void {
    this.stopped = true;
    this.clearTimers();
    if (this.socket !== null) {
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
      this.socket.onopen = null;
      if (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING) {
        this.socket.close(1000, 'client shutdown');
      }
      this.socket = null;
    }
    this.setState('closed');
  }

  /** Force an immediate reconnect — e.g. the app returned to the foreground. */
  refresh(): void {
    if (this.stopped) return;
    if (this.socket !== null && this.socket.readyState === WebSocket.OPEN) return;
    this.clearTimers();
    this.attempt = 0;
    this.open();
  }

  private setState(next: SocketState): void {
    if (this.state === next) return;
    this.state = next;
    this.handlers.onStateChange?.(next);
  }

  private open(): void {
    if (this.stopped) return;
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.setState('open');
      this.startHeartbeat();
    };

    socket.onmessage = (message: MessageEvent<unknown>) => {
      if (typeof message.data !== 'string') return;
      const event = parseDashboardEvent(message.data);
      if (event !== null) this.handlers.onEvent?.(event);
    };

    socket.onerror = () => {
      // `onclose` always follows; backoff is handled there.
    };

    socket.onclose = () => {
      this.stopHeartbeat();
      this.socket = null;
      if (this.stopped) {
        this.setState('closed');
        return;
      }
      this.scheduleReconnect();
    };
  }

  /**
   * Exponential backoff with full jitter:
   *   delay = random(0, min(maxDelay, base * 2^attempt))
   * Jitter matters because every phone on the account reconnects at once when a
   * flaky cell tower comes back.
   */
  private scheduleReconnect(): void {
    this.setState('reconnecting');
    const ceiling = Math.min(this.maxDelayMs, this.baseDelayMs * 2 ** this.attempt);
    const delay = Math.max(this.baseDelayMs, Math.round(Math.random() * ceiling));
    this.attempt = Math.min(this.attempt + 1, 16);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.socket !== null && this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ type: 'ping', ts: new Date().toISOString() }));
      }
    }, this.heartbeatMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private clearTimers(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.stopHeartbeat();
  }
}

/** Convenience factory used by the hooks. */
export function createDashboardSocket(options: DashboardSocketOptions): DashboardSocket {
  return new DashboardSocket(options);
}
