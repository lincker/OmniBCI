/** Own command continuations and feedback independently of the visible page. */
import { useEffect, useRef, useState } from "react";
import { api, ApiError, type EngineStatus } from "../api/client";
import { signalConfigApi } from "../api/signal-config";
import { useI18n, type TranslationKey } from "../i18n";
import { usePoll } from "./usePoll";
import type { AcquisitionController } from "./useAcquisition";
import { currentBindings, type ConfigurationModule, type CurrentConfigurationController } from "./useCurrentConfiguration";
import type { ConfigurationSection, RuntimeConfiguration } from "./useRuntimeConfiguration";
import type { ModuleActivation } from "../api/plugin-config";
import { runtimeBlockReason } from "../RuntimeConfiguration";
import { schemaErrors } from "../SchemaForm";
import { processingInputErrors } from "../ProcessingInput";

type ActionName = "start" | "stop" | "connectCurrent" | "launch" | "stopStim" | "trigger" | "readCurrent" | "stopRead" | "startProcessing" | "stopProcessing" | "startActuation" | "stopActuation";
type ActionState = { pending: boolean; error: string | null };
const actionModules: Record<ActionName, ConfigurationSection> = {
  start: "engine", stop: "engine", connectCurrent: "acquisition", launch: "stimulation", stopStim: "stimulation", trigger: "stimulation",
  readCurrent: "acquisition", stopRead: "acquisition", startProcessing: "processing", stopProcessing: "processing", startActuation: "actuation", stopActuation: "actuation",
};

export function recordingPathMissing(source: Pick<ModuleActivation["bindings"][number], "selection" | "parameters"> | undefined): boolean {
  return source?.selection.package_id === "OmniBCI.file_reader"
    && !(typeof source.parameters.data_file_path === "string" && source.parameters.data_file_path.trim());
}

export function useRuntimeControls(runtime: RuntimeConfiguration, current: CurrentConfigurationController,
  acquisition: AcquisitionController, engine: ReturnType<typeof usePoll<EngineStatus>>) {
  const { t } = useI18n();
  const stimulation = usePoll(api.stimulationStatus);
  const signal = usePoll(signalConfigApi.get);
  const [actions, setActions] = useState<Partial<Record<ActionName, ActionState>>>({});
  const values = useRef(actions);
  const [notices, setNotices] = useState<Partial<Record<ConfigurationModule, TranslationKey>>>({});
  useEffect(() => { setNotices({}); }, [runtime.status?.generation]);
  const update = (name: ActionName, value: ActionState) => {
    values.current = { ...values.current, [name]: value }; setActions(values.current);
  };
  const pending = (module: ConfigurationSection) => Object.entries(values.current).some(([name, state]) =>
    state.pending && (module === "engine" || actionModules[name as ActionName] === module))
    || ((module === "engine" || module === "acquisition") && acquisition.pending);
  const run = async (name: ActionName, action: () => Promise<unknown>, stop = false) => {
    const module = actionModules[name];
    if (values.current[name]?.pending || !stop && (pending(module) || values.current.start?.pending || values.current.stop?.pending)) return;
    update(name, { pending: true, error: null });
    try { await action(); }
    catch (cause) { update(name, { pending: true, error: cause instanceof ApiError ? `[${cause.code}] ${cause.message}` : String(cause) }); }
    finally {
      if (module === "engine") await Promise.all([engine.refresh(), runtime.refresh(), acquisition.refresh(), stimulation.refresh(), signal.refresh()]);
      else if (module === "acquisition") await acquisition.refresh();
      else if (module === "stimulation") await Promise.all([stimulation.refresh(), runtime.refresh()]);
      else await Promise.all([engine.refresh(), runtime.refresh(), ...(module === "processing" ? [signal.refresh()] : [])]);
      update(name, { pending: false, error: values.current[name]?.error ?? null });
    }
  };
  const startModule = async (module: ConfigurationModule) => {
    if (module === "acquisition") throw new Error("Use Connect to start acquisition.");
    const configuration = current.request(module);
    const result = await runtime.execute(module, configuration,
      module === "stimulation" ? api.stimStart : (body) => api.moduleStart(module, body),
      (operation) => current.acknowledge(module, configuration, operation.target_revisions[module]));
    if (result.status !== "succeeded") throw new Error(result.error ?? "Module startup failed.");
    if (result.result?.action === "prepared" && module !== "processing") {
      setNotices((previous) => ({ ...previous, [module]: module === "stimulation" ? "run.externalPrepared" : "run.externalActuationPrepared" }));
    }
  };
  const action = <A extends unknown[],>(name: ActionName, fn: (...args: A) => Promise<unknown>, stop = false) => ({
    ...(actions[name] ?? { pending: false, error: null }), run: (...args: A) => run(name, () => fn(...args), stop),
  });
  const controls = { stimulation, signal, notices, pending,
    busy: Object.values(actions).some((state) => state.pending) || acquisition.pending,
    enginePending: !!actions.start?.pending || !!actions.stop?.pending,
    start: action("start", async () => api.engineStart()),
    stop: action("stop", async () => { setNotices({}); await api.engineStop(); }, true),
    connectCurrent: action("connectCurrent", () => acquisition.connect.run(current.request("acquisition"))),
    launch: action("launch", async () => {
      setNotices((previous) => ({ ...previous, stimulation: undefined }));
      if (runtime.status?.recovery?.stimulation) return api.modulePrepare("stimulation");
      await startModule("stimulation");
    }),
    stopStim: action("stopStim", async () => {
      setNotices((previous) => ({ ...previous, stimulation: undefined })); await api.stimStop();
    }, true),
    trigger: action("trigger", api.trialStart),
    readCurrent: action("readCurrent", async () => {
      const accepted = current.modules.acquisition.activation;
      if (!accepted?.revision || current.hasChanges("acquisition", accepted)) throw new Error(t("run.reason.reconnectCurrentSource"));
      const generation = runtime.status?.generation;
      const connectionId = acquisition.data?.connection_id;
      if (!generation || !connectionId) throw new Error(t("run.reason.notConnected"));
      await acquisition.read.run({ expected_revision: accepted.revision, generation, connection_id: connectionId });
    }),
    stopRead: action("stopRead", async () => acquisition.stopRead.run(), true),
    startProcessing: action("startProcessing", () => startModule("processing")),
    stopProcessing: action("stopProcessing", async () => api.moduleStop("processing"), true),
    startActuation: action("startActuation", async () => {
      setNotices((previous) => ({ ...previous, actuation: undefined }));
      if (runtime.status?.recovery?.actuation) return api.modulePrepare("actuation");
      await startModule("actuation");
    }),
    stopActuation: action("stopActuation", async () => {
      setNotices((previous) => ({ ...previous, actuation: undefined })); await api.moduleStop("actuation");
    }, true),
  };
  const running = !!engine.data?.running;
  const engineAvailable = engine.data !== null && !engine.error;
  const activation = (module: ConfigurationModule) => current.modules[module].activation;
  const matches = (module: ConfigurationModule, applied = false) => current.modules[module].ready
    && activation(module)?.revision === runtime.status?.accepted[module]
    && (!applied || !running || (activation(module)?.revision === runtime.status?.applied?.[module]
      && runtime.status?.accepted.engine === runtime.status?.applied?.engine));
  const readyFor = (module: ConfigurationModule) => !current.modules[module].error && !runtime.error && matches(module);
  const currentReason = (module: ConfigurationModule) => current.modules[module].error
    ?? (current.modules[module].conflicted ? t("profiles.acceptedChanged") : !readyFor(module) ? t("run.reason.configurationLoading") : null);
  const operationReason = (module: ConfigurationSection, allowModuleRecovery = false) => {
    let reason = runtimeBlockReason(runtime, module);
    if (allowModuleRecovery && reason === "feedback.recovery" && !runtime.status?.recovery?.engine) {
      reason = runtime.error || runtime.status?.errors?.engine || runtime.status?.errors?.[module] ? "feedback.runtimeUnavailable" : null;
    }
    return controls.enginePending || pending(module) ? t("feedback.busy") : reason ? t(reason) : null;
  };
  const module = (name: ConfigurationModule) => {
    const accepted = activation(name);
    const bindings = currentBindings(name, current.entries, current.filterOrder, accepted);
    const has = (slot: string) => bindings.some((binding) => binding.slot === `${name}.${slot}`);
    const changed = current.hasChanges(name, accepted);
    const external = current.settings[name]?.[`${name.toUpperCase()}.auto_spawn`] === false;
    const acceptedExternal = matches(name) && accepted?.settings[`${name.toUpperCase()}.auto_spawn`] === false;
    const connected = name === "acquisition" ? !!acquisition.data?.connected
      : name === "stimulation" ? !!stimulation.data?.engine_connected : !!engine.data?.actuation_connected;
    const active = running && (name === "acquisition" ? connected : name === "processing" ? !!engine.data?.processor_alive
      : name === "stimulation" ? !!stimulation.data?.active && connected : !!engine.data?.actuation_alive && connected);
    const runtimeState = runtime.state(name);
    const available = engineAvailable && !runtime.error && !!runtime.status && !runtime.status.errors?.[name] && !runtime.status.errors?.engine && (name === "acquisition"
      ? acquisition.data !== null && !acquisition.error : name === "stimulation" ? stimulation.data !== null && !stimulation.error : true);
    const confirmed = available && engine.data?.running === runtime.status?.running && !runtimeState.uncertain && !runtimeState.recoveryRequired
      && !runtime.status?.recovery?.[name] && !runtime.status?.recovery?.engine;
    const recover = acceptedExternal && !!runtime.status?.recovery?.[name] && !connected;
    const engineReason = !engineAvailable ? t("run.reason.engineUnavailable") : null;
    const availabilityReason = engineReason ?? (name === "acquisition" && !available ? t("run.reason.acquisitionUnavailable")
      : name === "stimulation" && !available ? t("run.reason.stimulationUnavailable") : null);
    const validation = () => {
      const moduleError = current.validationError(name);
      if (moduleError) return moduleError;
      for (const binding of bindings) {
        const entry = current.entries[binding.slot];
        if (!entry) continue;
        if (entry.available === false && !external) return t("profiles.pluginUnavailable");
        if (entry.schema === null) return t("profiles.schemaRequired");
        const errors = entry.schema ? schemaErrors(entry.schema, entry.parameters, undefined, t) : [];
        if (binding.slot === "processing.decoder") errors.push(...processingInputErrors(entry.processingInput, t));
        if (errors.length) return errors[0];
      }
      return null;
    };
    const source = bindings.find((binding) => binding.slot === "acquisition.source");
    const sourceReason = currentReason(name) ?? (!has("source") ? t("run.reason.selectSource")
      : recordingPathMissing(source && "selection" in source ? source : undefined) ? t("run.missingRecording") : validation());
    let startReason = operationReason(name, recover) ?? availabilityReason;
    let stopReason: string | null = null;
    let startLabel: TranslationKey;
    let stopLabel: TranslationKey;
    let startAction;
    let stopAction;
    if (name === "acquisition") {
      startLabel = "run.connect"; stopLabel = "run.disconnect";
      startAction = { ...controls.connectCurrent, error: controls.connectCurrent.error ?? acquisition.connect.error };
      stopAction = acquisition.disconnect;
      startReason ??= connected ? t("run.reason.alreadyConnected") : sourceReason;
      stopReason = runtime.status?.recovery?.acquisition
        ? controls.enginePending || pending(name) ? t("feedback.busy") : availabilityReason
        : operationReason(name) ?? availabilityReason ?? (!connected ? t("run.reason.notConnected")
          : acquisition.data?.streaming ? t("run.reason.stopReading") : null);
    } else if (name === "processing") {
      startLabel = "run.startProcessing"; stopLabel = "run.stopProcessing";
      startAction = controls.startProcessing; stopAction = controls.stopProcessing;
      startReason ??= currentReason(name) ?? (active ? t("run.reason.processingActive") : !has("decoder") ? t("run.reason.selectDecoder")
        : !has("epoch_detector") ? t("run.reason.missingDetector") : validation());
      stopReason = stopAction.pending ? t("feedback.busy") : engineReason
        ?? (!engine.data?.processor_alive && !startAction.pending && !runtime.status?.recovery?.processing ? t("run.reason.processingStopped") : null);
    } else if (name === "stimulation") {
      startLabel = (recover ? acceptedExternal : external) ? connected ? "run.enableStimulation" : "run.prepareExternalStimulation" : "run.launchStimulation";
      stopLabel = "run.stopStimulation"; startAction = controls.launch; stopAction = controls.stopStim;
      startReason ??= !running ? t("run.reason.startEngine") : null;
      startReason ??= recover ? null : currentReason(name) ?? validation()
        ?? (changed && connected && (acceptedExternal || external) ? t("run.reason.disconnectExternal")
          : !has("paradigm") ? t("run.reason.selectParadigm")
          : stimulation.data?.active && (stimulation.data.process_alive || connected) ? t("run.reason.stimulationActive") : null);
      stopReason = stopAction.pending ? t("feedback.busy") : availabilityReason
        ?? (!(stimulation.data?.process_alive || stimulation.data?.active || connected || startAction.pending || notices.stimulation || runtime.status?.recovery?.stimulation)
          ? t("run.reason.stimulationStopped") : null);
    } else {
      startLabel = (recover ? acceptedExternal : external) && !connected ? "run.prepareExternalActuation" : "run.startActuation";
      stopLabel = "run.stopActuation"; startAction = controls.startActuation; stopAction = controls.stopActuation;
      startReason ??= !running ? t("run.reason.startEngine") : null;
      startReason ??= recover ? null : currentReason(name) ?? validation()
        ?? (changed && connected && (acceptedExternal || external) ? t("run.reason.disconnectExternal")
          : engine.data?.actuation_alive ? t("run.reason.actuationActive") : !has("effector") ? t("run.reason.selectEffector") : null);
      stopReason = stopAction.pending ? t("feedback.busy") : engineReason
        ?? (!engine.data?.actuation_alive && !connected && !startAction.pending && !notices.actuation && !runtime.status?.recovery?.actuation ? t("run.reason.actuationStopped") : null);
    }
    const status = !confirmed ? t("runtime.statusUnknown") : active ? t(name === "acquisition" ? "run.sourceConnected" : "run.running")
      : name === "acquisition" ? t("run.sourceNotConnected") : !running ? t("runtime.stopped")
      : name === "stimulation" && stimulation.data?.process_alive || name === "actuation" && engine.data?.actuation_alive ? t("runtime.waitingConnection")
      : (name === "stimulation" || name === "actuation") && connected ? t("runtime.connected") : t("runtime.stopped");
    const parameters = !confirmed || runtimeState.inProgress || pending(name) ? t("runtime.parametersUnconfirmed")
      : active ? !accepted?.revision || !readyFor(name) || !matches(name, true) ? t("runtime.parametersUnconfirmed")
        : changed ? t(name === "acquisition" ? "runtime.pendingSource" : "runtime.pendingParameters") : t("runtime.currentInUse")
      : t("runtime.nextStart");
    const readReason = name === "acquisition" ? operationReason(name) ?? availabilityReason
      ?? (!running ? t("run.reason.startEngine") : !connected ? t("run.reason.notConnected") : sourceReason)
      ?? (!matches(name, true) ? t("run.reason.selectionNotApplied") : acquisition.data?.streaming ? t("run.reason.alreadyReading")
        : changed ? t("run.reason.reconnectCurrentSource") : null) : null;
    const stopReadReason = controls.stopRead.pending || acquisition.stopRead.pending ? t("feedback.busy") : availabilityReason
      ?? (!acquisition.data?.streaming && !acquisition.read.pending && !controls.readCurrent.pending ? t("run.reason.notReading") : null);
    return { status, parameters, active, changed, available, external, connected,
      start: { ...startAction, label: t(startLabel), reason: startReason }, stop: { ...stopAction, label: t(stopLabel), reason: stopReason },
      read: { ...controls.readCurrent, error: controls.readCurrent.error ?? acquisition.read.error, label: t("run.startReading"), reason: readReason },
      stopRead: { ...controls.stopRead, error: controls.stopRead.error ?? acquisition.stopRead.error, label: t("run.stopReading"), reason: stopReadReason },
      notice: !changed && running && notices[name] ? t(notices[name]!) : null,
      reading: running && !!acquisition.data?.streaming, readingLabel: t(running && acquisition.data?.streaming ? "run.reading" : "run.notReading"),
      progress: runtimeState.uncertain ? t("feedback.confirming") : runtimeState.inProgress ? t("feedback.applying")
        : pending(name) || controls.enginePending ? t("feedback.busy") : null,
      heading: t("runtime.moduleStatus"), engineStart: { ...controls.start, label: t("run.startEngine"),
        reason: operationReason("engine", true) ?? engineReason ?? (running ? t("run.reason.alreadyRunning") : null) },
      needsEngine: engineAvailable && !running && (name === "stimulation" || name === "actuation"),
    };
  };
  return { ...controls, module, operationReason, currentReason, readyFor, matches };
}

export type RuntimeControls = ReturnType<typeof useRuntimeControls>;
