/**
 * Push registration and notification-action routing — both worlds.
 *
 *   • Native Android (Capacitor shell) → `@capacitor/push-notifications`
 *   • Browser PWA                      → `firebase/messaging` + `getToken()`
 *
 * Whichever path yields a token, it is registered with the backend so the FCM
 * HTTP v1 sender can reach this device, and the last-registered value is cached
 * in `@capacitor/preferences` so a warm start does not re-POST it.
 *
 * The three notification actions mirror the buttons on `PendingSetupCard` and
 * the ones rendered by `public/firebase-messaging-sw.js`:
 *
 *     confirm_tradovate → POST /api/v1/confirmation/{id}/confirm {"venue":"TRADOVATE"}
 *     route_kraken      → POST /api/v1/confirmation/{id}/confirm {"venue":"KRAKEN"}
 *     reject            → POST /api/v1/confirmation/{id}/reject
 *
 * A tap on one of those goes STRAIGHT into the 1-tap call — no intermediate
 * screen — because the setup only lives for 60 seconds.
 *
 * No secrets: the Firebase web config comes from import.meta.env (all public
 * client config) and the FCM service account never leaves the backend.
 */

import { Capacitor } from '@capacitor/core';
import { Preferences } from '@capacitor/preferences';
import {
  PushNotifications,
  type ActionPerformed,
  type PushNotificationSchema,
  type Token,
} from '@capacitor/push-notifications';
import { initializeApp, type FirebaseApp, type FirebaseOptions } from 'firebase/app';
import { getMessaging, getToken, isSupported, onMessage, type Messaging } from 'firebase/messaging';

import { API_BASE_URL, postOutsideContract } from '../api/client';
import type { DeviceRegistration, PushPlatform, Venue } from '../api/types';

// ── Action ids (shared with the service worker) ─────────────────────────────

export const ACTION_CONFIRM_TRADOVATE = 'confirm_tradovate';
export const ACTION_ROUTE_KRAKEN = 'route_kraken';
export const ACTION_REJECT = 'reject';

export type NotificationAction =
  | typeof ACTION_CONFIRM_TRADOVATE
  | typeof ACTION_ROUTE_KRAKEN
  | typeof ACTION_REJECT;

/**
 * High-importance Android notification channel the setups arrive on. The
 * backend must send `android.notification.channel_id` set to this value, plus
 * the three action buttons carrying the action ids above.
 */
export const ANDROID_CHANNEL_ID = 'atlas_setups';

const TOKEN_CACHE_KEY = 'atlas.push.token';
const PUSH_REGISTER_PATH = import.meta.env.VITE_PUSH_REGISTER_PATH ?? '/api/v1/push/register';

// ── Callbacks the app supplies ──────────────────────────────────────────────

export interface PushHandlers {
  /**
   * A notification action was tapped. The app turns this into exactly one
   * request against FROZEN CONTRACT B.
   */
  onAction: (signalId: string, action: NotificationAction) => void | Promise<void>;
  /** A push arrived while the app was open — refresh the pending roster. */
  onForegroundMessage?: (signalId: string | null) => void;
  /** Non-fatal diagnostics (permission denied, unsupported browser, …). */
  onStatus?: (message: string) => void;
}

export interface PushRegistration {
  platform: PushPlatform;
  token: string | null;
  /** Null when registration succeeded. */
  error: string | null;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function firebaseOptions(): FirebaseOptions | null {
  const apiKey = import.meta.env.VITE_FIREBASE_API_KEY;
  const projectId = import.meta.env.VITE_FIREBASE_PROJECT_ID;
  const appId = import.meta.env.VITE_FIREBASE_APP_ID;
  const messagingSenderId = import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID;
  if (!apiKey || !projectId || !appId || !messagingSenderId) return null;
  return {
    apiKey,
    projectId,
    appId,
    messagingSenderId,
    authDomain: `${projectId}.firebaseapp.com`,
    storageBucket: `${projectId}.appspot.com`,
  };
}

/** Map anything the transport hands us onto a known action id. */
function normaliseAction(raw: string | undefined): NotificationAction | null {
  switch (raw) {
    case ACTION_CONFIRM_TRADOVATE:
    case 'confirm':
    case 'tradovate':
      return ACTION_CONFIRM_TRADOVATE;
    case ACTION_ROUTE_KRAKEN:
    case 'kraken':
      return ACTION_ROUTE_KRAKEN;
    case ACTION_REJECT:
    case 'dismiss':
      return ACTION_REJECT;
    default:
      return null;
  }
}

/** The venue a confirm action implies; null for a reject. */
export function venueForAction(action: NotificationAction): Venue | null {
  if (action === ACTION_CONFIRM_TRADOVATE) return 'TRADOVATE';
  if (action === ACTION_ROUTE_KRAKEN) return 'KRAKEN';
  return null;
}

function readSignalId(data: Record<string, unknown> | undefined): string | null {
  if (data === undefined) return null;
  const value = data['signal_id'] ?? data['signalId'];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

async function registerWithBackend(token: string, platform: PushPlatform): Promise<void> {
  const cached = await Preferences.get({ key: TOKEN_CACHE_KEY }).catch(() => ({ value: null }));
  if (cached.value === token) return;

  const body: DeviceRegistration = { token, platform, app_id: 'com.atlas.trading' };
  await postOutsideContract<unknown>(PUSH_REGISTER_PATH, body);
  await Preferences.set({ key: TOKEN_CACHE_KEY, value: token }).catch(() => undefined);
}

// ── Native Android (Capacitor) ──────────────────────────────────────────────

async function registerNative(handlers: PushHandlers): Promise<PushRegistration> {
  const permission = await PushNotifications.checkPermissions();
  let receive = permission.receive;
  if (receive === 'prompt' || receive === 'prompt-with-rationale') {
    receive = (await PushNotifications.requestPermissions()).receive;
  }
  if (receive !== 'granted') {
    return { platform: 'android', token: null, error: 'Notification permission denied' };
  }

  /*
   * Android delivers the three HITL buttons from the FCM message itself — the
   * backend attaches them under `android.notification` with the action ids
   * below, and Capacitor 6 reports the tapped one as `event.actionId`. All the
   * client owes is a high-importance channel so the notification arrives as a
   * heads-up alert with sound on the lock screen; a setup only lives 60s.
   */
  await PushNotifications.createChannel({
    id: ANDROID_CHANNEL_ID,
    name: 'ATLAS setups',
    description: 'Pending trade setups awaiting a 1-tap decision.',
    importance: 5,
    visibility: 1,
    sound: 'default',
    vibration: true,
    lights: true,
  }).catch(() => undefined);

  const token = await new Promise<string | null>((resolve) => {
    const timeout = setTimeout(() => resolve(null), 10_000);
    void PushNotifications.addListener('registration', (value: Token) => {
      clearTimeout(timeout);
      resolve(value.value);
    });
    void PushNotifications.addListener('registrationError', () => {
      clearTimeout(timeout);
      resolve(null);
    });
    void PushNotifications.register();
  });

  // Foreground delivery — the app is open, so just refresh the roster.
  void PushNotifications.addListener('pushNotificationReceived', (notification: PushNotificationSchema) => {
    handlers.onForegroundMessage?.(readSignalId(notification.data as Record<string, unknown> | undefined));
  });

  // The 1-tap path: an action button straight into the confirmation call.
  void PushNotifications.addListener('pushNotificationActionPerformed', (event: ActionPerformed) => {
    const data = event.notification.data as Record<string, unknown> | undefined;
    const signalId = readSignalId(data);
    if (signalId === null) return;
    const action = normaliseAction(event.actionId);
    if (action === null) {
      // Body tap — surface the card rather than acting blind.
      handlers.onForegroundMessage?.(signalId);
      return;
    }
    void handlers.onAction(signalId, action);
  });

  if (token === null) {
    return { platform: 'android', token: null, error: 'FCM registration timed out' };
  }

  try {
    await registerWithBackend(token, 'android');
  } catch {
    return { platform: 'android', token, error: 'Token obtained but backend registration failed' };
  }
  return { platform: 'android', token, error: null };
}

// ── Browser PWA (firebase/messaging) ────────────────────────────────────────

let webApp: FirebaseApp | null = null;
let webMessaging: Messaging | null = null;

/**
 * Register the FCM background worker under its OWN scope so it does not fight
 * the vite-plugin-pwa app-shell worker for `/`. Firebase config travels as
 * query parameters — all of it is public client config.
 */
async function registerFcmServiceWorker(options: FirebaseOptions): Promise<ServiceWorkerRegistration> {
  const params = new URLSearchParams({
    apiKey: String(options.apiKey ?? ''),
    projectId: String(options.projectId ?? ''),
    appId: String(options.appId ?? ''),
    messagingSenderId: String(options.messagingSenderId ?? ''),
    apiBase: API_BASE_URL,
  });
  return navigator.serviceWorker.register(`/firebase-messaging-sw.js?${params.toString()}`, {
    scope: '/firebase-messaging-sw-scope/',
  });
}

async function registerWeb(handlers: PushHandlers): Promise<PushRegistration> {
  const options = firebaseOptions();
  if (options === null) {
    return { platform: 'web', token: null, error: 'Firebase env vars are not configured' };
  }
  if (!('serviceWorker' in navigator) || !('Notification' in window)) {
    return { platform: 'web', token: null, error: 'Push is unsupported in this browser' };
  }
  if (!(await isSupported())) {
    return { platform: 'web', token: null, error: 'firebase/messaging is unsupported here' };
  }

  const permission = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
  if (permission !== 'granted') {
    return { platform: 'web', token: null, error: 'Notification permission denied' };
  }

  const vapidKey = import.meta.env.VITE_FIREBASE_VAPID_KEY;
  if (!vapidKey) {
    return { platform: 'web', token: null, error: 'VITE_FIREBASE_VAPID_KEY is not set' };
  }

  webApp = webApp ?? initializeApp(options);
  webMessaging = webMessaging ?? getMessaging(webApp);

  let serviceWorkerRegistration: ServiceWorkerRegistration;
  try {
    serviceWorkerRegistration = await registerFcmServiceWorker(options);
  } catch {
    return { platform: 'web', token: null, error: 'Could not register the FCM service worker' };
  }

  // Messages delivered while the page is open bypass the worker.
  onMessage(webMessaging, (payload) => {
    handlers.onForegroundMessage?.(readSignalId(payload.data as Record<string, unknown> | undefined));
  });

  // The worker posts back when an action button was tapped while the app was
  // closed, and deep-links here when it could not complete the call itself.
  navigator.serviceWorker.addEventListener('message', (event: MessageEvent<unknown>) => {
    const data = event.data;
    if (data === null || typeof data !== 'object') return;
    const record = data as Record<string, unknown>;

    if (record['type'] === 'atlas:action-result') {
      const signalId = typeof record['signalId'] === 'string' ? record['signalId'] : null;
      if (signalId !== null) handlers.onForegroundMessage?.(signalId);
      return;
    }

    if (record['type'] === 'atlas:navigate' && typeof record['path'] === 'string') {
      const parsed = new URL(record['path'], window.location.origin);
      const signalId = parsed.searchParams.get('signal_id');
      const action = normaliseAction(parsed.searchParams.get('action') ?? undefined);
      if (signalId !== null && action !== null) {
        void handlers.onAction(signalId, action);
      } else if (signalId !== null) {
        handlers.onForegroundMessage?.(signalId);
      }
    }
  });

  let token: string;
  try {
    token = await getToken(webMessaging, { vapidKey, serviceWorkerRegistration });
  } catch {
    return { platform: 'web', token: null, error: 'getToken() failed' };
  }
  if (!token) {
    return { platform: 'web', token: null, error: 'FCM returned an empty token' };
  }

  try {
    await registerWithBackend(token, 'web');
  } catch {
    return { platform: 'web', token, error: 'Token obtained but backend registration failed' };
  }
  return { platform: 'web', token, error: null };
}

// ── Entry point ─────────────────────────────────────────────────────────────

/**
 * Set up push for whichever runtime we are in and wire the action handlers.
 * Safe to call once on mount; never throws.
 */
export async function initialisePush(handlers: PushHandlers): Promise<PushRegistration> {
  // React StrictMode double-invokes mount effects in development, and a second
  // native listener would route one notification tap into two orders.
  if (inFlightRegistration !== null) return inFlightRegistration;
  inFlightRegistration = runInitialisePush(handlers);
  return inFlightRegistration;
}

let inFlightRegistration: Promise<PushRegistration> | null = null;

async function runInitialisePush(handlers: PushHandlers): Promise<PushRegistration> {
  try {
    const result = Capacitor.isNativePlatform() ? await registerNative(handlers) : await registerWeb(handlers);
    if (result.error !== null) handlers.onStatus?.(result.error);
    return result;
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : 'Push initialisation failed';
    handlers.onStatus?.(message);
    return { platform: Capacitor.isNativePlatform() ? 'android' : 'web', token: null, error: message };
  }
}

/**
 * Handle a cold start that came from a notification deep link, e.g.
 * `/?signal_id=…&action=route_kraken` written by the background worker.
 * Returns the action it dispatched, or null.
 */
export function consumeLaunchAction(
  handlers: Pick<PushHandlers, 'onAction' | 'onForegroundMessage'>,
): NotificationAction | null {
  const params = new URLSearchParams(window.location.search);
  const signalId = params.get('signal_id');
  if (signalId === null) return null;

  const action = normaliseAction(params.get('action') ?? undefined);

  // Clean the URL so a refresh does not re-fire the order.
  const clean = new URL(window.location.href);
  clean.searchParams.delete('signal_id');
  clean.searchParams.delete('action');
  window.history.replaceState({}, '', clean.toString());

  if (action === null) {
    handlers.onForegroundMessage?.(signalId);
    return null;
  }
  void handlers.onAction(signalId, action);
  return action;
}
