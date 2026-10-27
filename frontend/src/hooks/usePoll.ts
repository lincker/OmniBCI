import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Poll an async getter on a fixed interval (default 2 s — engine/acquisition
 * status is low-frequency by design; results come over the WebSocket).
 * Pauses while the tab is hidden.
 *
 * `refresh()` triggers one immediate fetch — call it right after a control
 * action so the UI reflects the new state without waiting for the next tick.
 */
export function usePoll<T>(fn: () => Promise<T>, intervalMs = 2000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const request = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const fetchOnce = useCallback(async (): Promise<void> => {
    const id = ++request.current;
    try {
      const value = await fnRef.current();
      if (id === request.current) { setData(value); setError(null); }
    } catch (e) {
      if (id === request.current) setError(e as Error);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      if (!document.hidden) {
        const id = ++request.current;
        try {
          const value = await fnRef.current();
          if (alive && id === request.current) {
            setData(value);
            setError(null);
          }
        } catch (e) {
          if (alive && id === request.current) setError(e as Error);
        }
      }
      if (alive) timer = setTimeout(tick, intervalMs);
    };

    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [intervalMs]);

  return { data, error, refresh: fetchOnce };
}
