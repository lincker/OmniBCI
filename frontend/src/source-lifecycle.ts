/** Shared source preparation for setup inspection and repeated acquisition runs. */
import { api, type AcquisitionStatus } from "./api/client";
import { pluginConfigApi } from "./api/plugin-config";
import { runtimeConfigApi, type RuntimeStatus } from "./api/runtime-config";

export type SourceStage = "checking" | "starting_engine" | "connecting" | "validating" | "inspecting";
type SourceState = { runtime: RuntimeStatus; acquisition: AcquisitionStatus };

async function readState(revision: string, generation?: string, connectionId?: string): Promise<SourceState> {
  const [runtime, acquisition] = await Promise.all([runtimeConfigApi.status(), api.acquisitionStatus()]);
  if (!runtime || !acquisition) throw new Error("Source status is unavailable. Refresh before retrying.");
  if (runtime.recovery_required !== false || ["queued", "applying"].includes(runtime.operation?.status ?? "")) {
    throw new Error("Resolve the current runtime operation or recovery state before preparing the source.");
  }
  if (!revision || runtime.accepted_revision !== revision) {
    throw new Error("The startup configuration changed. Review the current selection before retrying.");
  }
  if (runtime.running && runtime.applied_revision !== revision) {
    throw new Error("The running configuration differs from the startup selection. Review and apply it in Setup.");
  }
  if ((runtime.running && !runtime.generation) || (generation && runtime.generation !== generation)) {
    throw new Error("The engine changed during preparation. Refresh its state before retrying.");
  }
  if (acquisition.connected && (!runtime.running || !acquisition.connection_id)) {
    throw new Error("The source connection was not confirmed. Refresh its state before retrying.");
  }
  if (connectionId && (!acquisition.connected || acquisition.connection_id !== connectionId)) {
    throw new Error("The source connection changed during preparation. Refresh its state before retrying.");
  }
  return { runtime, acquisition };
}

export async function prepareSource(revision: string, onStage?: (stage: SourceStage) => void): Promise<SourceState> {
  onStage?.("checking");
  const [selection, initial] = await Promise.all([pluginConfigApi.activation(), readState(revision)]);
  if (!selection || selection.revision !== revision) {
    throw new Error("The startup configuration changed. Review the current selection before retrying.");
  }
  if (!selection.bindings.some((binding) => binding.slot === "acquisition.source")) {
    throw new Error("Select and apply a source profile in Acquisition before starting.");
  }
  let state = initial;
  if (!state.runtime.running) {
    onStage?.("starting_engine");
    await api.engineStart();
    state = await readState(revision);
    if (!state.runtime.running) throw new Error("Engine startup was not confirmed. Refresh before retrying.");
  }
  const generation = state.runtime.generation!;
  if (!state.acquisition.connected) {
    onStage?.("connecting");
    await api.acqConnect();
    state = await readState(revision, generation);
    if (!state.acquisition.connected || !state.acquisition.connection_id) {
      throw new Error("The source connection was not confirmed. Refresh before retrying.");
    }
  }
  return state;
}

export async function startAcquisition(revision: string, onStage?: (stage: SourceStage) => void): Promise<SourceState> {
  const prepared = await prepareSource(revision, onStage);
  const generation = prepared.runtime.generation!;
  const connectionId = prepared.acquisition.connection_id!;
  let current = await readState(revision, generation, connectionId);
  if (current.acquisition.streaming) return current;
  onStage?.("validating");
  // Stream start prepares configured processing and enabled outputs on the backend.
  await api.acqStreamStart();
  current = await readState(revision, generation, connectionId);
  // The command acknowledges startup; a short recording may already have reached EOF by this refresh.
  return current;
}
