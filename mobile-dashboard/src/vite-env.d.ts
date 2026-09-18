/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

/**
 * Typed view of the build-time environment. Every key is public: it is inlined
 * into the browser bundle. Secrets belong in the backend `.env` only.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_WS_BASE_URL?: string;
  readonly VITE_FIREBASE_API_KEY?: string;
  readonly VITE_FIREBASE_PROJECT_ID?: string;
  readonly VITE_FIREBASE_APP_ID?: string;
  readonly VITE_FIREBASE_MESSAGING_SENDER_ID?: string;
  readonly VITE_FIREBASE_VAPID_KEY?: string;
  readonly VITE_ACCOUNT_EQUITY?: string;
  readonly VITE_PUSH_REGISTER_PATH?: string;
  readonly VITE_DEV_PROXY_TARGET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
