import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage, type DataSourceMetadata } from "../api/client";
import { useI18n, type Translate } from "../i18n";
import { usePoll } from "./usePoll";
import type { RuntimeConfiguration } from "./useRuntimeConfiguration";
import type { ModuleConfigurationRequest, StartReadingRequest } from "../api/runtime-config";
import type { CurrentConfigurationController } from "./useCurrentConfiguration";

type Operation = "connect" | "disconnect" | "read" | "stopRead" | "inspect";

/** Interpret only worker error types carried by the existing command response. */
function acquisitionError(cause: unknown, t: Translate): string | null {
  if (cause === undefined) return null;
  if (!(cause instanceof ApiError)) return errorMessage(cause);
  const detail = cause.detail;
  const workerError = detail && typeof detail === "object" && !Array.isArray(detail)
    && "error_code" in detail ? detail.error_code : undefined;
  const message = cause.code === "COMMAND_EXECUTION_FAILED" && workerError === "ConnectionRefusedError"
    ? t("acquisition.error.connectionRefused")
    : cause.code === "COMMAND_EXECUTION_FAILED" && workerError === "TimeoutError"
      ? t("acquisition.error.deviceTimeout") : cause.message;
  return `[${cause.code}] ${message}`;
}

/** One connection state, source metadata cache, and command lock shared by Setup and Run. */
export function useAcquisition(runtime: RuntimeConfiguration, refreshEngine: () => Promise<void>, current: CurrentConfigurationController) {
  const { t } = useI18n();
  const status = usePoll(api.acquisitionStatus);
  const [operations, setOperations] = useState<Partial<Record<Operation, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<Operation, unknown>>>({});
  const [metadataState, setMetadataState] = useState<{ forConnection: string; value: DataSourceMetadata } | null>(null);
  const [inspectError, setInspectError] = useState<{ forConnection: string; cause: unknown } | null>(null);
  const pending = useRef<Partial<Record<Operation, boolean>>>({});
  const trackedConnection = useRef<string | null>(null);
  const hydratedConnection = useRef<string | null>(null);
  const metadataGeneration = useRef(0);

  const activeConnectionId = !status.error && status.data?.connected ? status.data.connection_id ?? null : null;
  if (trackedConnection.current !== activeConnectionId) {
    trackedConnection.current = activeConnectionId;
    metadataGeneration.current += 1;
    if (!activeConnectionId) hydratedConnection.current = null;
  }

  const refresh = useCallback(async () => {
    await Promise.all([status.refresh(), refreshEngine(), runtime.refresh()]);
  }, [status.refresh, refreshEngine, runtime.refresh]);

  useEffect(() => {
    if (!activeConnectionId || !status.data?.metadata_available) return;
    if (hydratedConnection.current === activeConnectionId) return;
    hydratedConnection.current = activeConnectionId;
    const generation = ++metadataGeneration.current;
    void api.acqMetadata().then((value) => {
      if (generation === metadataGeneration.current && value) {
        setMetadataState({ forConnection: activeConnectionId, value });
        setInspectError(null);
      }
    }).catch(() => {
      if (hydratedConnection.current === activeConnectionId) hydratedConnection.current = null;
    });
  }, [activeConnectionId, status.data?.metadata_available]);

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

  const inspect = async () => {
    if (pending.current.inspect || Object.values(pending.current).some(Boolean)) return;
    const connectionId = trackedConnection.current;
    if (!connectionId) return;
    const generation = ++metadataGeneration.current;
    hydratedConnection.current = connectionId;
    pending.current = { ...pending.current, inspect: true }; setOperations(pending.current);
    setMetadataState(null); setInspectError(null);
    try {
      const value = await api.acqMetadataRefresh();
      if (generation === metadataGeneration.current && value) {
        setMetadataState({ forConnection: connectionId, value });
      }
    } catch (cause) {
      if (generation === metadataGeneration.current) {
        setInspectError({ forConnection: connectionId, cause });
      }
    } finally {
      try { await refresh(); }
      finally { pending.current = { ...pending.current, inspect: false }; setOperations(pending.current); }
    }
  };

  const metadata = activeConnectionId && metadataState?.forConnection === activeConnectionId ? metadataState.value : null;
  const metadataFailure = activeConnectionId && inspectError?.forConnection === activeConnectionId
    ? t("profiles.inspectFailed", { error: acquisitionError(inspectError.cause, t) ?? errorMessage(inspectError.cause) })
    : null;

  return {
    ...status, refresh, metadata, pending: Object.values(operations).some(Boolean),
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
    inspect: { pending: !!operations.inspect, error: metadataFailure, run: inspect },
  };
}

export type AcquisitionController = ReturnType<typeof useAcquisition>;
