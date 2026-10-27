import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { useI18n, type Translate } from "../i18n";
import { connectSource, startReading } from "../source-lifecycle";
import { usePoll } from "./usePoll";
import type { RuntimeConfiguration } from "./useRuntimeConfiguration";

type Operation = "connect" | "disconnect" | "read" | "stopRead";

/** Interpret only worker error types carried by the existing command response. */
function acquisitionError(cause: unknown, t: Translate): string | null {
  if (cause === undefined) return null;
  if (!(cause instanceof ApiError)) return cause instanceof Error ? cause.message : String(cause);
  const detail = cause.detail;
  const workerError = detail && typeof detail === "object" && !Array.isArray(detail)
    && "error_code" in detail ? detail.error_code : undefined;
  const message = cause.code === "COMMAND_EXECUTION_FAILED" && workerError === "ConnectionRefusedError"
    ? t("acquisition.error.connectionRefused")
    : cause.code === "COMMAND_EXECUTION_FAILED" && workerError === "TimeoutError"
      ? t("acquisition.error.deviceTimeout") : cause.message;
  return `[${cause.code}] ${message}`;
}

/** One connection state and command lock shared by Setup and Run. */
export function useAcquisition(runtime: RuntimeConfiguration, refreshEngine: () => Promise<void>) {
  const { t } = useI18n();
  const status = usePoll(api.acquisitionStatus);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [errors, setErrors] = useState<Partial<Record<Operation, unknown>>>({});
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
      setErrors((current) => ({ ...current, [name]: cause }));
    } finally {
      try { await refresh(); }
      finally { locked.current = false; setOperation(null); }
    }
  };
  return {
    ...status, refresh, pending: operation !== null,
    connect: { pending: operation === "connect", error: acquisitionError(errors.connect, t),
      run: (revision: string) => run("connect", () => connectSource(revision)) },
    disconnect: { pending: operation === "disconnect", error: acquisitionError(errors.disconnect, t),
      run: () => run("disconnect", async () => {
        const current = await api.acquisitionStatus();
        if (!current) throw new Error("Source status is unavailable. Refresh before retrying.");
        if (current.connected && current.connection_id !== status.data?.connection_id) {
          throw new Error("The source connection changed. Refresh before disconnecting it.");
        }
        if (current.streaming) throw new Error("Stop reading before disconnecting the source.");
        if (current.connected) await api.acqDisconnect();
      }) },
    read: { pending: operation === "read", error: acquisitionError(errors.read, t),
      run: (revision: string) => run("read", () => startReading(revision)) },
    stopRead: { pending: operation === "stopRead", error: acquisitionError(errors.stopRead, t),
      run: () => run("stopRead", api.acqStreamStop) },
  };
}

export type AcquisitionController = ReturnType<typeof useAcquisition>;
