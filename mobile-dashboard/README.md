# ATLAS — Mobile HITL Dashboard

LAYER 7 of the ATLAS build plan: the installable Android PWA that puts a
human in the loop for 60 seconds before any order reaches a venue.

**Stack:** Capacitor 6 · React 18 · TypeScript (strict) · Vite 5 · vite-plugin-pwa · Firebase Cloud Messaging

It talks to exactly one thing — the FastAPI middleware described by
**FROZEN CONTRACT B** (`../IMPLEMENTATION_ARTIFACT.md` §4). It owns no trading
logic, holds no broker credentials, and can do nothing the backend would not let
any other authenticated client do.

---

## What it does

A validated TradingView alert becomes a `PENDING_APPROVAL` record held in Redis
under a 60-second TTL, and a high-priority FCM push lands on the phone. From
there the trader has one job and 60 seconds to do it:

```
  push notification            in-app card
  ┌──────────────────┐        ┌──────────────────────────────┐
  │ BUY NQ1! — 60s   │        │  BUY  NQ1!        ◷ 41       │
  │ [Confirm Tradovate]│  ==>  │  entry / stop / TP1 / TP2    │
  │ [Route Kraken]   │        │  1.50R · 3.00R · $1,000 risk │
  │ [Reject]         │        │ [Confirm Tradovate][Route Kraken]│
  └──────────────────┘        │ [          Reject           ]│
                              └──────────────────────────────┘
```

Both surfaces fire the *same single request*. Tapping an action button on the
lock screen goes straight to the backend — there is no intermediate screen,
because there is no time for one.

### Design rules this client enforces

| Rule | Where |
|------|-------|
| Expiry costs **zero** network calls — the card retires itself off `expires_at` | `hooks/useCountdown.ts`, `components/PendingSetupCard.tsx` |
| HTTP **410 Gone** is a normal outcome ("already expired"), never an error | `api/client.ts` → `ConfirmationOutcome` |
| One tap = one request; a second tap cannot land | `submittedRef` + `inFlight` set + disabled buttons |
| Kill switch needs a 2-second hold **and** a confirm tap | `components/KillSwitchButton.tsx` |
| Kill before flatten, always | `hooks/useKillSwitch.ts` |
| No secrets in the bundle | everything via `import.meta.env` / `.env.example` |

---

## Layout

```
mobile-dashboard/
├── capacitor.config.ts          # appId com.atlas.trading, androidScheme https
├── vite.config.ts               # React + PWA manifest + /api & /ws dev proxy
├── index.html
├── .env.example                 # copy to .env.local
├── public/
│   ├── manifest.webmanifest
│   ├── firebase-messaging-sw.js # background FCM + the three action buttons
│   └── icon-{192,512}.png, icon-maskable-512.png, favicon.svg
└── src/
    ├── api/
    │   ├── types.ts             # PendingSetup, Venue, Action, Regime, SignalStatus,
    │   │                        #   KillSwitchStatus, OrderResult, DashboardEvent
    │   ├── client.ts            # typed fetch over the §4 endpoints; 410 handling
    │   └── socket.ts            # /ws/dashboard, exponential backoff + jitter
    ├── push/fcm.ts              # Capacitor native + firebase/messaging web
    ├── components/
    │   ├── PendingSetupCard.tsx # ← the 1-tap confirmation widget (spec §6)
    │   ├── RegimeRadar.tsx      # ← 5m/1h/Daily heatmap (spec §6)
    │   ├── KillSwitchButton.tsx # ← emergency liquidation (spec §6)
    │   ├── StatusBar.tsx
    │   └── Toast.tsx
    ├── hooks/
    │   ├── usePendingSetups.ts  # REST + socket + local expiry, owns the mutations
    │   ├── useCountdown.ts      # rAF countdown ring, visibility-aware
    │   └── useKillSwitch.ts     # emergency status polling + engage/release
    ├── lib/{setup,haptics}.ts   # R-multiple + dollar risk; Capacitor/Web haptics
    └── styles/*.css             # mobile-first dark theme, safe-area, 44px+ targets
```

---

## Dev run

```bash
cp .env.example .env.local     # fill in VITE_API_BASE_URL at minimum
npm install
npm run dev                    # http://localhost:5173
```

The dev server proxies `/api` **and** `/ws` to `http://localhost:8000`, so with
the backend stack up (`docker compose up` from the repo root) nothing else needs
configuring. Point the proxy elsewhere with `VITE_DEV_PROXY_TARGET`.

Open it on your phone over the LAN with `npm run dev -- --host` and the machine's
IP; the countdown ring and the thumb-zone layout only really make sense on glass.

### Scripts

| Script | Does |
|--------|------|
| `npm run dev` | Vite dev server with the `/api` + `/ws` proxy |
| `npm run build` | `typecheck` then a production PWA build into `dist/` |
| `npm run preview` | serve the built `dist/` on :4173 |
| `npm run typecheck` | `tsc --noEmit` over both tsconfigs, strict |
| `npm run cap:sync` | build, then `cap sync android` |
| `npm run cap:android` | `cap open android` (Android Studio) |

---

## PWA build

```bash
npm run build      # -> dist/ (app shell precached by vite-plugin-pwa)
npm run preview
```

`dist/` contains the app shell, the generated `manifest.webmanifest`, the
workbox service worker `sw.js`, and the untouched `firebase-messaging-sw.js`.

Serve `dist/` from **HTTPS** — service workers, push, and "Add to home screen"
all require a secure origin. Chrome for Android then offers the install prompt
and the app launches `standalone` with no browser chrome.

Two service workers live on the origin and deliberately do **not** overlap:

* `sw.js` (workbox) owns scope `/` — the app shell. `/api/v1/*` is `NetworkOnly`,
  because a cached confirmation inside a 60-second window is worse than no data.
* `firebase-messaging-sw.js` is registered by `src/push/fcm.ts` under scope
  `/firebase-messaging-sw-scope/` and is excluded from the precache, so it can be
  updated independently.

---

## Android (Capacitor 6)

The `android/` directory is **gitignored** and generated locally:

```bash
npm run build
npx cap add android        # once, per checkout
npm run cap:sync           # build + copy web assets into android/
npm run cap:android        # open in Android Studio, then Run
```

`capacitor.config.ts` sets `appId: com.atlas.trading`, `appName: ATLAS`,
`webDir: dist`, `server.androidScheme: https`, the `PushNotifications`
presentation options, and the splash / status-bar styling.

Because the Android shell serves from `https://localhost` it **cannot** use the
Vite proxy — set `VITE_API_BASE_URL` to an absolute origin in `.env.local`
*before* `npm run cap:sync`, and make sure the backend's CORS allows it.

After `npx cap add android`, add the FCM plugin wiring:

1. Drop `google-services.json` (Firebase console → Project settings → Android app,
   package name `com.atlas.trading`) into `android/app/`.
2. `android/build.gradle` → `classpath 'com.google.gms:google-services:4.4.2'`
3. `android/app/build.gradle` → `apply plugin: 'com.google.gms.google-services'`
4. On Android 13+ the runtime `POST_NOTIFICATIONS` permission is requested by
   `initialisePush()` on first launch.

---

## Wiring FCM

### 1. Client config

All six values are **public** client config — none of them is a secret, and none
of them can send a push on their own. Copy them into `.env.local`:

```
VITE_FIREBASE_API_KEY=              # Project settings → General → Your apps
VITE_FIREBASE_PROJECT_ID=
VITE_FIREBASE_APP_ID=
VITE_FIREBASE_MESSAGING_SENDER_ID=
VITE_FIREBASE_VAPID_KEY=            # Cloud Messaging → Web Push certificates
```

The sending credential — the FCM service-account JSON used by
`backend/app/push/fcm.py` for HTTP v1 — lives **only** in the backend `.env`.

### 2. Token registration

`initialisePush()` picks the right path automatically:

* **native Android** → `@capacitor/push-notifications` `register()`, plus a
  high-importance notification channel `atlas_setups` so setups arrive as
  heads-up alerts on the lock screen;
* **browser PWA** → `firebase/messaging` `getToken({ vapidKey, serviceWorkerRegistration })`.

Either way the token is POSTed once to `VITE_PUSH_REGISTER_PATH`
(default `/api/v1/push/register`) and cached in `@capacitor/preferences` so a
warm start does not re-register. That path is the **only** endpoint this client
calls that is not in FROZEN CONTRACT B — the contract does not enumerate a
device-registration route. Change the constant if the backend picks another.

### 3. Message shape the backend must send

The three action ids are the contract between the backend sender, the
background worker and the native listener:

```
confirm_tradovate → POST /api/v1/confirmation/{id}/confirm  {"venue":"TRADOVATE"}
route_kraken      → POST /api/v1/confirmation/{id}/confirm  {"venue":"KRAKEN"}
reject            → POST /api/v1/confirmation/{id}/reject
```

Send a **data message** (high priority, `ttl` ≤ 60s) carrying at least:

```jsonc
{
  "data": {
    "signal_id": "…",          // required — everything keys off this
    "symbol": "NQ1!",
    "action": "BUY",
    "venue": "TRADOVATE",
    "limit_price": "20155.25",
    "stop_loss": "20140.00",
    "take_profit_1": "20178.12",
    "risk_pct": "1.0",
    "regime": "BULL_TREND_EXPANSION",
    "expires_at": "2026-01-01T00:00:60Z"
  },
  "android": {
    "priority": "high",
    "ttl": "60s",
    "notification": { "channel_id": "atlas_setups" }
  }
}
```

On the **web**, `firebase-messaging-sw.js` renders the notification itself and
attaches the three buttons. On **native Android**, the buttons come from the FCM
payload's `android.notification` action list; Capacitor reports the tapped one as
`actionId` and `src/push/fcm.ts` routes it straight into the call.

If a lock-screen tap cannot reach the backend (offline, no cookie), the worker
deep-links into the app as `/?signal_id=…&action=…` and `consumeLaunchAction()`
retries it on the first frame — then scrubs the query string so a refresh cannot
re-fire the order.

---

## Security notes

* No secret is committed and none is bundled. Every `VITE_*` value is public
  client config; broker keys, the webhook HMAC secret and the FCM service account
  stay in the backend `.env`.
* `node_modules/`, `dist/` and `android/` are excluded by the root `.gitignore`.
* Requests are sent with `credentials: 'include'`, so the backend's own session /
  bearer scheme is what actually authorises a routing decision. This client
  assumes the middleware is **not** exposed unauthenticated to the internet.
* Strict TypeScript throughout, and no `any` in `src/api/`.
