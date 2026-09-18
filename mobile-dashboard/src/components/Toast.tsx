/**
 * Minimal toast stack — the only place the app narrates the result of a tap.
 *
 * Deliberately tiny: it renders above the thumb zone, auto-dismisses, and
 * distinguishes an *expired* setup (a normal terminal outcome of the 60s TTL)
 * from a real failure.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

export type ToastTone = 'success' | 'info' | 'warning' | 'danger';

export interface Toast {
  id: string;
  tone: ToastTone;
  title: string;
  body?: string;
  /** ms before auto-dismiss; 0 keeps it until tapped. */
  ttlMs: number;
}

export interface ToastInput {
  tone?: ToastTone;
  title: string;
  body?: string;
  ttlMs?: number;
}

interface ToastApi {
  push: (toast: ToastInput) => void;
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let counter = 0;
function nextId(): string {
  counter += 1;
  return `toast-${Date.now().toString(36)}-${counter}`;
}

export function ToastProvider({ children }: { children: ReactNode }): JSX.Element {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (input: ToastInput) => {
      const toast: Toast = {
        id: nextId(),
        tone: input.tone ?? 'info',
        title: input.title,
        ...(input.body === undefined ? {} : { body: input.body }),
        ttlMs: input.ttlMs ?? 4_200,
      };
      // Cap the stack so a reconnect storm cannot bury the screen.
      setToasts((current) => [...current.slice(-2), toast]);
      if (toast.ttlMs > 0) {
        timers.current.set(
          toast.id,
          setTimeout(() => dismiss(toast.id), toast.ttlMs),
        );
      }
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((timer) => clearTimeout(timer));
      pending.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(() => ({ push, dismiss }), [push, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext);
  if (context === null) {
    throw new Error('useToast must be used inside <ToastProvider>');
  }
  return context;
}

function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}): JSX.Element {
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          type="button"
          className={`toast toast--${toast.tone}`}
          onClick={() => onDismiss(toast.id)}
        >
          <span className="toast__title">{toast.title}</span>
          {toast.body !== undefined && <span className="toast__body">{toast.body}</span>}
        </button>
      ))}
    </div>
  );
}
