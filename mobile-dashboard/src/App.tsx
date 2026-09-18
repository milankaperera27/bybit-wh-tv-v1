/**
 * ATLAS — HITL 1-tap confirmation console.
 *
 * Layout is deliberately one-handed and one-screen:
 *
 *   ┌ StatusBar ───────────── connection / health / drawdown
 *   │ pending setup cards ─── the only thing that scrolls
 *   │ RegimeRadar ─────────── context for the decision
 *   └ KillSwitchButton ────── pinned in the thumb zone
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { KillSwitchButton } from './components/KillSwitchButton';
import { PendingSetupCard } from './components/PendingSetupCard';
import { RegimeRadar } from './components/RegimeRadar';
import { StatusBar } from './components/StatusBar';
import { ToastProvider, useToast } from './components/Toast';
import { useKillSwitch } from './hooks/useKillSwitch';
import { usePendingSetups } from './hooks/usePendingSetups';
import {
  ACTION_REJECT,
  consumeLaunchAction,
  initialisePush,
  venueForAction,
  type NotificationAction,
} from './push/fcm';

function Dashboard(): JSX.Element {
  const toast = useToast();
  const setupsState = usePendingSetups();
  const killSwitchState = useKillSwitch();

  const { confirm, reject, refresh, killSwitch: pushedKillSwitch } = setupsState;
  const { applyPushed } = killSwitchState;

  const [pushToken, setPushToken] = useState<string | null>(null);

  // Socket-pushed kill-switch updates feed the REST-backed hook.
  useEffect(() => {
    if (pushedKillSwitch !== null) applyPushed(pushedKillSwitch);
  }, [pushedKillSwitch, applyPushed]);

  /**
   * The single place a notification action becomes a request. Kept in a ref so
   * the push listeners registered once on mount always see fresh callbacks.
   */
  const handleNotificationAction = useCallback(
    async (signalId: string, action: NotificationAction): Promise<void> => {
      const venue = venueForAction(action);
      const outcome = action === ACTION_REJECT ? await reject(signalId) : await confirm(signalId, venue ?? 'TRADOVATE');

      if (outcome.kind === 'ok') {
        toast.push({
          tone: action === ACTION_REJECT ? 'info' : 'success',
          title: action === ACTION_REJECT ? 'Rejected from notification' : `Routed to ${venue}`,
          body: outcome.result.message ?? outcome.result.status,
        });
      } else if (outcome.kind === 'expired') {
        toast.push({
          tone: 'warning',
          title: 'Setup already expired',
          body: outcome.detail ?? 'The 60-second window closed first. No order was sent.',
        });
      } else {
        toast.push({ tone: 'danger', title: 'Action failed', body: outcome.message });
      }
    },
    [confirm, reject, toast],
  );

  const actionRef = useRef(handleNotificationAction);
  actionRef.current = handleNotificationAction;

  // ── Push wiring: once, on mount ───────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    const handlers = {
      onAction: (signalId: string, action: NotificationAction) => actionRef.current(signalId, action),
      onForegroundMessage: () => void refresh(),
      onStatus: (message: string) => {
        if (!cancelled) toast.push({ tone: 'warning', title: 'Push', body: message, ttlMs: 6_000 });
      },
    };

    // A cold start from a notification deep link acts immediately.
    consumeLaunchAction(handlers);

    void initialisePush(handlers).then((registration) => {
      if (!cancelled) setPushToken(registration.token);
    });

    return () => {
      cancelled = true;
    };
    // `refresh` and `toast` are stable; the action callback lives behind a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { setups, inFlight, loading, error, lastUpdated, socketState, regime, retire } = setupsState;

  return (
    <div className="app">
      <StatusBar
        socketState={socketState}
        pendingCount={setups.length}
        killSwitch={killSwitchState.status}
        lastUpdated={lastUpdated}
        error={error}
        onRefresh={() => void refresh()}
      />

      <main className="app__main">
        <section className="queue" aria-label="Pending setups">
          {loading && setups.length === 0 && <p className="queue__empty">Loading pending setups…</p>}

          {!loading && setups.length === 0 && (
            <div className="queue__idle">
              <span className="queue__idle-mark" aria-hidden="true">
                ◎
              </span>
              <p className="queue__idle-title">No setup awaiting approval</p>
              <p className="queue__idle-sub">
                A validated TradingView alert appears here — and on your lock screen — for 60 seconds.
              </p>
            </div>
          )}

          {setups.map((setup) => (
            <PendingSetupCard
              key={setup.signal_id}
              setup={setup}
              pending={inFlight.has(setup.signal_id)}
              onConfirm={confirm}
              onReject={reject}
              onRetire={retire}
            />
          ))}
        </section>

        <RegimeRadar snapshot={regime} disconnected={socketState !== 'open'} />

        {pushToken === null && (
          <p className="app__note">
            Push is not registered on this device — you will only see setups while this screen is open.
          </p>
        )}
      </main>

      <footer className="app__footer">
        <KillSwitchButton
          status={killSwitchState.status}
          busy={killSwitchState.busy}
          onEngage={killSwitchState.engage}
          onRelease={killSwitchState.release}
        />
      </footer>
    </div>
  );
}

export function App(): JSX.Element {
  return (
    <ToastProvider>
      <Dashboard />
    </ToastProvider>
  );
}

export default App;
