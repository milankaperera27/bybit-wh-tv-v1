/**
 * Haptic feedback that works in both worlds.
 *
 * On the Capacitor Android shell this goes through `@capacitor/haptics`; in a
 * browser PWA it falls back to the Vibration API. Both are best-effort: a
 * failure here must never block a trade action.
 */

import { Capacitor } from '@capacitor/core';
import { Haptics, ImpactStyle, NotificationType } from '@capacitor/haptics';

const isNative = (): boolean => Capacitor.isNativePlatform();

function vibrate(pattern: number | number[]): void {
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
    try {
      navigator.vibrate(pattern);
    } catch {
      /* user agent refused — ignore */
    }
  }
}

/** Light tick — a button accepted the press. */
export async function tapFeedback(): Promise<void> {
  if (isNative()) {
    try {
      await Haptics.impact({ style: ImpactStyle.Light });
      return;
    } catch {
      /* fall through */
    }
  }
  vibrate(15);
}

/** Firm thud — a real order just left the device. */
export async function commitFeedback(): Promise<void> {
  if (isNative()) {
    try {
      await Haptics.impact({ style: ImpactStyle.Heavy });
      return;
    } catch {
      /* fall through */
    }
  }
  vibrate([25, 40, 25]);
}

/** Success pattern — the backend acknowledged. */
export async function successFeedback(): Promise<void> {
  if (isNative()) {
    try {
      await Haptics.notification({ type: NotificationType.Success });
      return;
    } catch {
      /* fall through */
    }
  }
  vibrate([12, 60, 12]);
}

/** Warning pattern — expired, rejected by risk, or a failed call. */
export async function warnFeedback(): Promise<void> {
  if (isNative()) {
    try {
      await Haptics.notification({ type: NotificationType.Warning });
      return;
    } catch {
      /* fall through */
    }
  }
  vibrate([60, 40, 60]);
}

/** Escalating buzz while the kill switch is being held to arm. */
export async function armFeedback(): Promise<void> {
  if (isNative()) {
    try {
      await Haptics.impact({ style: ImpactStyle.Medium });
      return;
    } catch {
      /* fall through */
    }
  }
  vibrate(30);
}
