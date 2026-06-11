import { useEffect, useRef, useState } from "react";

/**
 * Poll an async getter on a fixed interval (default 2 s — engine/acquisition
 * status is low-frequency by design; results come over the WebSocket).
 * Pauses while the tab is hidden.
 */
export function usePoll<T>(fn: () => Promise<T>, intervalMs = 2000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      if (!document.hidden) {
        try {
          const value = await fnRef.current();
          if (alive) {
            setData(value);
            setError(null);
          }
        } catch (e) {
          if (alive) setError(e as Error);
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

  return { data, error };
}
