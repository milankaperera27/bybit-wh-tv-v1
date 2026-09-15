/* eslint-disable no-undef */
/**
 * ATLAS — Firebase Cloud Messaging background worker.
 *
 * Registered by `src/push/fcm.ts` under its OWN scope
 * (`/firebase-messaging-sw-scope/`) so it never competes with the
 * vite-plugin-pwa app-shell service worker that owns `/`.
 *
 * NO SECRETS LIVE IN THIS FILE. The Firebase web config and the API base URL
 * are handed in as query parameters on the registration URL, e.g.
 *   /firebase-messaging-sw.js?apiKey=...&projectId=...&apiBase=https://...
 * and the registering page sources them from import.meta.env.
 *
 * Responsibility: render the actionable heads-up notification carrying the
 * three HITL buttons — [Confirm Tradovate] [Route Kraken] [Reject] — and turn a
 * button tap into exactly one call against FROZEN CONTRACT B (§4), even while
 * the app is closed.
 */

importScripts('https://www.gstatic.com/firebasejs/11.0.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/11.0.2/firebase-messaging-compat.js');

const PARAMS = new URL(self.location.href).searchParams;

const FIREBASE_CONFIG = {
  apiKey: PARAMS.get('apiKey') || '',
  projectId: PARAMS.get('projectId') || '',
  appId: PARAMS.get('appId') || '',
  messagingSenderId: PARAMS.get('messagingSenderId') || '',
  // Derived — never configured separately.
  authDomain: `${PARAMS.get('projectId') || ''}.firebaseapp.com`,
  storageBucket: `${PARAMS.get('projectId') || ''}.appspot.com`,
};

/** Origin of the FastAPI middleware. Empty string = same origin as this worker. */
const API_BASE = (PARAMS.get('apiBase') || '').replace(/\/+$/, '');

/** Notification action ids, mirrored by src/push/fcm.ts. */
const ACTION_CONFIRM_TRADOVATE = 'confirm_tradovate';
const ACTION_ROUTE_KRAKEN = 'route_kraken';
const ACTION_REJECT = 'reject';

const NOTIFICATION_ACTIONS = [
  { action: ACTION_CONFIRM_TRADOVATE, title: 'Confirm Tradovate' },
  { action: ACTION_ROUTE_KRAKEN, title: 'Route Kraken' },
  { action: ACTION_REJECT, title: 'Reject' },
];

if (FIREBASE_CONFIG.apiKey && FIREBASE_CONFIG.projectId) {
  firebase.initializeApp(FIREBASE_CONFIG);
  const messaging = firebase.messaging();

  messaging.onBackgroundMessage((payload) => {
    const data = payload && payload.data ? payload.data : {};
    showSetupNotification(data);
  });
}

/**
 * Data-only FCM messages bypass onBackgroundMessage in some Chrome builds, so
 * the raw `push` event is handled too. Both paths funnel into one renderer and
 * the tag dedupes them.
 */
self.addEventListener('push', (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch {
    return;
  }
  const data = payload.data || payload || {};
  if (!data.signal_id) return;
  event.waitUntil(showSetupNotification(data));
});

function money(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—';
}

function showSetupNotification(data) {
  const symbol = data.symbol || 'SETUP';
  const action = (data.action || '').toUpperCase();
  const regime = data.regime || 'UNKNOWN';
  const entry = money(data.limit_price);
  const stop = money(data.stop_loss);
  const tp1 = money(data.take_profit_1);
  const risk = data.risk_pct ? `${data.risk_pct}%` : '—';

  const title = `${action || 'SIGNAL'} ${symbol} — confirm in 60s`;
  const body =
    `Entry ${entry} · Stop ${stop} · TP1 ${tp1}\n` +
    `Risk ${risk} · ${regime.replace(/_/g, ' ')}`;

  return self.registration.showNotification(title, {
    body,
    tag: `atlas-setup-${data.signal_id}`,
    renotify: true,
    requireInteraction: true,
    silent: false,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    vibrate: [40, 60, 40, 60, 120],
    timestamp: Date.now(),
    data: {
      signal_id: data.signal_id,
      venue: data.venue || 'TRADOVATE',
      expires_at: data.expires_at || null,
    },
    actions: NOTIFICATION_ACTIONS,
  });
}

self.addEventListener('notificationclick', (event) => {
  const notification = event.notification;
  const data = notification.data || {};
  const signalId = data.signal_id;
  notification.close();

  if (!signalId) {
    event.waitUntil(focusApp('/'));
    return;
  }

  const action = event.action;

  // Plain body tap: just surface the card in the app.
  if (!action) {
    event.waitUntil(focusApp(`/?signal_id=${encodeURIComponent(signalId)}`));
    return;
  }

  event.waitUntil(dispatchAction(signalId, action));
});

/**
 * Fire the single request the tap represents. Falls back to deep-linking into
 * the app (which retries through the in-app client) if the direct call fails —
 * e.g. the auth cookie is absent or the device is offline.
 */
async function dispatchAction(signalId, action) {
  const id = encodeURIComponent(signalId);
  let url;
  let body;

  if (action === ACTION_REJECT) {
    url = `${API_BASE}/api/v1/confirmation/${id}/reject`;
    body = '{}';
  } else if (action === ACTION_ROUTE_KRAKEN) {
    url = `${API_BASE}/api/v1/confirmation/${id}/confirm`;
    body = JSON.stringify({ venue: 'KRAKEN' });
  } else if (action === ACTION_CONFIRM_TRADOVATE) {
    url = `${API_BASE}/api/v1/confirmation/${id}/confirm`;
    body = JSON.stringify({ venue: 'TRADOVATE' });
  } else {
    return focusApp(`/?signal_id=${id}`);
  }

  try {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body,
    });

    if (response.status === 410) {
      // Setup already expired — a normal outcome, not a failure.
      return toast('Setup expired', `${signalId} lapsed before the tap landed.`);
    }
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const label =
      action === ACTION_REJECT
        ? 'Rejected'
        : action === ACTION_ROUTE_KRAKEN
          ? 'Routed to Kraken'
          : 'Confirmed on Tradovate';
    await notifyClients({ type: 'atlas:action-result', signalId, action, ok: true });
    return toast(label, `${signalId}`);
  } catch (err) {
    await notifyClients({ type: 'atlas:action-result', signalId, action, ok: false });
    // Hand off to the app so the user can retry with one more tap.
    return focusApp(`/?signal_id=${id}&action=${encodeURIComponent(action)}`);
  }
}

function toast(title, body) {
  return self.registration.showNotification(title, {
    body,
    tag: 'atlas-result',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    requireInteraction: false,
  });
}

async function notifyClients(message) {
  const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clientList) {
    client.postMessage(message);
  }
}

async function focusApp(path) {
  const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clientList) {
    if ('focus' in client) {
      client.postMessage({ type: 'atlas:navigate', path });
      return client.focus();
    }
  }
  if (self.clients.openWindow) {
    return self.clients.openWindow(path);
  }
  return undefined;
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
