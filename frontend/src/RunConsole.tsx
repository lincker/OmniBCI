import { useEffect, useState } from "react";
import { type EngineStatus } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { MAX_ACKS, type ResultsFeed } from "./hooks/useResultsFeed";
import { translateControlLabel, translatePluginName, useI18n, type TranslationKey, type TranslationParams } from "./i18n";
import { EpochProcessing } from "./EpochProcessing";
import { AcquisitionPreview } from "./AcquisitionPreview";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi, selectionKey, configurationModules, type ModuleActivation, type PluginProfile, type PluginDefinition } from "./api/plugin-config";
import { MAX_TRIALS, parseTrialSequence, readTrialControls, type TrialControl } from "./plugin-controls";
import { runtimeBlockReason } from "./RuntimeConfiguration";
import type { AcquisitionController } from "./hooks/useAcquisition";
import { currentBindings as collectCurrentBindings, type ConfigurationModule, type CurrentConfigurationController } from "./hooks/useCurrentConfiguration";
import type { RuntimeControls } from "./hooks/useRuntimeControls";
import "./RunConsole.css";

const startupModules = [
  { id: "acquisition", label: "run.acquisition", slots: [
    ["acquisition.source", "profiles.slotSource"],
  ] },
  { id: "processing", label: "run.signalProcessing", slots: [
    ["processing.epoch_detector", "profiles.slotEpochDetector"],
    ["processing.filters", "profiles.slotFilters"],
    ["processing.decoder", "run.decoder"],
  ] },
  { id: "stimulation", label: "run.stimulation", slots: [
    ["stimulation.paradigm", "run.paradigm"],
    ["stimulation.trigger", "profiles.slotTrigger"],
  ] },
  { id: "actuation", label: "run.actuator", slots: [
    ["actuation.effector", "profiles.slotEffector"],
  ] },
] as const;

function Dot({ on, label }: { on: boolean | undefined; label: string }) {
  const { t } = useI18n();
  return <span className={`dot ${on ? "dot-on" : "dot-off"}`}>{on === undefined ? "○" : "●"} {label}
    {on === undefined && ` · ${t("run.unavailable")}`}</span>;
}

function actionReasons(id: string, actions: [string, string | null][]) {
  const messages = new Map<string, string[]>();
  for (const [label, reason] of actions) {
    if (reason) messages.set(reason, [...(messages.get(reason) ?? []), label]);
  }
  return messages.size > 0 && <div id={id} className="action-reasons meta">
    {[...messages].map(([reason, labels]) => <p key={reason}><strong>{labels.join(" / ")}:</strong> {reason}</p>)}
  </div>;
}

function actionErrors(actions: [string, string | null][]) {
  return actions.filter(([, error]) => error).map(([label, error]) =>
    <p className="inline-message error" role="alert" key={label}><strong>{label}:</strong> {error}</p>);
}

export function recordingPathMissing(source: Pick<ModuleActivation["bindings"][number], "selection" | "parameters"> | undefined): boolean {
  return source?.selection.package_id === "OmniBCI.file_reader"
    && !(typeof source.parameters.data_file_path === "string" && source.parameters.data_file_path.trim());
}

export function RunConsole({ engine, acquisition, feed, runtime, current, runtimeControls, onEditSetup, onEditProcessing, onResults }: {
  engine: ReturnType<typeof usePoll<EngineStatus>>;
  acquisition: AcquisitionController;
  feed: ResultsFeed;
  runtimeControls: RuntimeControls;
  onEditSetup: () => void;
  onEditProcessing?: () => void;
  onResults: () => void;
  runtime: import("./hooks/useRuntimeConfiguration").RuntimeConfiguration;
  current: CurrentConfigurationController;
}) {
  const { t } = useI18n();
  const { stimulation, signal, start, stop, launch, stopStim, trigger, readCurrent,
    startProcessing, stopProcessing, startActuation, stopActuation, stopRead } = runtimeControls;
  const { connect, disconnect, read } = acquisition;
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [selectedParadigm, setSelectedParadigm] = useState<PluginDefinition | null>(null);
  const [controls, setControls] = useState<Record<string, TrialControl>>({});
  const [loadError, setLoadError] = useState<string | { key: TranslationKey; params?: TranslationParams } | null>(null);
  const [controlValues, setControlValues] = useState<Record<string, string>>({});
  const [trialError, setTrialError] = useState<"count" | "sequence" | { name: string; label: string } | null>(null);
  const [configurationReload, setConfigurationReload] = useState(0);
  const [controlsLoading, setControlsLoading] = useState(false);
  const stimulationNotice = runtimeControls.notices.stimulation;
  const actuationNotice = runtimeControls.notices.actuation;
  const operationReason = (module: ConfigurationModule | "engine", allowModuleRecovery = false) => {
    let reason = runtimeBlockReason(runtime, module);
    if (allowModuleRecovery && reason === "feedback.recovery" && !runtime.status?.recovery.engine) {
      reason = runtime.error || runtime.status?.errors?.engine || runtime.status?.errors?.[module] ? "feedback.runtimeUnavailable" : null;
    }
    return runtimeControls.enginePending || runtimeControls.pending(module) ? t("feedback.busy") : reason ? t(reason) : null;
  };
  const controlsBlocked = !!operationReason("stimulation");
  const running = engine.data?.running ?? false;
  const connected = acquisition.data?.connected ?? false;
  const reading = acquisition.data?.streaming ?? false;
  const processingRunning = engine.data?.processor_alive ?? false;
  const actuationRunning = engine.data?.actuation_alive ?? false;
  const engineAvailable = engine.data !== null && !engine.error;
  const acquisitionAvailable = engineAvailable && acquisition.data !== null && !acquisition.error;
  const stimulationAvailable = stimulation.data !== null && !stimulation.error;
  const activation = (module: ConfigurationModule) => current.modules[module].activation;
  const bindings = (module: ConfigurationModule) => activation(module)?.bindings ?? [];
  const matches = (module: ConfigurationModule, applied = false) => current.modules[module].ready
    && activation(module)?.revision === runtime.status?.accepted[module]
    && (!applied || !running || (activation(module)?.revision === runtime.status?.applied?.[module]
      && runtime.status?.accepted.engine === runtime.status?.applied?.engine));
  const readyFor = (module: ConfigurationModule) => !current.modules[module].error && !runtime.error && matches(module);
  const currentBindings = (module: ConfigurationModule) => collectCurrentBindings(module, current.entries, current.filterOrder, activation(module));
  const currentSource = currentBindings("acquisition").find((item) => item.slot === "acquisition.source");
  const currentDecoder = currentBindings("processing").some((item) => item.slot === "processing.decoder");
  const currentDetector = currentBindings("processing").some((item) => item.slot === "processing.epoch_detector");
  const currentParadigmBinding = currentBindings("stimulation").find((item) => item.slot === "stimulation.paradigm");
  const currentParadigmConfigured = !!currentParadigmBinding;
  const currentEffector = currentBindings("actuation").some((item) => item.slot === "actuation.effector");
  const pendingSource = current.hasChanges("acquisition", activation("acquisition"));
  const pendingStimulation = current.hasChanges("stimulation", activation("stimulation"));
  const pendingActuation = current.hasChanges("actuation", activation("actuation"));
  const decoderConfigured = readyFor("processing") ? bindings("processing").some((item) => item.slot === "processing.decoder") : null;
  const missingDetector = decoderConfigured && !bindings("processing").some((item) => item.slot === "processing.epoch_detector");
  const externalStimulation = matches("stimulation") && activation("stimulation")?.settings["STIMULATION.auto_spawn"] === false;
  const currentExternalStimulation = current.settings.stimulation?.["STIMULATION.auto_spawn"] === false;
  const currentParadigmUnavailable = !currentExternalStimulation && currentParadigmBinding && selectedParadigm
    && !selectedParadigm.available && selectionKey(currentParadigmBinding.selection) === selectionKey(selectedParadigm.selection);
  const stimulationStartLabel = t((runtime.status?.recovery?.stimulation ? externalStimulation : currentExternalStimulation)
    ? stimulation.data?.engine_connected ? "run.enableStimulation" : "run.prepareExternalStimulation" : "run.launchStimulation");
  const externalActuation = matches("actuation") && activation("actuation")?.settings["ACTUATION.auto_spawn"] === false;
  const currentExternalActuation = current.settings.actuation?.["ACTUATION.auto_spawn"] === false;
  const actuationStartLabel = t((runtime.status?.recovery?.actuation ? externalActuation : currentExternalActuation)
    && !engine.data?.actuation_connected ? "run.prepareExternalActuation" : "run.startActuation");
  const emptySelection = (module: ConfigurationModule) => t(current.modules[module].error ? "run.unavailable"
    : !readyFor(module) ? "run.loading" : "run.none");
  const sourceNotConfigured = readyFor("acquisition") && !currentSource;
  const selectedName = (slot: string) => {
    const module = slot.split(".")[0] as ConfigurationModule;
    const binding = bindings(module).find((item) => item.slot === slot);
    if (!binding) return emptySelection(module);
    return profiles.find((item) => item.profile_id === binding.profile_id)?.name
      ?? `${binding.selection.package_id}/${binding.selection.contribution_id} · ${binding.selection.version}`;
  };
  const selectedPlugin = (binding: ModuleActivation["bindings"][number]) => {
    const name = binding.slot === "acquisition.source" && !binding.profile_id
      ? t("profiles.currentSourceConfiguration")
      : profiles.find((item) => item.profile_id === binding.profile_id)?.name;
    const identity = `${binding.selection.package_id}/${binding.selection.contribution_id}`;
    return <><strong>{name ?? identity}</strong>
      {name && <small className="meta">{identity}</small>}
      <small className="meta">{t("run.pinnedVersion", { version: binding.selection.version })}
        {binding.profile_id && <> · {t("run.profileRevision", { revision: binding.profile_revision })}</>}</small></>;
  };
  const missingRecording = recordingPathMissing(currentSource && "selection" in currentSource ? currentSource : undefined);
  const playback = bindings("acquisition").some((item) => item.slot === "acquisition.source" && item.selection.package_id === "OmniBCI.file_reader");
  const currentParadigm = stimulation.data?.paradigm || selectedParadigm?.plugin_type || "";
  const paradigmName = translatePluginName(currentParadigm,
    selectedParadigm?.plugin_type === currentParadigm ? selectedParadigm.label : currentParadigm || t("run.none"), t);
  const paradigmBinding = bindings("stimulation").find((item) => item.slot === "stimulation.paradigm");
  const paradigmKey = paradigmBinding ? selectionKey(paradigmBinding.selection) : null;
  const trialControlsReady = readyFor("stimulation") && selectedParadigm && (externalStimulation || selectedParadigm.available)
    && selectionKey(selectedParadigm.selection) === paradigmKey && !loadError && !controlsLoading
    && matches("stimulation", true)
    && (!stimulation.data?.active || externalStimulation || stimulation.data.paradigm === selectedParadigm.plugin_type);
  const engineReason = !engineAvailable ? t("run.reason.engineUnavailable") : null;
  const acquisitionReason = engineReason ?? (!acquisitionAvailable ? t("run.reason.acquisitionUnavailable") : null);
  const stimulationReason = !stimulationAvailable ? t("run.reason.stimulationUnavailable") : null;
  const recordingReason = missingRecording ? t("run.missingRecording") : null;
  const currentReason = (module: ConfigurationModule) => current.modules[module].error
    ?? (!readyFor(module) ? t("run.reason.configurationLoading") : null);
  const trialSetupReason = currentReason("stimulation") ?? (trialControlsReady ? null : t(loadError ? "run.reason.trialControlsUnavailable"
    : !selectedParadigm || !externalStimulation && !selectedParadigm.available ? "run.reason.selectParadigm"
    : running && !matches("stimulation", true) ? "run.reason.selectionNotApplied" : "run.reason.paradigmMismatch"));
  const recovery = runtime.status?.recovery ?? {};
  const recoverStimulation = externalStimulation && !!recovery.stimulation && !stimulation.data?.engine_connected;
  const recoverActuation = externalActuation && !!recovery.actuation && !engine.data?.actuation_connected;
  const startReason = operationReason("engine", true) ?? engineReason ?? (running ? t("run.reason.alreadyRunning") : null);
  const stopReason = stop.pending ? t("feedback.busy") : engineReason
    ?? (!running && !runtimeControls.busy && !runtime.inProgress && !Object.keys(recovery).length ? t("run.reason.notRunning") : null);
  const sourceConfigurationReason = currentReason("acquisition") ?? (!currentSource ? t("run.reason.selectSource") : recordingReason);
  const connectReason = operationReason("acquisition") ?? acquisitionReason ?? (connected ? t("run.reason.alreadyConnected") : sourceConfigurationReason);
  const disconnectReason = recovery.acquisition
    ? (runtimeControls.enginePending || runtimeControls.pending("acquisition") ? t("feedback.busy") : acquisitionReason)
    : operationReason("acquisition") ?? acquisitionReason ?? (!connected ? t("run.reason.notConnected")
      : reading ? t("run.reason.stopReading") : null);
  const readReason = operationReason("acquisition") ?? acquisitionReason ?? (!running ? t("run.reason.startEngine")
    : !connected ? t("run.reason.notConnected") : sourceConfigurationReason)
    ?? (!matches("acquisition", true) ? t("run.reason.selectionNotApplied")
      : reading ? t("run.reason.alreadyReading") : pendingSource ? t("run.reason.reconnectCurrentSource") : null);
  const stopReadReason = stopRead.pending || acquisition.stopRead.pending ? t("feedback.busy") : acquisitionReason ?? (!reading && !read.pending && !readCurrent.pending ? t("run.reason.notReading") : null);
  const startProcessingReason = operationReason("processing") ?? engineReason ?? currentReason("processing")
    ?? (processingRunning ? t("run.reason.processingActive") : !currentDecoder ? t("run.reason.selectDecoder")
      : !currentDetector ? t("run.reason.missingDetector") : current.validationError("processing"));
  const stopProcessingReason = stopProcessing.pending ? t("feedback.busy") : engineReason ?? (!processingRunning && !startProcessing.pending && !recovery.processing ? t("run.reason.processingStopped") : null);
  const startActuationReason = operationReason("actuation", recoverActuation) ?? engineReason ?? (!running ? t("run.reason.startEngine") : null)
    ?? (recoverActuation ? null : currentReason("actuation") ?? current.validationError("actuation")
    ?? (pendingActuation && engine.data?.actuation_connected && (externalActuation || currentExternalActuation) ? t("run.reason.disconnectExternal")
      : actuationRunning ? t("run.reason.actuationActive") : !currentEffector ? t("run.reason.selectEffector") : null));
  const stopActuationReason = stopActuation.pending ? t("feedback.busy") : engineReason ?? (!actuationRunning && !engine.data?.actuation_connected && !startActuation.pending && !recovery.actuation ? t("run.reason.actuationStopped") : null);
  const launchReason = operationReason("stimulation", recoverStimulation) ?? engineReason ?? (!running ? t("run.reason.startEngine") : null)
    ?? stimulationReason ?? (recoverStimulation ? null : currentReason("stimulation") ?? current.validationError("stimulation")
    ?? (pendingStimulation && stimulation.data?.engine_connected && (externalStimulation || currentExternalStimulation) ? t("run.reason.disconnectExternal") : null)
    ?? (currentParadigmUnavailable ? t("profiles.pluginUnavailable") : null)
    ?? (pendingStimulation || currentExternalStimulation || !matches("stimulation", true) ? !currentParadigmConfigured ? t("run.reason.selectParadigm") : null : trialSetupReason)
    ?? (stimulation.data?.active && (stimulation.data.process_alive || stimulation.data.engine_connected)
      ? t("run.reason.stimulationActive") : null));
  const stopStimReason = stopStim.pending ? t("feedback.busy") : stimulationReason
    ?? (!(stimulation.data?.process_alive || stimulation.data?.active || stimulation.data?.engine_connected || launch.pending || recovery.stimulation)
      ? t("run.reason.stimulationStopped") : null);
  const triggerReason = operationReason("stimulation") ?? stimulationReason ?? trialSetupReason
    ?? (!stimulation.data?.active ? t(externalStimulation ? "run.reason.enableStimulation" : "run.reason.launchStimulation")
      : !stimulation.data.engine_connected ? t("run.reason.connectStimulation") : null);
  const hasTrialCount = Object.values(controls).some((control) => control.field === "n_trials");
  const sequenceControl = Object.entries(controls).find(([, control]) => control.default_sequence !== undefined);
  const countControl = Object.entries(controls).find(([, control]) => control.field === "n_trials");
  const repetitions = Number(controlValues[countControl?.[0] ?? "n_trials"] ?? countControl?.[1].default ?? 1);
  let targetSequence: number[] | undefined;
  if (sequenceControl) {
    const [name, control] = sequenceControl;
    try { targetSequence = parseTrialSequence(controlValues[name] ?? control.default_sequence!, control); }
    catch { /* Invalid text is reported when the form is submitted. */ }
  }
  const totalTrials = targetSequence && Number.isSafeInteger(repetitions) && repetitions > 0
    ? targetSequence.length * repetitions : undefined;
  useEffect(() => {
    let alive = true;
    void pluginConfigApi.profiles().then((value) => { if (alive) setProfiles(value); }).catch(() => {});
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    let alive = true;
    setLoadError(null);
    setControlsLoading(!!paradigmKey);
    if (!paradigmKey) { setSelectedParadigm(null); setControls({}); return; }
    void pluginConfigApi.catalog("paradigm").then((paradigms) => {
      if (!alive) return;
      const paradigm = paradigms.find((item) => selectionKey(item.selection) === paradigmKey);
      setControlsLoading(false);
      setSelectedParadigm(paradigm ?? null);
      try {
        const next = readTrialControls(paradigm?.controls);
        setControls((previous) => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
      } catch (error) { setLoadError({ key: "run.invalidTrialControls", params: { detail: String(error) } }); }
    }).catch((error) => { if (alive) { setControlsLoading(false); setLoadError({ key: "run.trialControlsUnavailable", params: { detail: String(error) } }); } });
    return () => { alive = false; };
  }, [paradigmKey, configurationReload]);
  const controlsKey = JSON.stringify([selectedParadigm?.selection, controls]);
  useEffect(() => {
    setControlValues(Object.fromEntries(Object.entries(controls)
      .map(([name, control]) => [name, control.default_sequence ?? (control.default == null ? "" : String(control.default))])));
    setTrialError(null);
  }, [controlsKey]);

  const triggerTrial = () => {
    if (controlsBlocked || !trialControlsReady || !stimulationAvailable
        || !stimulation.data?.active || !stimulation.data.engine_connected) return;
    if (sequenceControl && !targetSequence) { setTrialError("sequence"); return; }
    let nTrials = hasTrialCount ? 1 : Number(controlValues.n_trials ?? 1);
    let perTrial: number | undefined;
    for (const [name, control] of Object.entries(controls)) {
      if (control.default_sequence !== undefined) continue;
      const raw = controlValues[name];
      const value = raw?.trim() ? Number(raw) : undefined;
      if (value === undefined || !Number.isFinite(value)
          || (control.field === "trial_params" && !Number.isSafeInteger(value))
          || (control.min != null && value < Number(control.min))
          || (control.max != null && value > Number(control.max))) {
        setTrialError({ name, label: String(control.label ?? name) }); return;
      }
      if (control.field === "n_trials") nTrials = value;
      if (control.field === "trial_params") perTrial = value;
    }
    const total = nTrials * (targetSequence?.length ?? 1);
    if (!Number.isSafeInteger(nTrials) || nTrials < 1 || !Number.isSafeInteger(total) || total > MAX_TRIALS) {
      setTrialError("count"); return;
    }
    setTrialError(null);
    const params = targetSequence
      ? Array.from({ length: total }, (_, index) => targetSequence[index % targetSequence.length])
      : perTrial === undefined ? undefined : Array(nTrials).fill(perTrial);
    void trigger.run(total, params);
  };

  return <div className="runtime-content">
    <div className="breadcrumbs">{t("run.breadcrumb")} <Icon name="chevron" /> {t("run.console")}</div>
    <header className="editor-heading"><div><p className="eyebrow">{t("run.controlCenter")}</p><h1>{t("run.title")}</h1>
      <p>{t("run.description")}</p></div>
      <span className={`status-tag ${engineAvailable && running ? "success" : ""}`}><span className="status-dot" />
        {!engineAvailable ? t("run.engineUnavailable") : running ? t("run.engineRunning") : t("run.engineStopped")}</span></header>
    <section className="card engine-card" aria-label={t("run.engineControls")}>
      <div><h2>{t("run.engine")}</h2><div className="engine-status">
        <Dot on={engineAvailable ? running : undefined} label={t("run.running")} />
      </div><p className="meta">{t("run.engineHelp")}</p>{engine.error && <p className="error" role="alert">{engine.error.message}</p>}</div>
      <div className="control-feedback"><div className="actions"><button className="primary" disabled={!!startReason}
        aria-describedby={startReason ? "engine-action-reasons" : undefined}
        onClick={() => { if (!startReason) return start.run(); }}><Icon name="play" />{start.pending ? t("run.starting") : t("run.startEngine")}</button>
        <button className="danger" disabled={!!stopReason}
          aria-describedby={stopReason ? "engine-action-reasons" : undefined} onClick={() => void stop.run()}>{t("run.stopEngine")}</button></div>
        {actionReasons("engine-action-reasons", [[t("run.startEngine"), startReason], [t("run.stopEngine"), stopReason]])}
        {actionErrors([[t("run.startEngine"), start.error], [t("run.stopEngine"), stop.error]])}
      </div>
    </section>
    <section className="setup-summary" aria-label={t("run.selectedSetup")}>
      <div className="startup-heading"><h2 className="startup-title">{t("run.selectedSetup")}</h2>
        <button className="text-button" onClick={onEditSetup}>{t(sourceNotConfigured ? "run.configureSource" : "run.editSetup")} <Icon name="chevron" /></button>
      </div>
      {sourceNotConfigured && <div className="startup-empty" role="status">
        <strong>{t("run.noSourceConfigured")}</strong><p>{t("run.emptyConfigurationHelp")}</p>
      </div>}
      <div className="summary-items">
        {startupModules.map((module) => <section className="startup-module" key={module.id}
          aria-labelledby={`startup-${module.id}-title`}>
          <h3 id={`startup-${module.id}-title`}>{t(module.label)}</h3>
          {module.slots.map(([slot, label]) => {
            const filters = slot === "processing.filters";
            const selected = readyFor(module.id) ? bindings(module.id).filter((item) => filters ? item.kind === "filter" : item.slot === slot) : [];
            return <div key={slot}><span>{t(label)}</span>
              {!selected.length ? <strong>{emptySelection(module.id)}</strong>
                : filters ? <ol className="startup-filters" aria-label={t(label)}>
                  {selected.map((binding) => <li key={binding.slot}>{selectedPlugin(binding)}</li>)}
                </ol> : selectedPlugin(selected[0])}
            </div>;
          })}
        </section>)}
      </div>
      {configurationModules.some(readyFor) && <p className="meta">{t("run.selectedConfigurationHelp")}</p>}
      {startupModules.filter((module) => current.modules[module.id].ready && current.hasChanges(module.id, activation(module.id))).map((module) =>
        <p className="meta" role="status" key={module.id}>{t("run.currentChanges", { module: t(module.label) })}</p>)}
    </section>
    {missingRecording && <p className="inline-message" role="status">{t("run.missingRecording")}</p>}
    {loadError && <div className="inline-message error">
      <p role="alert">{typeof loadError === "string" ? loadError : t(loadError.key, loadError.params)}</p>
      <button onClick={() => setConfigurationReload((value) => value + 1)}>{t("run.retryConfiguration")}</button>
    </div>}
    {startupModules.filter((module) => current.modules[module.id].error).map((module) => <div className="inline-message error" key={module.id}>
      <p role="alert">{t(module.label)}: {current.modules[module.id].error}</p>
      <button onClick={() => void current.reload(module.id)}>{t("run.retryConfiguration")}</button></div>)}
    <div className="runtime-grid">
      <section className="card"><h2><Icon name="signal" />{t("run.acquisition")}</h2>
        <p><Dot on={acquisitionAvailable ? connected : undefined} label={t(!acquisitionAvailable ? "run.sourceConnection"
          : connected ? "run.sourceConnected" : "run.sourceNotConnected")} />
          <Dot on={acquisitionAvailable ? reading : undefined} label={t(!acquisitionAvailable ? "run.dataReading"
            : reading ? "run.reading" : "run.notReading")} /></p>
        {acquisition.error && <p className="error" role="alert">{acquisition.error.message}</p>}
        <p className="meta">{acquisitionAvailable ? <>{translatePluginName(acquisition.data?.source ?? "", acquisition.data?.source ?? t("run.noSource"), t)}<br />
          {t("run.acquisitionDetails", { sampleRate: acquisition.data?.sample_rate ?? "—", channelCount: acquisition.data?.channel_count ?? "—" })}</> : t("run.unavailable")}</p>
        <div className="actions">
          <button disabled={!!connectReason}
            aria-describedby={connectReason ? "connection-action-reasons" : "connection-help"}
            onClick={() => { if (!connectReason) {
              try {
                return connect.run(current.request("acquisition"));
              } catch (cause) { setLoadError(cause instanceof Error ? cause.message : String(cause)); }
            } }}>{t("run.connect")}</button>
          <button disabled={!!disconnectReason}
            aria-describedby={disconnectReason ? "connection-action-reasons" : "connection-help"}
            onClick={() => { if (!disconnectReason) return disconnect.run(); }}>{t("run.disconnect")}</button>
        </div>
        <p className="meta" id="connection-help">{t("run.connectHelp")}</p>
        {actionReasons("connection-action-reasons", [[t("run.connect"), connectReason], [t("run.disconnect"), disconnectReason]])}
        {actionErrors([[t("run.connect"), connect.error], [t("run.disconnect"), disconnect.error]])}
        <div className="actions">
          <button className="primary" disabled={!!readReason}
            aria-describedby={readReason ? "reading-action-reasons" : "acquisition-read-help"}
            onClick={() => { if (!readReason) return readCurrent.run(); }}><Icon name="play" />{t("run.startReading")}</button>
          <button disabled={!!stopReadReason}
            aria-describedby={stopReadReason ? "reading-action-reasons" : "acquisition-read-help"}
            onClick={() => { if (!stopReadReason) return stopRead.run(); }}>{t("run.stopReading")}</button>
        </div>
        <p className="meta" id="acquisition-read-help">{t("run.readHelp")}
          {playback && <> {t("run.playbackRestart")}</>}</p>
        {actionReasons("reading-action-reasons", [[t("run.startReading"), readReason], [t("run.stopReading"), stopReadReason]])}
        {actionErrors([[t("run.startReading"), readCurrent.error ?? read.error], [t("run.stopReading"), stopRead.error ?? acquisition.stopRead.error]])}
        <AcquisitionPreview generation={running ? runtime.status?.generation : null}
          revision={runtime.status?.applied?.acquisition} connectionId={acquisition.data?.connection_id ?? null}
          streaming={reading} disabled={!!operationReason("acquisition")} />
      </section>
      <section className="card" aria-label={t("run.activeDecoderInput")}>
        <h2><Icon name="workflow" />{t("run.signalProcessing")}</h2>
        <p><Dot on={engineAvailable ? processingRunning : undefined} label={t(processingRunning ? "run.processRunning" : "run.processStopped")} /></p>
        <div className="actions">
          <button disabled={!!startProcessingReason} aria-describedby={startProcessingReason ? "processing-action-reasons" : undefined}
            onClick={() => { if (!startProcessingReason) return startProcessing.run(); }}>{t("run.startProcessing")}</button>
          <button disabled={!!stopProcessingReason} aria-describedby={stopProcessingReason ? "processing-action-reasons" : undefined}
            onClick={() => { if (!stopProcessingReason) return stopProcessing.run(); }}>{t("run.stopProcessing")}</button>
        </div>
        <p className="meta">{t("run.processingHelp")}</p>
        {actionReasons("processing-action-reasons", [[t("run.startProcessing"), startProcessingReason], [t("run.stopProcessing"), stopProcessingReason]])}
        {actionErrors([[t("run.startProcessing"), startProcessing.error], [t("run.stopProcessing"), stopProcessing.error]])}
        {currentDecoder && !currentDetector && onEditProcessing && <button className="text-button" onClick={onEditProcessing}>{t("run.configureProcessing")}</button>}
      <div className="summary-items"><div><span>{t("run.signalPreparation")}</span>
        <strong>{current.modules.processing.error ? t("run.unavailable") : decoderConfigured === null ? t("run.loading")
          : !decoderConfigured ? t("run.processingNotConfigured") : !engineAvailable || signal.error ? t("run.unavailable")
          : missingDetector ? t("run.epochDetectorMissing") : !processingRunning ? t("run.processStopped") : !signal.data ? t("run.loading")
          : signal.data.readiness.ready ? t("run.ready") : t("run.validatedBeforeReading")}</strong></div>
        <div><span>{t("run.activeDecoderChannels")}</span><strong>{current.modules.processing.error ? t("run.unavailable")
          : decoderConfigured === null ? t("run.loading") : !decoderConfigured ? t("run.noActiveMapping")
          : !engineAvailable || signal.error ? t("run.unavailable") : !processingRunning ? t("run.none") : !signal.data ? t("run.loading") : signal.data.active_input
          ? signal.data.active_input.outputs.length ? signal.data.active_input.outputs.map((output) => output.name).join(", ") : t("run.allSourceChannels")
          : t("run.noActiveMapping")}</strong></div></div>
      {decoderConfigured && signal.error && <p className="error" role="alert">{signal.error.message}</p>}
      {decoderConfigured && !missingDetector && engineAvailable && !signal.error && signal.data && !signal.data.readiness.ready && processingRunning && <p className="meta">{t("run.prepareSignalHelp")}</p>}
    </section>
      <section className="card"><h2><Icon name="monitor" />{t("run.stimulation")}</h2>
        {externalStimulation && <p className="meta"><strong>{t("run.externalStimulation")}</strong><br />{t("run.externalStimulationHelp")}</p>}
        <p>{!externalStimulation && <Dot on={stimulationAvailable ? stimulation.data?.process_alive : undefined}
          label={t(stimulation.data?.process_alive ? "run.processRunning" : "run.processStopped")} />}
          <Dot on={stimulationAvailable ? stimulation.data?.engine_connected : undefined}
            label={t(stimulation.data?.engine_connected ? "run.engineConnected" : "run.engineNotConnected")} /></p>
        {stimulation.error && <p className="error" role="alert">{stimulation.error.message}</p>}
        <p className="meta">{paradigmName}<br />{t("profiles.slotTrigger")}: {selectedName("stimulation.trigger")}</p>
        <div className="actions"><button disabled={!!launchReason}
          aria-describedby={launchReason ? "stimulation-action-reasons" : undefined} onClick={() => void launch.run()}>{launch.pending
            ? t(externalStimulation ? "run.enablingStimulation" : "run.launching") : stimulationStartLabel}</button>
          <button disabled={!!stopStimReason}
            aria-describedby={stopStimReason ? "stimulation-action-reasons" : undefined} onClick={() => void stopStim.run()}>{t("run.stopStimulation")}</button></div>
        {actionReasons("stimulation-action-reasons", [[stimulationStartLabel, launchReason], [t("run.stopStimulation"), stopStimReason]])}
        {!pendingStimulation && stimulationNotice && <p className="inline-message success" role="status">{t(stimulationNotice)}</p>}
        {actionErrors([[stimulationStartLabel, launch.error], [t("run.stopStimulation"), stopStim.error]])}
        <form className="actions trial-controls" onSubmit={(event) => { event.preventDefault(); triggerTrial(); }}>
          {sequenceControl && <div className="trial-sequence">
            <label className="control"><span className="meta">{t("run.targetSequence")}</span>
              <input type="text" required disabled={controlsBlocked} maxLength={100000}
                aria-describedby="trial-sequence-help" aria-invalid={trialError === "sequence" || undefined}
                value={controlValues[sequenceControl[0]] ?? sequenceControl[1].default_sequence}
                onChange={(event) => { setControlValues({ ...controlValues, [sequenceControl[0]]: event.target.value }); setTrialError(null); }} /></label>
            <button type="button" disabled={controlsBlocked} onClick={() => {
              setControlValues({ ...controlValues, [sequenceControl[0]]: sequenceControl[1].default_sequence! }); setTrialError(null);
            }}>{t("run.resetSequence")}</button>
            <p className="meta" id="trial-sequence-help">{t("run.sequenceHelp")}
              {sequenceControl[1].min != null && sequenceControl[1].max != null
                && <> {t("run.sequenceRange", { min: sequenceControl[1].min, max: sequenceControl[1].max })}</>}</p>
          </div>}
          {!hasTrialCount && <label className="control"><span className="meta">{t(sequenceControl ? "run.sequenceRepetitions" : "run.numberOfTrials")}</span>
            <input aria-label={t(sequenceControl ? "run.sequenceRepetitions" : "run.numberOfTrials")} type="number" required min={1} max={MAX_TRIALS} step={1} disabled={controlsBlocked}
              value={controlValues.n_trials ?? "1"} onChange={(event) => setControlValues({ ...controlValues, n_trials: event.target.value })} /></label>}
          {Object.entries(controls).filter(([, control]) => control.default_sequence === undefined).map(([name, control]) => <label key={name} className="control">
            <span className="meta">{sequenceControl && control.field === "n_trials" ? t("run.sequenceRepetitions")
              : translateControlLabel(name, String(control.label ?? name), t)}</span>
            <input type="number" required disabled={controlsBlocked} value={controlValues[name] ?? ""}
              min={control.min == null ? (control.field === "n_trials" ? 1 : undefined) : Number(control.min)}
              max={control.max == null ? (control.field === "n_trials" ? 10000 : undefined) : Number(control.max)}
              step={control.field === "n_trials" || control.field === "trial_params" || control.type === "int" ? 1 : "any"}
              onChange={(event) => setControlValues({ ...controlValues, [name]: event.target.value })} /></label>)}
          <button type="submit" className="primary" disabled={controlsBlocked || !trialControlsReady || !stimulationAvailable || !stimulation.data?.active || !stimulation.data.engine_connected}
            aria-describedby={triggerReason ? "trial-action-reasons" : undefined}>{t("run.triggerTrial")}</button>
          {totalTrials !== undefined && <p className={`trial-sequence-summary meta${totalTrials > MAX_TRIALS ? " error" : ""}`} aria-live="polite">
            {t("run.sequenceSummary", { targets: targetSequence!.length, repetitions, total: totalTrials })}
          </p>}
        </form>
        {actionReasons("trial-action-reasons", [[t("run.triggerTrial"), triggerReason]])}
        {trialError && <p className="inline-message error" role="alert">{trialError === "count" ? t(sequenceControl ? "run.invalidSequenceCount" : "run.invalidTrialCount")
          : trialError === "sequence" ? t("run.invalidSequence")
          : t("run.invalidControl", { label: translateControlLabel(trialError.name, trialError.label, t) })}</p>}
        {actionErrors([[t("run.triggerTrial"), trigger.error]])}
      </section>
      <section className="card"><h2><Icon name="monitor" />{t("run.actuation")}</h2>
        {externalActuation && <p className="meta">{t("run.externalActuationHelp")}</p>}
        <p><Dot on={engineAvailable ? actuationRunning : undefined} label={t(actuationRunning ? "run.running" : "run.reason.actuationStopped")} />
          <Dot on={engineAvailable ? engine.data?.actuation_connected : undefined}
            label={t(engine.data?.actuation_connected ? "run.engineConnected" : "run.engineNotConnected")} /></p>
        <p className="meta">{selectedName("actuation.effector")}</p>
        <div className="actions">
          <button disabled={!!startActuationReason} aria-describedby={startActuationReason ? "actuation-action-reasons" : undefined}
            onClick={() => { if (!startActuationReason) return startActuation.run(); }}>{actuationStartLabel}</button>
          <button disabled={!!stopActuationReason} aria-describedby={stopActuationReason ? "actuation-action-reasons" : undefined}
            onClick={() => { if (!stopActuationReason) return stopActuation.run(); }}>{t("run.stopActuation")}</button>
        </div>
        <p className="meta">{t("run.actuationHelp")}</p>
        {actionReasons("actuation-action-reasons", [[actuationStartLabel, startActuationReason], [t("run.stopActuation"), stopActuationReason]])}
        {!current.hasChanges("actuation", activation("actuation")) && actuationNotice && <p className="inline-message success" role="status">{t(actuationNotice)}</p>}
        {actionErrors([[actuationStartLabel, startActuation.error], [t("run.stopActuation"), stopActuation.error]])}
      </section>
    </div>
    {feed.error && <p className="error" role="alert">{feed.error}</p>}
    <EpochProcessing onResults={onResults} disabled={!!operationReason("processing")} />
    <details className="ack-panel"><summary>{t("run.acknowledgements")} <span className="count">{feed.acks.length}</span></summary>
      <p className="meta">{t("run.acknowledgementsScope", { limit: MAX_ACKS })}</p>
      {!feed.acks.length ? <p className="meta">{t("run.acknowledgementsHelp")}</p> : <ul className="acks">
        {feed.acks.map((ack, index) => <li key={index}><code>{ack.command}</code>
          {ack.target_index != null && <> → {t("run.target", { index: ack.target_index })}</>} · {ack.status === "ok" || ack.status === "error" ? t(`run.ack.${ack.status}`) : ack.status}
          {ack.message && <span className="meta"> {ack.message}</span>}</li>)}</ul>}
    </details>
  </div>;
}
