/** Shared source preparation for setup inspection and repeated acquisition runs. */
import { api, type AcquisitionStatus } from "./api/client";
import { pluginConfigApi, type RevisionMap } from "./api/plugin-config";
import { runtimeConfigApi, type RuntimeStatus } from "./api/runtime-config";
import { operationActive, operationAffects, type ConfigurationSection } from "./hooks/useRuntimeConfiguration";

type SourceState = { runtime: RuntimeStatus; acquisition: AcquisitionStatus };

function checkRuntimeRevision(runtime: RuntimeStatus, revisions: RevisionMap, generation?: string, requireApplied = true) {
  const required = Object.keys(revisions).filter((module) => module !== "system") as ConfigurationSection[];
  if (Object.keys(runtime.recovery).some((module) => module === "system" || required.includes(module as ConfigurationSection))
      || Object.values(runtime.operations).some((operation) => operation && operationActive(operation)
        && required.some((module) => operationAffects(operation, module)))) {
    throw new Error("Resolve the current runtime operation or recovery state before continuing.");
  }
  if (!revisions.system || Object.entries(revisions).some(([module, revision]) => !revision || runtime.accepted[module] !== revision)) {
    throw new Error("The required module configuration changed. Review the current selection before retrying.");
  }
  if (runtime.running && Object.entries(revisions).some(([module, revision]) => (requireApplied || module === "system") && runtime.applied?.[module] !== revision)) {
    throw new Error("The running module differs from the selected values. Stop and restart that module before continuing.");
  }
  if ((runtime.running && !runtime.generation) || (generation && runtime.generation !== generation)) {
    throw new Error("The engine changed during this operation. Refresh its state before retrying.");
  }
}

/** Check only the modules consumed by the following command, plus shared settings. */
export async function assertConfigurationRevision(revisions: RevisionMap, generation?: string, requireApplied = true): Promise<RuntimeStatus> {
  const runtime = await runtimeConfigApi.status();
  if (!runtime) throw new Error("Runtime status is unavailable. Refresh before retrying.");
  checkRuntimeRevision(runtime, revisions, generation, requireApplied);
  return runtime;
}

async function readState(revisions: RevisionMap, generation?: string, connectionId?: string, requireApplied = true): Promise<SourceState> {
  const [runtime, acquisition] = await Promise.all([runtimeConfigApi.status(), api.acquisitionStatus()]);
  if (!runtime || !acquisition) throw new Error("Source status is unavailable. Refresh before retrying.");
  checkRuntimeRevision(runtime, revisions, generation, requireApplied || acquisition.connected);
  if (acquisition.connected && (!runtime.running || !acquisition.connection_id)) {
    throw new Error("The source connection was not confirmed. Refresh its state before retrying.");
  }
  if (connectionId && (!acquisition.connected || acquisition.connection_id !== connectionId)) {
    throw new Error("The source connection changed during the source operation. Refresh its state before retrying.");
  }
  return { runtime, acquisition };
}

export async function connectSource(revisions: RevisionMap, valid = () => true): Promise<SourceState | undefined> {
  const expected = { acquisition: revisions.acquisition, system: revisions.system };
  const [selection, initial] = await Promise.all([pluginConfigApi.module("acquisition"), readState(expected, undefined, undefined, false)]);
  if (selection.revision !== expected.acquisition) {
    throw new Error("The acquisition configuration changed. Review the current selection before retrying.");
  }
  if (!selection.bindings.some((binding) => binding.slot === "acquisition.source")) {
    throw new Error("Configure a source in Acquisition before starting.");
  }
  if (!valid()) return;
  let state = initial;
  if (!state.runtime.running) {
    await api.engineStart();
    if (!valid()) return;
    state = await readState(expected, undefined, undefined, false);
    if (!state.runtime.running) throw new Error("Engine startup was not confirmed. Refresh before retrying.");
  }
  if (!valid()) return;
  const generation = state.runtime.generation!;
  if (!state.acquisition.connected) {
    await api.acqConnect();
    state = await readState(expected, generation);
    if (!state.acquisition.connected || !state.acquisition.connection_id) {
      throw new Error("The source connection was not confirmed. Refresh before retrying.");
    }
  }
  return state;
}

export async function startReading(revisions: RevisionMap, valid = () => true): Promise<SourceState | undefined> {
  const initial = await readState(revisions);
  if (!initial.runtime.running || !initial.acquisition.connected) {
    throw new Error("Connect the source before reading data.");
  }
  const generation = initial.runtime.generation!;
  const connectionId = initial.acquisition.connection_id!;
  let current = await readState(revisions, generation, connectionId);
  if (!valid()) return;
  if (current.acquisition.streaming) return current;
  // Stream start prepares processing only when that module is already running.
  await api.acqStreamStart();
  current = await readState(revisions, generation, connectionId);
  // A short recording may already have reached EOF after the acknowledged start.
  return current;
}
