import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { connectSource, startReading } from "../source-lifecycle";
import { usePoll } from "./usePoll";
import type { RuntimeConfiguration } from "./useRuntimeConfiguration";

type Operation = "connect" | "disconnect" | "read" | "stopRead";

/** One connection state and command lock shared by Setup and Run. */
export function useAcquisition(runtime: RuntimeConfiguration, refreshEngine: () => Promise<void>) {
  const status = usePoll(api.acquisitionStatus);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [errors, setErrors] = useState<Partial<Record<Operation, string>>>({});
  const locked = useRef(false);
  const refresh = useCallback(async () => {
    await Promise.all([status.refresh(), refreshEngine(), runtime.refresh()]);
  }, [status.refresh, refreshEngine, runtime.refresh]);

  const run = async (name: Operation, action: () => Promise<unknown>) => {
    if (locked.current) return;
    locked.current = true;
    setOperation(name);
    setErrors((current) => ({ ...current, [name]: undefined }));
    try { await action(); }
    catch (cause) {
      setErrors((current) => ({ ...current, [name]: cause instanceof ApiError
        ? `[${cause.code}] ${cause.message}` : cause instanceof Error ? cause.message : String(cause) }));
    } finally {
      try { await refresh(); }
      finally { locked.current = false; setOperation(null); }
    }
  };
  return {
    ...status, refresh, pending: operation !== null,
    connect: { pending: operation === "connect", error: errors.connect ?? null,
      run: (revision: string) => run("connect", () => connectSource(revision)) },
    disconnect: { pending: operation === "disconnect", error: errors.disconnect ?? null,
      run: () => run("disconnect", async () => {
        const current = await api.acquisitionStatus();
        if (!current) throw new Error("Source status is unavailable. Refresh before retrying.");
        if (current.connected && current.connection_id !== status.data?.connection_id) {
          throw new Error("The source connection changed. Refresh before disconnecting it.");
        }
        if (current.streaming) throw new Error("Stop reading before disconnecting the source.");
        if (current.connected) await api.acqDisconnect();
      }) },
    read: { pending: operation === "read", error: errors.read ?? null,
      run: (revision: string) => run("read", () => startReading(revision)) },
    stopRead: { pending: operation === "stopRead", error: errors.stopRead ?? null,
      run: () => run("stopRead", api.acqStreamStop) },
  };
}

export type AcquisitionController = ReturnType<typeof useAcquisition>;
