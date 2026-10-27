/** Shared source preparation for setup inspection and repeated acquisition runs. */
import { api, type AcquisitionStatus } from "./api/client";
import { pluginConfigApi } from "./api/plugin-config";
import { runtimeConfigApi, type RuntimeStatus } from "./api/runtime-config";

type SourceState = { runtime: RuntimeStatus; acquisition: AcquisitionStatus };

async function readState(revision: string, generation?: string, connectionId?: string): Promise<SourceState> {
  const [runtime, acquisition] = await Promise.all([runtimeConfigApi.status(), api.acquisitionStatus()]);
  if (!runtime || !acquisition) throw new Error("Source status is unavailable. Refresh before retrying.");
  if (runtime.recovery_required !== false || ["queued", "applying"].includes(runtime.operation?.status ?? "")) {
    throw new Error("Resolve the current runtime operation or recovery state before connecting the source.");
  }
  if (!revision || runtime.accepted_revision !== revision) {
    throw new Error("The startup configuration changed. Review the current selection before retrying.");
  }
  if (runtime.running && runtime.applied_revision !== revision) {
    throw new Error("The running configuration differs from the startup selection. Review and apply it in Setup.");
  }
  if ((runtime.running && !runtime.generation) || (generation && runtime.generation !== generation)) {
    throw new Error("The engine changed during the source operation. Refresh its state before retrying.");
  }
  if (acquisition.connected && (!runtime.running || !acquisition.connection_id)) {
    throw new Error("The source connection was not confirmed. Refresh its state before retrying.");
  }
  if (connectionId && (!acquisition.connected || acquisition.connection_id !== connectionId)) {
    throw new Error("The source connection changed during the source operation. Refresh its state before retrying.");
  }
  return { runtime, acquisition };
}

export async function connectSource(revision: string): Promise<SourceState> {
  const [selection, initial] = await Promise.all([pluginConfigApi.activation(), readState(revision)]);
  if (!selection || selection.revision !== revision) {
    throw new Error("The startup configuration changed. Review the current selection before retrying.");
  }
  if (!selection.bindings.some((binding) => binding.slot === "acquisition.source")) {
    throw new Error("Select and apply a source profile in Acquisition before starting.");
  }
  let state = initial;
  if (!state.runtime.running) {
    await api.engineStart();
    state = await readState(revision);
    if (!state.runtime.running) throw new Error("Engine startup was not confirmed. Refresh before retrying.");
  }
  const generation = state.runtime.generation!;
  if (!state.acquisition.connected) {
    await api.acqConnect();
    state = await readState(revision, generation);
    if (!state.acquisition.connected || !state.acquisition.connection_id) {
      throw new Error("The source connection was not confirmed. Refresh before retrying.");
    }
  }
  return state;
}

export async function startReading(revision: string): Promise<SourceState> {
  const initial = await readState(revision);
  if (!initial.runtime.running || !initial.acquisition.connected) {
    throw new Error("Connect the source before reading data.");
  }
  const generation = initial.runtime.generation!;
  const connectionId = initial.acquisition.connection_id!;
  let current = await readState(revision, generation, connectionId);
  if (current.acquisition.streaming) return current;
  // Stream start prepares configured processing and enabled outputs on the backend.
  await api.acqStreamStart();
  current = await readState(revision, generation, connectionId);
  // A short recording may already have reached EOF after the acknowledged start.
  return current;
}
