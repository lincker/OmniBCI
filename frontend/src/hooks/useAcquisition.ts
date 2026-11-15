import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { useI18n, type Translate } from "../i18n";
import { usePoll } from "./usePoll";
import type { RuntimeConfiguration } from "./useRuntimeConfiguration";
import type { ModuleConfigurationRequest, StartReadingRequest } from "../api/runtime-config";
import type { CurrentConfigurationController } from "./useCurrentConfiguration";

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
export function useAcquisition(runtime: RuntimeConfiguration, refreshEngine: () => Promise<void>, current: CurrentConfigurationController) {
  const { t } = useI18n();
  const status = usePoll(api.acquisitionStatus);
  const [operations, setOperations] = useState<Partial<Record<Operation, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<Operation, unknown>>>({});
  const pending = useRef<Partial<Record<Operation, boolean>>>({});
  const refresh = useCallback(async () => {
    await Promise.all([status.refresh(), refreshEngine(), runtime.refresh()]);
  }, [status.refresh, refreshEngine, runtime.refresh]);

  const run = async (name: Operation, action: () => Promise<unknown>) => {
    if (pending.current[name] || name !== "stopRead" && Object.values(pending.current).some(Boolean)) return;
    pending.current = { ...pending.current, [name]: true }; setOperations(pending.current);
    setErrors((current) => ({ ...current, [name]: undefined }));
    try { await action(); }
    catch (cause) {
      setErrors((current) => ({ ...current, [name]: cause }));
    } finally {
      try { await refresh(); }
      finally { pending.current = { ...pending.current, [name]: false }; setOperations(pending.current); }
    }
  };
  return {
    ...status, refresh, pending: Object.values(operations).some(Boolean),
    connect: { pending: !!operations.connect, error: acquisitionError(errors.connect, t),
      run: (configuration: ModuleConfigurationRequest) => run("connect", async () => {
        const result = await runtime.execute("acquisition", configuration, api.acqConnect,
          (operation) => current.acknowledge("acquisition", configuration, operation.target_revisions.acquisition));
        if (result.status !== "succeeded") throw new Error(result.error ?? "Acquisition connection failed.");
      }) },
    disconnect: { pending: !!operations.disconnect, error: acquisitionError(errors.disconnect, t),
      run: () => run("disconnect", async () => {
        const current = await api.acquisitionStatus();
        if (!current) throw new Error("Source status is unavailable. Refresh before retrying.");
        if (current.connected && current.connection_id !== status.data?.connection_id) {
          throw new Error("The source connection changed. Refresh before disconnecting it.");
        }
        const recovery = runtime.status?.recovery.acquisition;
        if (current.streaming && !recovery) throw new Error("Stop reading before disconnecting the source.");
        if (current.connected || recovery) await api.acqDisconnect();
      }) },
    read: { pending: !!operations.read, error: acquisitionError(errors.read, t),
      run: (identity: StartReadingRequest) => run("read", () => api.acqStreamStart(identity)) },
    stopRead: { pending: !!operations.stopRead, error: acquisitionError(errors.stopRead, t),
      run: () => run("stopRead", api.acqStreamStop) },
  };
}

export type AcquisitionController = ReturnType<typeof useAcquisition>;
