import type { CapacitorConfig } from '@capacitor/cli';

/**
 * Capacitor 6 Android shell for the ATLAS HITL dashboard.
 *
 * `webDir` points at the Vite build output. Run `npm run cap:sync` to rebuild
 * the web assets and copy them into the (gitignored) `android/` project.
 */
const config: CapacitorConfig = {
  appId: 'com.atlas.trading',
  appName: 'ATLAS',
  webDir: 'dist',
  bundledWebRuntime: false,
  android: {
    allowMixedContent: false,
    captureInput: true,
    webContentsDebuggingEnabled: false,
    backgroundColor: '#05070cff',
  },
  server: {
    androidScheme: 'https',
    // Uncomment and point at your dev machine for live-reload on device:
    // url: 'http://192.168.1.10:5173',
    // cleartext: true,
  },
  plugins: {
    PushNotifications: {
      // Show the actionable heads-up notification even while the app is in the
      // foreground — a pending setup only lives for 60 seconds.
      presentationOptions: ['badge', 'sound', 'alert'],
    },
    SplashScreen: {
      launchShowDuration: 600,
      launchAutoHide: true,
      backgroundColor: '#05070cff',
      androidSplashResourceName: 'splash',
      androidScaleType: 'CENTER_CROP',
      showSpinner: false,
      splashFullScreen: true,
      splashImmersive: false,
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#05070cff',
      overlaysWebView: false,
    },
    Keyboard: {
      resize: 'body',
      style: 'DARK',
      resizeOnFullScreen: true,
    },
  },
};

export default config;
