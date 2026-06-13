import { useCallback, useState } from "react";
import { ApiError } from "../api/client";

/**
 * Wrap a control action for a button: disables while pending (no double
 * fire), surfaces failures as a displayable string.  Backend business
 * branches (409/503) carry a stable ErrorCode — shown inline, never thrown.
 */
export function useAction<A extends unknown[]>(
  fn: (...args: A) => Promise<unknown>,
  onDone?: () => void,
) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (...args: A) => {
      setPending(true);
      setError(null);
      try {
        await fn(...args);
        onDone?.();
      } catch (e) {
        setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
      } finally {
        setPending(false);
      }
    },
    [fn, onDone],
  );

  return { run, pending, error };
}
