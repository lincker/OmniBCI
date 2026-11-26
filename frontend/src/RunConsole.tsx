import { useEffect, useState, type ReactNode } from "react";
import { type EngineStatus } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { MAX_ACKS, type ResultsFeed } from "./hooks/useResultsFeed";
import { translateControlLabel, translatePluginName, useI18n, type TranslationKey, type TranslationParams } from "./i18n";
import { EpochProcessing } from "./EpochProcessing";
import { AcquisitionPreview } from "./AcquisitionPreview";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi, selectionId, selectionKey, configurationModules, type ConfigurationModule, type PluginDefinition } from "./api/plugin-config";
import { MAX_TRIALS, parseTrialSequence, readTrialControls, type TrialControl } from "./plugin-controls";
import { ModuleRuntime } from "./ModuleRuntime";
import { LiveResults } from "./LiveResults";
import type { AcquisitionController } from "./hooks/useAcquisition";
import { currentBindings as collectCurrentBindings, type CurrentConfigurationController } from "./hooks/useCurrentConfiguration";
import { isIndependentOutput } from "./hooks/useRuntimeConfiguration";
import { recordingPathMissing, type RuntimeControls } from "./hooks/useRuntimeControls";
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

type Step = ConfigurationModule;
const pipelineSteps = [
  { id: "acquisition", label: "run.acquisition", slot: "acquisition.source" },
  { id: "processing", label: "run.signalProcessing", slot: "processing.decoder" },
  { id: "stimulation", label: "run.stimulation", slot: "stimulation.paradigm" },
  { id: "actuation", label: "run.actuation", slot: "actuation.effector", optional: true },
] as const;
type StepPhase = "done" | "working" | "failed" | "partial" | "waiting" | "unconfigured";

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
  const { stimulation, actuation, signal, start, stop, trigger, operationReason, currentReason, readyFor, matches } = runtimeControls;
  const [selectedParadigm, setSelectedParadigm] = useState<PluginDefinition | null>(null);
  const [controls, setControls] = useState<Record<string, TrialControl>>({});
  const [loadError, setLoadError] = useState<string | { key: TranslationKey; params?: TranslationParams } | null>(null);
  const [controlValues, setControlValues] = useState<Record<string, string>>({});
  const [trialError, setTrialError] = useState<"count" | "sequence" | { name: string; label: string } | null>(null);
  const [configurationReload, setConfigurationReload] = useState(0);
  const [controlsLoading, setControlsLoading] = useState(false);
  // null follows the next pipeline step automatically; a click pins the user's choice.
  const [pinnedStep, setPinnedStep] = useState<Step | null>(null);
  const controlsBlocked = !!operationReason("stimulation");
  const running = engine.data?.running ?? false;
  const reading = acquisition.data?.streaming ?? false;
  const processingRunning = engine.data?.processor_alive ?? false;
  const engineAvailable = engine.data !== null && !engine.error;
  const acquisitionAvailable = engineAvailable && acquisition.data !== null && !acquisition.error;
  const stimulationAvailable = stimulation.data !== null && !stimulation.error;
  const activation = (module: ConfigurationModule) => current.modules[module].activation;
  const bindings = (module: ConfigurationModule) => activation(module)?.bindings ?? [];
  const currentBindings = (module: ConfigurationModule) => collectCurrentBindings(module, current.entries, current.filterOrder, activation(module));
  const currentSource = currentBindings("acquisition").find((item) => item.slot === "acquisition.source");
  const currentDecoder = currentBindings("processing").some((item) => item.slot === "processing.decoder");
  const currentDetector = currentBindings("processing").some((item) => item.slot === "processing.epoch_detector");
  const decoderConfigured = readyFor("processing") ? bindings("processing").some((item) => item.slot === "processing.decoder") : null;
  const missingDetector = decoderConfigured && !bindings("processing").some((item) => item.slot === "processing.epoch_detector");
  const processingDetailsUnavailable = !engineAvailable || !!signal.error || processingRunning && !matches("processing", true);
  const externalStimulation = matches("stimulation") && activation("stimulation")?.settings["STIMULATION.auto_spawn"] === false;
  const emptySelection = (module: ConfigurationModule) => t(current.modules[module].error ? "run.unavailable"
    : !readyFor(module) ? "run.loading" : "run.none");
  const sourceNotConfigured = readyFor("acquisition") && !currentSource;
  const selectedName = (slot: string) => {
    const module = slot.split(".")[0] as ConfigurationModule;
    const binding = bindings(module).find((item) => item.slot === slot);
    return binding ? `${binding.selection.package_id}/${binding.selection.contribution_id} · ${binding.selection.version}` : emptySelection(module);
  };
  const selectedPlugin = (binding: ReturnType<typeof currentBindings>[number]) =>
    <><strong>{binding.selection.package_id}/{binding.selection.contribution_id}</strong>
      <small className="meta">{t("run.pinnedVersion", { version: binding.selection.version })}</small></>;
  const missingRecording = recordingPathMissing(currentSource);
  const playback = acquisition.data?.metadata_provenance === "file-reported"
    || bindings("acquisition").some((item) => item.slot === "acquisition.source" && "data_file_path" in item.parameters);
  const selectedParadigmId = selectedParadigm ? selectionId(selectedParadigm.selection) : "";
  const currentParadigm = stimulation.data?.paradigm || selectedParadigmId;
  const paradigmName = translatePluginName(currentParadigm,
    selectedParadigm && selectedParadigmId === currentParadigm ? selectedParadigm.label : currentParadigm || t("run.none"), t);
  const paradigmBinding = bindings("stimulation").find((item) => item.slot === "stimulation.paradigm");
  const paradigmKey = paradigmBinding ? selectionKey(paradigmBinding.selection) : null;
  const trialControlsReady = readyFor("stimulation") && selectedParadigm && (externalStimulation || selectedParadigm.available)
    && selectionKey(selectedParadigm.selection) === paradigmKey && !loadError && !controlsLoading
    && matches("stimulation", true)
    && (!stimulation.data?.active || externalStimulation || stimulation.data.paradigm === selectedParadigmId);
  const engineReason = !engineAvailable ? t("run.reason.engineUnavailable") : null;
  const stimulationReason = !stimulationAvailable ? t("run.reason.stimulationUnavailable") : null;
  const paradigmUnavailable = !selectedParadigm || !externalStimulation && !selectedParadigm.available;
  // Before stimulation is launched there is nothing to mismatch; the launch reason below is the actionable one.
  const awaitingLaunch = !stimulation.data?.active && !loadError && !controlsLoading && !paradigmUnavailable;
  const trialSetupReason = currentReason("stimulation") ?? (trialControlsReady || awaitingLaunch ? null : t(loadError ? "run.reason.trialControlsUnavailable"
    : paradigmUnavailable ? "run.reason.selectParadigm"
    : stimulation.data?.active && !matches("stimulation", true) ? "run.reason.selectionNotApplied" : "run.reason.paradigmMismatch"));
  const recovery = runtime.status?.recovery ?? {};
  const startReason = operationReason("engine", true) ?? engineReason ?? (running ? t("run.reason.alreadyRunning") : null);
  const stopReason = stop.pending ? t("feedback.busy") : engineReason
    ?? (!running && !runtimeControls.busy && !runtime.inProgress && !Object.keys(recovery).some((module) => !isIndependentOutput(module)) ? t("run.reason.notRunning") : null);
  const triggerReason = operationReason("stimulation") ?? stimulationReason ?? trialSetupReason
    ?? (!stimulation.data?.active ? t(externalStimulation ? "run.reason.enableStimulation" : "run.reason.launchStimulation")
      : !stimulation.data.connected ? t("run.reason.connectStimulation") : null);
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
    setLoadError(null);
    setControlsLoading(!!paradigmKey);
    if (!paradigmKey) { setSelectedParadigm(null); setControls({}); return; }
    void pluginConfigApi.catalog("paradigm").then((paradigms) => {
      if (!alive) return;
      const paradigm = paradigms.find((item) => selectionKey(item.selection) === paradigmKey);
      setControlsLoading(false);
      setSelectedParadigm(paradigm ?? null);
      const entry = current.get("stimulation.paradigm");
      if (entry?.selection && selectionKey(entry.selection) === paradigmKey) {
        current.set("stimulation.paradigm", { ...entry, available: paradigm?.available ?? false, schema: paradigm?.schema });
      }
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

  const trialBatch = () => {
    if (sequenceControl && !targetSequence) { setTrialError("sequence"); return; }
    let nTrials = hasTrialCount ? 1 : Number(controlValues.n_trials ?? 1);
    let perTrial: number | undefined;
    const payloadKey = Object.values(controls).find((control) => control.field === "trial_params")?.payload_key;
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
    const params = Array.from({ length: total }, (_, index) => {
      const value = targetSequence ? targetSequence[index % targetSequence.length] : perTrial;
      return payloadKey === undefined || value === undefined ? {} : { [payloadKey]: value };
    });
    return { n_trials: total, trial_params: params };
  };
  const triggerTrial = () => {
    if (controlsBlocked || !trialControlsReady || !stimulationAvailable
        || !stimulation.data?.active || !stimulation.data.connected) return;
    const batch = trialBatch();
    if (batch) void trigger.run(batch.n_trials, batch.trial_params);
  };

  const steps = pipelineSteps.map((step) => {
    const state = runtimeControls.module(step.id);
    const configured = !readyFor(step.id) || currentBindings(step.id).some((item) => item.slot === step.slot);
    const done = step.id === "acquisition" ? state.reading : state.active;
    const acquisitionActions = step.id === "acquisition" ? [state.read, state.stopRead] : [];
    const working = !!state.progress || [state.start, state.stop, ...acquisitionActions].some((action) => action.pending);
    const failed = [state.start, state.stop, ...acquisitionActions].some((action) => action.error);
    const phase: StepPhase = failed ? "failed" : working ? "working" : !configured ? "unconfigured" : done ? "done"
      : state.active || state.connected ? "partial" : "waiting";
    const status = phase === "unconfigured" ? t("pipeline.notConfigured")
      : step.id === "acquisition" && state.active ? state.readingLabel : state.status;
    return { ...step, optional: "optional" in step, configured, done, phase, status };
  });
  const engineNext = engineAvailable && !running;
  const nextStep = engineNext ? null : steps.find((step) => !step.optional && step.configured && !step.done)?.id ?? null;
  const allDone = engineAvailable && running && !nextStep;
  const selectedStep: Step = pinnedStep ?? nextStep ?? "acquisition";
  const followingStep = (id: Step) => {
    const index = steps.findIndex((step) => step.id === id);
    return steps.slice(index + 1).find((step) => step.configured && !step.done) ?? null;
  };
  const moveStep = (from: number, delta: number) => {
    const target = steps[(from + delta + steps.length) % steps.length].id;
    setPinnedStep(target);
    document.getElementById(`run-step-${target}`)?.focus();
  };
  const pluginInUse = (id: Step) => {
    const step = pipelineSteps.find((item) => item.id === id)!;
    const binding = readyFor(id) ? currentBindings(id).find((item) => item.slot === step.slot) : undefined;
    return binding
      ? <p className="step-plugin meta">{t("pipeline.inUse", { name: `${binding.selection.package_id}/${binding.selection.contribution_id}` })}</p>
      : null;
  };
  const stepPanel = (id: Step, card: ReactNode) => {
    const following = followingStep(id);
    const step = steps.find((item) => item.id === id)!;
    return <div role="tabpanel" id={`run-panel-${id}`} aria-labelledby={`run-step-${id}`} hidden={selectedStep !== id} className="run-step-panel">
      {card}
      {step.done && following && <button type="button" className="text-button step-next" onClick={() => setPinnedStep(following.id)}>
        {t("pipeline.nextTo", { step: t(following.label) })} <Icon name="chevron" /></button>}
    </div>;
  };
  const stepHelp = (...lines: ReactNode[]) => <details className="step-help">
    <summary>{t("pipeline.details")}</summary>{lines.map((line, index) => <p className="meta" key={index}>{line}</p>)}
  </details>;

  return <div className="runtime-content">
    <div className="breadcrumbs">{t("run.breadcrumb")} <Icon name="chevron" /> {t("run.console")}</div>
    <header className="editor-heading"><div><p className="eyebrow">{t("run.controlCenter")}</p><h1>{t("run.title")}</h1>
      <p>{t("run.description")}</p></div>
      <div className="run-heading-actions">
        <span className={`status-tag ${engineAvailable && running ? "success" : ""}`}><span className="status-dot" />
          {!engineAvailable ? t("run.engineUnavailable") : running ? t("run.engineRunning") : t("run.engineStopped")}</span>
        <button className="text-button" onClick={onEditSetup}>{t(sourceNotConfigured ? "run.configureSource" : "run.editSetup")} <Icon name="chevron" /></button>
      </div></header>
    <section className={`card engine-card${engineAvailable && running ? " engine-running" : ""}${engineNext ? " engine-next" : ""}`} aria-label={t("run.engineControls")}>
      <div><h2>{t("run.engine")}{engineNext && <span className="step-badge">{t("pipeline.next")}</span>}</h2><div className="engine-status">
        <Dot on={engineAvailable ? running : undefined} label={engineAvailable && !running ? t("run.engineStopped") : t("run.running")} />
      </div><p className="meta">{t("run.engineHelp")}</p>{engine.error && <p className="error" role="alert">{engine.error.message}</p>}</div>
      <div className="control-feedback"><div className="actions"><button className={running ? undefined : "primary"} disabled={!!startReason}
        aria-describedby={startReason ? "engine-action-reasons" : undefined}
        onClick={() => { if (!startReason) return start.run(); }}><Icon name="play" />{start.pending ? t("run.starting") : t("run.startEngine")}</button>
        <button className="danger" disabled={!!stopReason}
          aria-describedby={stopReason ? "engine-action-reasons" : undefined} onClick={() => void stop.run()}>{t("run.stopEngine")}</button></div>
        {actionReasons("engine-action-reasons", [[t("run.startEngine"), startReason], [t("run.stopEngine"), stopReason]])}
        {actionErrors([[t("run.startEngine"), start.error], [t("run.stopEngine"), stop.error]])}
      </div>
    </section>
    <nav className="run-pipeline" role="tablist" aria-label={t("pipeline.label")}>
      {steps.map((step, index) => <button type="button" role="tab" key={step.id} id={`run-step-${step.id}`}
        className="run-step" data-phase={step.phase} data-next={step.id === nextStep || undefined}
        aria-selected={selectedStep === step.id} aria-controls={`run-panel-${step.id}`} tabIndex={selectedStep === step.id ? 0 : -1}
        onClick={() => setPinnedStep(step.id)}
        onKeyDown={(event) => {
          if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); moveStep(index, 1); }
          if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); moveStep(index, -1); }
        }}>
        <span className="run-step-index" aria-hidden="true">{step.phase === "done" ? <Icon name="check" /> : index + 1}</span>
        <span className="run-step-text"><strong>{t(step.label)}</strong>
          <small>{step.status}{step.optional && <> · {t("pipeline.optional")}</>}</small></span>
        {step.id === nextStep && <span className="step-badge">{t("pipeline.next")}</span>}
      </button>)}
    </nav>
    {allDone && <p className="inline-message success" role="status">{t("pipeline.allDone")}</p>}
    {missingRecording && <p className="inline-message" role="status">{t("run.missingRecording")}</p>}
    {loadError && <div className="inline-message error">
      <p role="alert">{typeof loadError === "string" ? loadError : t(loadError.key, loadError.params)}</p>
      <button onClick={() => setConfigurationReload((value) => value + 1)}>{t("run.retryConfiguration")}</button>
    </div>}
    {startupModules.filter((module) => current.modules[module.id].error).map((module) => <div className="inline-message error" key={module.id}>
      <p role="alert">{t(module.label)}: {current.modules[module.id].error}</p>
      <button onClick={() => void current.reload(module.id)}>{t("run.retryConfiguration")}</button></div>)}
    <div className="run-layout">
    <div className="run-steps">
      {stepPanel("acquisition", <section className="card"><h2><Icon name="signal" />{t("run.acquisition")}</h2>
        {pluginInUse("acquisition")}
        {sourceNotConfigured && <div className="startup-empty" role="status">
          <strong>{t("run.noSourceConfigured")}</strong><p>{t("run.emptyConfigurationHelp")}</p>
        </div>}
        {acquisition.error && <p className="error" role="alert">{acquisition.error.message}</p>}
        <p className="meta">{acquisitionAvailable ? <>{translatePluginName(acquisition.data?.source ?? "", acquisition.data?.source ?? t("run.noSource"), t)} · {" "}
          {t("run.acquisitionDetails", { sampleRate: acquisition.data?.sample_rate ?? "—", channelCount: acquisition.data?.channel_count ?? "—" })}</> : t("run.unavailable")}</p>
        <ModuleRuntime module="acquisition" controls={runtimeControls} />
        {stepHelp(t("run.connectHelp"), <>{t("run.readHelp")}{playback && <> {t("run.playbackRestart")}</>}</>)}
        <AcquisitionPreview generation={running ? runtime.status?.generation : null}
          revision={runtime.status?.applied?.acquisition} connectionId={acquisition.data?.connection_id ?? null}
          streaming={reading} disabled={!!operationReason("acquisition")} />
      </section>)}
      {stepPanel("processing", <section className="card" aria-label={t("run.activeDecoderInput")}>
        <h2><Icon name="workflow" />{t("run.signalProcessing")}</h2>
        {pluginInUse("processing")}
        <ModuleRuntime module="processing" controls={runtimeControls} />
        {currentDecoder && !currentDetector && onEditProcessing && <button className="text-button" onClick={onEditProcessing}>{t("run.configureProcessing")}</button>}
      <div className="summary-items"><div><span>{t("run.signalPreparation")}</span>
        <strong>{current.modules.processing.error ? t("run.unavailable") : decoderConfigured === null ? t("run.loading")
          : processingDetailsUnavailable ? t("run.unavailable") : !decoderConfigured ? t("run.processingNotConfigured")
          : missingDetector ? t("run.epochDetectorMissing") : !processingRunning ? t("run.processStopped") : !signal.data ? t("run.loading")
          : signal.data.readiness.ready ? t("run.ready") : t("run.validatedBeforeReading")}</strong></div>
        <div><span>{t("run.activeDecoderChannels")}</span><strong>{current.modules.processing.error ? t("run.unavailable")
          : decoderConfigured === null ? t("run.loading") : processingDetailsUnavailable ? t("run.unavailable") : !decoderConfigured ? t("run.noActiveMapping")
          : !processingRunning ? t("run.none") : !signal.data ? t("run.loading") : signal.data.active_input
          ? signal.data.active_input.outputs.length ? signal.data.active_input.outputs.map((output) => output.name).join(", ") : t("run.allSourceChannels")
          : t("run.noActiveMapping")}</strong></div></div>
      {decoderConfigured && signal.error && <p className="error" role="alert">{signal.error.message}</p>}
      {decoderConfigured && !missingDetector && !processingDetailsUnavailable && signal.data && !signal.data.readiness.ready && processingRunning && <p className="meta">{t("run.prepareSignalHelp")}</p>}
      {stepHelp(t("run.processingHelp"))}
    </section>)}
      {stepPanel("stimulation", <section className="card"><h2><Icon name="monitor" />{t("run.stimulation")}</h2>
        {pluginInUse("stimulation")}
        {externalStimulation && <p className="meta"><strong>{t("run.externalStimulation")}</strong><br />{t("run.externalStimulationHelp")}</p>}
        {stimulation.error && <p className="error" role="alert">{stimulation.error.message}</p>}
        {stimulation.data?.active && stimulation.data.connected && matches("stimulation", true)
          && <p className="meta">{paradigmName}<br />{t("profiles.slotTrigger")}: {selectedName("stimulation.trigger")}</p>}
        <ModuleRuntime module="stimulation" controls={runtimeControls} />
        {stimulationAvailable && stimulation.data && <p className="meta">
          {t("bridge.local")} · {t(stimulation.data.connected ? "bridge.connected" : "bridge.disconnected")}<br />
          {t("bridge.localFeedback")} · {t(stimulation.data.engine_linked ? "bridge.connected" : "bridge.disconnected")}
        </p>}
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
          <button type="submit" className="primary" disabled={controlsBlocked || !trialControlsReady || !stimulationAvailable || !stimulation.data?.active || !stimulation.data.connected}
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
      </section>)}
      {stepPanel("actuation", <section className="card"><h2><Icon name="monitor" />{t("run.actuation")}</h2>
        {pluginInUse("actuation")}
        {actuation.data?.active && actuation.data.connected && matches("actuation", true)
          && <p className="meta">{selectedName("actuation.effector")}</p>}
        <ModuleRuntime module="actuation" controls={runtimeControls} />
        {actuation.data && !actuation.error && <p className="meta">
          {t("run.controllerLink")} · {t(actuation.data.connected ? "bridge.connected" : "bridge.disconnected")}<br />
          {t("run.decodedInput")} · {t(actuation.data.engine_linked ? "bridge.connected" : "bridge.disconnected")}
        </p>}
        {stepHelp(t("run.actuationHelp"))}
      </section>)}
    </div>
    <LiveResults feed={feed} onResults={onResults} />
    </div>
    <details className="run-more">
      <summary>{t("run.selectedSetup")}</summary>
      <section className="setup-summary" aria-label={t("run.selectedSetup")}>
      <h2 className="startup-title visually-hidden">{t("run.selectedSetup")}</h2>
      <div className="summary-items">
        {startupModules.map((module) => <section className="startup-module" key={module.id}
          aria-labelledby={`startup-${module.id}-title`}>
          <h3 id={`startup-${module.id}-title`}>{t(module.label)}</h3>
          {module.slots.map(([slot, label]) => {
            const filters = slot === "processing.filters";
            const selected = readyFor(module.id) ? currentBindings(module.id).filter((item) => filters ? item.slot.startsWith("processing.filter.") : item.slot === slot) : [];
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
      </section>
    </details>
    <EpochProcessing onResults={onResults} disabled={!!operationReason("processing")} />
    <details className="ack-panel"><summary>{t("run.acknowledgements")} <span className="count">{feed.acks.length}</span></summary>
      <p className="meta">{t("run.acknowledgementsScope", { limit: MAX_ACKS })}</p>
      {!feed.acks.length ? <p className="meta">{t("run.acknowledgementsHelp")}</p> : <ul className="acks">
        {feed.acks.map((ack, index) => <li key={index}><code>{ack.command}</code>
          {ack.trial_params && Object.entries(ack.trial_params).map(([key, value]) => {
            const label = Object.values(controls).find((control) => control.payload_key === key)?.label ?? key;
            return <span key={key}> → {label}: {typeof value === "string" ? value : JSON.stringify(value)}</span>;
          })} · {ack.status === "ok" || ack.status === "error" ? t(`run.ack.${ack.status}`) : ack.status}
          {ack.message && <span className="meta"> {ack.message}</span>}</li>)}</ul>}
    </details>
  </div>;
}
