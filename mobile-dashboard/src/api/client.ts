/**
 * Typed fetch wrapper over FROZEN CONTRACT B (IMPLEMENTATION_ARTIFACT.md §4).
 *
 * Every path in this module is verbatim from the contract:
 *
 *   GET  /api/v1/confirmation/pending
 *   GET  /api/v1/confirmation/{signal_id}
 *   POST /api/v1/confirmation/{signal_id}/confirm   body {"venue":"TRADOVATE"|"KRAKEN"}
 *   POST /api/v1/confirmation/{signal_id}/reject
 *   POST /api/v1/emergency/kill
 *   POST /api/v1/emergency/release
 *   POST /api/v1/emergency/flatten
 *   GET  /api/v1/emergency/status
 *   GET  /api/v1/health
 *
 * HTTP 410 Gone is *not* an error: it is the backend telling us the 60-second
 * Redis TTL lapsed before the tap landed. It is surfaced as the distinct
 * `{ kind: 'expired' }` outcome so the UI can retire the card quietly instead of
 * showing a failure.
 *
 * No `any` anywhere in this file.
 */

import type {
  ConfirmRequest,
  FlattenResult,
  HealthStatus,
  KillSwitchStatus,
  OrderResult,
  PendingSetup,
  RejectRequest,
  Venue,
} from './types';

// ── Configuration ───────────────────────────────────────────────────────────

/**
 * Origin of the FastAPI middleware.
 *
 * Empty string ⇒ same origin, which is what we want under `npm run dev`
 * (the Vite proxy forwards `/api`) and for a web deploy behind one reverse
 * proxy. The Capacitor Android shell serves from `https://localhost` and cannot
 * proxy, so it must set `VITE_API_BASE_URL` to an absolute origin.
 */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '');

/** Base of the live dashboard WebSocket, derived from the REST origin. */
export function resolveWebSocketBase(): string {
  const explicit = import.meta.env.VITE_WS_BASE_URL;
  if (explicit && explicit.length > 0) return explicit.replace(/\/+$/, '');
  if (API_BASE_URL.length > 0) return API_BASE_URL.replace(/^http/, 'ws');
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}`;
}

/** Default request timeout. A pending setup only lives 60s — fail fast. */
const DEFAULT_TIMEOUT_MS = 8_000;

// ── Error & outcome types ───────────────────────────────────────────────────

/** Any non-2xx response other than the expiry-signalling 410. */
export class ApiError extends Error {
  readonly status: number;
  readonly path: string;
  readonly detail: string | null;

  constructor(status: number, path: string, detail: string | null, message?: string) {
    super(message ?? `${status} on ${path}${detail ? `: ${detail}` : ''}`);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
    this.detail = detail;
  }

  /** Network unreachable, DNS failure, CORS block or timeout. */
  get isOffline(): boolean {
    return this.status === 0;
  }
}

/**
 * Result of an operation that can legitimately find the setup already gone.
 *
 * `expired` is a success-shaped terminal outcome, not a failure: the state
 * machine moved PENDING_APPROVAL → EXPIRED with zero broker interaction.
 */
export type ConfirmationOutcome<T> =
  | { kind: 'ok'; data: T }
  | { kind: 'expired'; signalId: string; detail: string | null };

export function isExpired<T>(
  outcome: ConfirmationOutcome<T>,
): outcome is { kind: 'expired'; signalId: string; detail: string | null } {
  return outcome.kind === 'expired';
}

// ── Core transport ──────────────────────────────────────────────────────────

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  /** Treat 410 Gone as a non-error outcome instead of throwing. */
  allowGone?: boolean;
}

interface RawResponse {
  status: number;
  /** Parsed JSON body, or null for 204/empty responses. */
  payload: unknown;
}

function joinUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

function extractDetail(payload: unknown): string | null {
  if (typeof payload === 'string') return payload;
  if (payload !== null && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    const detail = record['detail'] ?? record['message'] ?? record['error'];
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first !== null && typeof first === 'object') {
        const msg = (first as Record<string, unknown>)['msg'];
        if (typeof msg === 'string') return msg;
      }
    }
  }
  return null;
}

/** Single place where `fetch` is called. Never throws on 410 when allowed. */
async function request(path: string, options: RequestOptions = {}): Promise<RawResponse> {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS, allowGone = false } = options;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(joinUrl(path), {
      method,
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (cause) {
    const aborted = cause instanceof DOMException && cause.name === 'AbortError';
    throw new ApiError(
      0,
      path,
      aborted ? 'request timed out' : 'network unreachable',
      aborted ? `Timed out after ${timeoutMs}ms on ${path}` : `Network error on ${path}`,
    );
  } finally {
    clearTimeout(timer);
  }

  let payload: unknown = null;
  const text = await response.text();
  if (text.length > 0) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = text;
    }
  }

  if (response.status === 410 && allowGone) {
    return { status: 410, payload };
  }
  if (!response.ok) {
    throw new ApiError(response.status, path, extractDetail(payload));
  }
  return { status: response.status, payload };
}

/**
 * Runtime shape guard. The backend is the source of truth for the schema, so we
 * only assert enough to catch a proxy serving HTML where JSON was expected.
 */
function expectObject(payload: unknown, path: string): Record<string, unknown> {
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ApiError(502, path, 'malformed response body (expected a JSON object)');
  }
  return payload as Record<string, unknown>;
}

function expectArray(payload: unknown, path: string): unknown[] {
  if (Array.isArray(payload)) return payload;
  // Tolerate `{"setups": [...]}` / `{"items": [...]}` envelopes.
  if (payload !== null && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    for (const key of ['setups', 'items', 'pending', 'results', 'data'] as const) {
      const value = record[key];
      if (Array.isArray(value)) return value;
    }
  }
  throw new ApiError(502, path, 'malformed response body (expected a JSON array)');
}

// ── Confirmation endpoints ──────────────────────────────────────────────────

/** `GET /api/v1/confirmation/pending` — every live (unexpired) setup. */
export async function fetchPendingSetups(signal?: AbortSignal): Promise<PendingSetup[]> {
  const path = '/api/v1/confirmation/pending';
  const { payload } = await request(path, { signal });
  return expectArray(payload, path) as PendingSetup[];
}

/**
 * `GET /api/v1/confirmation/{signal_id}` — single setup detail.
 * Returns `{ kind: 'expired' }` when the TTL already lapsed (410).
 */
export async function fetchSetup(
  signalId: string,
  signal?: AbortSignal,
): Promise<ConfirmationOutcome<PendingSetup>> {
  const path = `/api/v1/confirmation/${encodeURIComponent(signalId)}`;
  const { status, payload } = await request(path, { signal, allowGone: true });
  if (status === 410) {
    return { kind: 'expired', signalId, detail: extractDetail(payload) };
  }
  return { kind: 'ok', data: expectObject(payload, path) as unknown as PendingSetup };
}

/**
 * `POST /api/v1/confirmation/{signal_id}/confirm` with body `{"venue": …}`.
 *
 * This is the 1-tap call behind [Confirm Tradovate] and [Route Kraken].
 */
export async function confirmSetup(
  signalId: string,
  venue: Venue,
  signal?: AbortSignal,
): Promise<ConfirmationOutcome<OrderResult>> {
  const path = `/api/v1/confirmation/${encodeURIComponent(signalId)}/confirm`;
  const body: ConfirmRequest = { venue };
  const { status, payload } = await request(path, { method: 'POST', body, signal, allowGone: true });
  if (status === 410) {
    return { kind: 'expired', signalId, detail: extractDetail(payload) };
  }
  return { kind: 'ok', data: expectObject(payload, path) as unknown as OrderResult };
}

/** `POST /api/v1/confirmation/{signal_id}/reject` — transition → REJECTED. */
export async function rejectSetup(
  signalId: string,
  reason?: string,
  signal?: AbortSignal,
): Promise<ConfirmationOutcome<OrderResult>> {
  const path = `/api/v1/confirmation/${encodeURIComponent(signalId)}/reject`;
  const body: RejectRequest = reason === undefined ? {} : { reason };
  const { status, payload } = await request(path, { method: 'POST', body, signal, allowGone: true });
  if (status === 410) {
    return { kind: 'expired', signalId, detail: extractDetail(payload) };
  }
  return { kind: 'ok', data: expectObject(payload, path) as unknown as OrderResult };
}

// ── Emergency endpoints ─────────────────────────────────────────────────────

/** `POST /api/v1/emergency/kill` — engage the global kill switch. */
export async function engageKillSwitch(reason?: string, signal?: AbortSignal): Promise<KillSwitchStatus> {
  const path = '/api/v1/emergency/kill';
  const { payload } = await request(path, {
    method: 'POST',
    body: reason === undefined ? {} : { reason },
    signal,
  });
  return expectObject(payload, path) as unknown as KillSwitchStatus;
}

/** `POST /api/v1/emergency/release` — disengage the global kill switch. */
export async function releaseKillSwitch(signal?: AbortSignal): Promise<KillSwitchStatus> {
  const path = '/api/v1/emergency/release';
  const { payload } = await request(path, { method: 'POST', body: {}, signal });
  return expectObject(payload, path) as unknown as KillSwitchStatus;
}

/** `POST /api/v1/emergency/flatten` — cancel working orders and flatten all venues. */
export async function flattenAll(signal?: AbortSignal): Promise<FlattenResult> {
  const path = '/api/v1/emergency/flatten';
  const { payload } = await request(path, { method: 'POST', body: {}, signal, timeoutMs: 20_000 });
  return expectObject(payload, path) as unknown as FlattenResult;
}

/** `GET /api/v1/emergency/status` — kill-switch + daily drawdown state. */
export async function fetchEmergencyStatus(signal?: AbortSignal): Promise<KillSwitchStatus> {
  const path = '/api/v1/emergency/status';
  const { payload } = await request(path, { signal });
  return expectObject(payload, path) as unknown as KillSwitchStatus;
}

// ── Health ──────────────────────────────────────────────────────────────────

/** `GET /api/v1/health` — liveness/readiness. */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthStatus> {
  const path = '/api/v1/health';
  const { payload } = await request(path, { signal, timeoutMs: 4_000 });
  return expectObject(payload, path) as unknown as HealthStatus;
}

// ── Escape hatch for adjacent (non-contract) endpoints ───────────────────────

/**
 * POST to a path outside FROZEN CONTRACT B. Used solely by `src/push/fcm.ts` to
 * register the device token, which the contract does not enumerate.
 */
export async function postOutsideContract<T>(path: string, body: unknown): Promise<T> {
  const { payload } = await request(path, { method: 'POST', body });
  return payload as T;
}
