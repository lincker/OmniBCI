import { useEffect, useRef, useState } from "react";
import { api, type AcquisitionStatus, type EngineStatus } from "./api/client";
import { useAction } from "./hooks/useAction";
import { usePoll } from "./hooks/usePoll";
import { MAX_ACKS, type ResultsFeed } from "./hooks/useResultsFeed";
import { translateControlLabel, translatePluginName, useI18n, type TranslationKey, type TranslationParams } from "./i18n";
import { EpochProcessing } from "./EpochProcessing";
import { AcquisitionPreview } from "./AcquisitionPreview";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi, selectionKey, type Activation, type PluginProfile, type PluginDefinition } from "./api/plugin-config";
import { MAX_TRIALS, parseTrialSequence, readTrialControls, type TrialControl } from "./plugin-controls";
import { signalConfigApi } from "./api/signal-config";
import { runtimeBlockReason } from "./RuntimeConfiguration";
import { startAcquisition, type SourceStage } from "./source-lifecycle";
import "./RunConsole.css";

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

export function recordingPathMissing(selection: Activation | null): boolean {
  const source = selection?.bindings.find((item) => item.slot === "acquisition.source");
  return source?.selection.package_id === "OmniBCI.file_reader"
    && !(typeof source.parameters.data_file_path === "string" && source.parameters.data_file_path.trim());
}

export function RunConsole({ engine, acquisition, feed, runtime, onBusyChange, onEditSetup, onEditProcessing, onResults }: {
  engine: ReturnType<typeof usePoll<EngineStatus>>;
  acquisition: ReturnType<typeof usePoll<AcquisitionStatus>>;
  feed: ResultsFeed;
  onBusyChange: (busy: boolean) => void;
  onEditSetup: () => void;
  onEditProcessing?: () => void;
  onResults: () => void;
  runtime: import("./hooks/useRuntimeConfiguration").RuntimeConfiguration;
}) {
  const { t } = useI18n();
  const stimulation = usePoll(api.stimulationStatus);
  const signal = usePoll(signalConfigApi.get);
  const [selection, setSelection] = useState<Activation | null>(null);
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [selectedParadigm, setSelectedParadigm] = useState<PluginDefinition | null>(null);
  const [controls, setControls] = useState<Record<string, TrialControl>>({});
  const [loadError, setLoadError] = useState<string | { key: TranslationKey; params?: TranslationParams } | null>(null);
  const [controlValues, setControlValues] = useState<Record<string, string>>({});
  const [trialError, setTrialError] = useState<"count" | "sequence" | { name: string; label: string } | null>(null);
  const [quickPending, setQuickPending] = useState(false);
  const [quickStage, setQuickStage] = useState<SourceStage | null>(null);
  const [quickError, setQuickError] = useState<string | null>(null);
  const [quickOutcome, setQuickOutcome] = useState<"inactive" | null>(null);
  const quickLock = useRef(false);
  const refresh = async () => { await Promise.all([engine.refresh(), acquisition.refresh(), stimulation.refresh(), signal.refresh()]); };
  const refreshAfter = <A extends unknown[],>(action: (...args: A) => Promise<unknown>) => async (...args: A) => {
    try { return await action(...args); }
    finally { await refresh(); }
  };
  const start = useAction(refreshAfter(api.engineStart));
  const stop = useAction(refreshAfter(api.engineStop));
  const disconnect = useAction(refreshAfter(api.acqDisconnect));
  const stopRead = useAction(refreshAfter(api.acqStreamStop));
  const launch = useAction(refreshAfter(api.stimStart));
  const stopStim = useAction(refreshAfter(api.stimStop));
  const trigger = useAction(api.trialStart);
  const actions = [start, stop, disconnect, stopRead, launch, stopStim, trigger];
  const busy = quickPending || actions.some((action) => action.pending);
  const controlsBlocked = busy || runtime.blocked;
  const stopBlocked = busy || runtime.inProgress || !runtime.status;
  const running = engine.data?.running ?? false;
  const connected = acquisition.data?.connected ?? false;
  const reading = acquisition.data?.streaming ?? false;
  const engineAvailable = engine.data !== null && !engine.error;
  const acquisitionAvailable = engineAvailable && acquisition.data !== null && !acquisition.error;
  const stimulationAvailable = stimulation.data !== null && !stimulation.error;
  const configurationError = loadError && (typeof loadError === "string"
    || !["run.invalidTrialControls", "run.trialControlsUnavailable"].includes(loadError.key));
  const decoderConfigured = selection && !configurationError && selection.revision === runtime.status?.accepted_revision
    ? selection.bindings.some((item) => item.slot === "processing.decoder") : null;
  const missingDetector = decoderConfigured && !selection?.bindings.some((item) => item.slot === "processing.epoch_detector");
  const externalStimulation = selection?.revision === runtime.status?.accepted_revision
    && selection?.settings?.["STIMULATION.auto_spawn"] === false;
  const acquisitionStartLabel = t(acquisitionAvailable && connected ? "run.startReading" : "run.startAcquisition");
  const stimulationStartLabel = t(externalStimulation ? "run.enableStimulation" : "run.launchStimulation");
  const selectedName = (slot: string) => {
    if (configurationError) return t("run.unavailable");
    if (!selection || selection.revision !== runtime.status?.accepted_revision) return t("run.loading");
    const binding = selection?.bindings.find((item) => item.slot === slot);
    if (!binding) return t("run.none");
    return profiles.find((item) => item.profile_id === binding.profile_id)?.name
      ?? `${binding.selection.package_id}/${binding.selection.contribution_id} · ${binding.selection.version}`;
  };
  const selectedVersion = (slot: string) => {
    if (!selection || selection.revision !== runtime.status?.accepted_revision) return null;
    const binding = selection.bindings.find((item) => item.slot === slot);
    return binding && <small className="meta">{t("run.pinnedVersion", { version: binding.selection.version ?? t("profiles.unversioned") })}
      {binding.profile_id && <> · {t("run.profileRevision", { revision: binding.profile_revision })}</>}</small>;
  };
  const missingRecording = recordingPathMissing(selection);
  const playback = selection?.bindings.some((item) => item.slot === "acquisition.source" && item.selection.package_id === "OmniBCI.file_reader");
  const currentParadigm = stimulation.data?.paradigm || selectedParadigm?.plugin_type || "";
  const paradigmName = translatePluginName(currentParadigm,
    selectedParadigm?.plugin_type === currentParadigm ? selectedParadigm.label : currentParadigm || t("run.none"), t);
  const trialControlsReady = selectedParadigm?.available && !loadError
    && selection?.revision === runtime.status?.accepted_revision
    && (!running || runtime.status?.applied_revision === selection?.revision)
    && (!stimulation.data?.active || stimulation.data.paradigm === selectedParadigm.plugin_type);
  const runtimeReason = runtimeBlockReason(runtime);
  const operationReason = busy ? t("feedback.busy") : runtimeReason ? t(runtimeReason) : null;
  const stopOperationReason = stopBlocked ? t(busy ? "feedback.busy"
    : runtime.inProgress ? runtime.uncertain ? "feedback.confirming" : "feedback.applying"
    : "feedback.runtimeUnavailable") : null;
  const engineReason = !engineAvailable ? t("run.reason.engineUnavailable") : null;
  const acquisitionReason = engineReason ?? (!acquisitionAvailable ? t("run.reason.acquisitionUnavailable") : null);
  const stimulationReason = !stimulationAvailable ? t("run.reason.stimulationUnavailable") : null;
  const recordingReason = missingRecording ? t("run.missingRecording") : null;
  const trialSetupReason = trialControlsReady ? null : t(loadError ? "run.reason.trialControlsUnavailable"
    : !selectedParadigm?.available ? "run.reason.selectParadigm"
    : selection?.revision !== runtime.status?.accepted_revision ? "run.reason.selectionChanged"
    : running && runtime.status?.applied_revision !== selection?.revision ? "run.reason.selectionNotApplied"
    : "run.reason.paradigmMismatch");
  const startReason = operationReason ?? engineReason ?? (running ? t("run.reason.alreadyRunning")
    : !selection || configurationError ? t("run.reason.configurationUnavailable") : recordingReason);
  const stopReason = stopOperationReason ?? engineReason ?? (!running ? t("run.reason.notRunning") : null);
  const disconnectReason = operationReason ?? acquisitionReason ?? (!connected ? t("run.reason.notConnected")
    : reading ? t("run.reason.stopReading") : null);
  const readReason = operationReason ?? acquisitionReason ?? (!selection ? t("run.reason.configurationUnavailable")
    : selection.revision !== runtime.status?.accepted_revision ? t("run.reason.selectionChanged")
    : !selection.bindings.some((item) => item.slot === "acquisition.source") ? t("run.reason.selectSource")
    : running && runtime.status?.applied_revision !== selection.revision ? t("run.reason.selectionNotApplied")
    : reading ? t("run.reason.alreadyReading") : missingDetector ? t("run.reason.missingDetector") : recordingReason);
  const stopReadReason = stopOperationReason ?? acquisitionReason ?? (!reading ? t("run.reason.notReading") : null);
  const launchReason = operationReason ?? engineReason ?? (!running ? t("run.reason.startEngine") : null)
    ?? stimulationReason ?? trialSetupReason
    ?? (externalStimulation && !stimulation.data?.engine_connected ? t("run.reason.externalStimulation") : null)
    ?? (stimulation.data?.active && (stimulation.data.process_alive || stimulation.data.engine_connected)
      ? t("run.reason.stimulationActive") : null);
  const stopStimReason = stopOperationReason ?? stimulationReason
    ?? (!(stimulation.data?.process_alive || stimulation.data?.active || stimulation.data?.engine_connected)
      ? t("run.reason.stimulationStopped") : null);
  const triggerReason = operationReason ?? stimulationReason ?? trialSetupReason
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
  const steps = [
    { label: t("pipeline.selectPlugins"), done: Boolean(selection?.bindings.some((item) => item.slot === "acquisition.source")) },
    { label: t("pipeline.engineStarted"), done: engineAvailable && running },
    { label: t("pipeline.sourceConnected"), done: acquisitionAvailable && running && connected },
    { label: t("pipeline.reading"), done: acquisitionAvailable && running && connected && reading },
  ];
  const currentStep = steps.findIndex((step) => !step.done);

  const quickStart = async () => {
    if (readReason || !selection || quickLock.current) return;
    quickLock.current = true;
    setQuickPending(true); setQuickStage("checking"); setQuickError(null); setQuickOutcome(null);
    try {
      const result = await startAcquisition(selection.revision, setQuickStage);
      setQuickOutcome(result.acquisition.streaming ? null : "inactive");
      setQuickStage(null);
    } catch (cause) {
      setQuickError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      try { await Promise.all([refresh(), runtime.refresh()]); }
      finally { quickLock.current = false; setQuickPending(false); }
    }
  };

  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    let alive = true;
    void Promise.allSettled([
      pluginConfigApi.activation(), pluginConfigApi.profiles(), pluginConfigApi.catalog("paradigm"),
    ]).then(([activation, savedProfiles, paradigms]) => {
      if (!alive) return;
      setSelectedParadigm(null); setControls({});
      const selected = activation.status === "fulfilled" ? activation.value : null;
      setSelection(selected);
      setProfiles(savedProfiles.status === "fulfilled" ? savedProfiles.value : []);
      if (activation.status === "rejected") { setLoadError(String(activation.reason)); return; }
      if (!selected) { setLoadError({ key: "run.configurationUnavailable" }); return; }
      if (paradigms.status === "rejected") {
        setLoadError({ key: "run.trialControlsUnavailable", params: { detail: String(paradigms.reason) } }); return;
      }
      const binding = selected.bindings.find((item) => item.slot === "stimulation.paradigm");
      const paradigm = binding ? paradigms.value.find((item) => selectionKey(item.selection) === selectionKey(binding.selection)) : undefined;
      setSelectedParadigm(paradigm ?? null);
      try { setControls(readTrialControls(paradigm?.controls)); setLoadError(null); }
      catch (error) { setLoadError({ key: "run.invalidTrialControls", params: { detail: String(error) } }); }
    }).catch((error) => { if (alive) setLoadError(String(error)); });
    return () => { alive = false; };
  }, [runtime.status?.accepted_revision, runtime.status?.applied_revision]);

  // Controls are taken from the exact selected package, contribution, and version.
  useEffect(() => {
    setControlValues(Object.fromEntries(Object.entries(controls)
      .map(([name, control]) => [name, control.default_sequence ?? (control.default == null ? "" : String(control.default))])));
    setTrialError(null);
  }, [controls]);

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
    <ol className="pipeline" aria-label={t("run.systemProgress")} aria-live="polite">
      {steps.map((step, index) => <li className="step-wrap" key={step.label}
        aria-current={index === currentStep ? "step" : undefined}
        aria-label={`${step.label}: ${step.done ? t("run.complete") : index === currentStep ? t("run.currentStep") : t("run.pending")}`}>
        {index > 0 && <span className="step-line" aria-hidden="true">
          <span className={`step-line-half ${steps[index - 1].done ? "done" : ""}`} />
          <span className={`step-line-half ${step.done ? "done" : ""}`} />
        </span>}
        <div className={`step ${step.done ? "done" : index === currentStep ? "current" : ""}`}>
          <span className="step-dot" aria-hidden="true">{step.done ? <Icon name="check" /> : index + 1}</span>
          <span className="step-label">{step.label}</span>
        </div>
      </li>)}
    </ol>
    <section className="setup-summary" aria-label={t("run.selectedSetup")}>
      <h2 className="startup-title">{t("run.selectedSetup")}</h2>
      <div className="summary-items">
        <div><span>{t("run.selectedAcquisition")}</span><strong>{selectedName("acquisition.source")}</strong>{selectedVersion("acquisition.source")}</div>
        <div><span>{t("run.decoder")}</span><strong>{selectedName("processing.decoder")}</strong>{selectedVersion("processing.decoder")}</div>
        <div><span>{t("run.paradigm")}</span><strong>{selectedName("stimulation.paradigm")}</strong>{selectedVersion("stimulation.paradigm")}</div>
        <div><span>{t("run.actuation")}</span><strong>{selectedName("actuation.effector")}</strong>{selectedVersion("actuation.effector")}</div>
      </div>
      <button className="text-button" disabled={busy} onClick={onEditSetup}>{t("run.editSetup")} <Icon name="chevron" /></button>
      <p className="meta">{t("run.selectedConfigurationHelp")}</p>
    </section>
    {missingRecording && <p className="inline-message" role="status">{t("run.missingRecording")}</p>}
    {loadError && <p className="inline-message error" role="alert">{typeof loadError === "string" ? loadError : t(loadError.key, loadError.params)}</p>}
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
          {reading ? <button className="primary" disabled={!!stopReadReason}
            aria-describedby={stopReadReason ? "reading-action-reasons" : "acquisition-start-help"}
            onClick={() => void stopRead.run()}>{t("run.stopAcquisition")}</button>
            : <button className="primary" disabled={!!readReason}
              aria-describedby={readReason ? "reading-action-reasons acquisition-start-help" : "acquisition-start-help"}
              onClick={quickStart}><Icon name="play" />{acquisitionStartLabel}</button>}
        </div>
        <p className="meta" id="acquisition-start-help">{t(reading ? "run.stopAcquisitionHelp" : "run.quickStartHelp")}
          {playback && <> {t("run.playbackRestart")}</>}</p>
        {actionReasons("reading-action-reasons", [[reading ? t("run.stopAcquisition") : acquisitionStartLabel, reading ? stopReadReason : readReason]])}
        {missingDetector && onEditProcessing && <button className="text-button" disabled={busy} onClick={onEditProcessing}>{t("run.configureProcessing")}</button>}
        {quickStage && <p className="meta" role="status">{quickPending ? t(`run.stage.${quickStage}`)
          : t("run.startInterrupted", { stage: t(`run.stage.${quickStage}`) })}</p>}
        {quickError && <p className="error" role="alert">{quickError}</p>}
        {quickOutcome === "inactive" && acquisitionAvailable && connected && !reading && !quickPending
          && <p className="inline-message" role="status">{t("run.readingInactiveAfterStart")}</p>}
        {actionErrors([[t("run.stopAcquisition"), stopRead.error]])}
        <AcquisitionPreview generation={running ? runtime.status?.generation : null}
          revision={runtime.status?.applied_revision} connectionId={acquisition.data?.connection_id ?? null}
          streaming={reading} disabled={controlsBlocked} />
      </section>
      <section className="card"><h2><Icon name="system" />{t("run.stimulation")}</h2>
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
          <button disabled={stopBlocked || !stimulationAvailable || !(stimulation.data?.process_alive || stimulation.data?.active || stimulation.data?.engine_connected)}
            aria-describedby={stopStimReason ? "stimulation-action-reasons" : undefined} onClick={() => void stopStim.run()}>{t("run.stopStimulation")}</button></div>
        {actionReasons("stimulation-action-reasons", [[stimulationStartLabel, launchReason], [t("run.stopStimulation"), stopStimReason]])}
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
    </div>
    {feed.error && <p className="error" role="alert">{feed.error}</p>}
    <section className="setup-summary" aria-label={t("run.activeDecoderInput")}>
      <div className="summary-items"><div><span>{t("run.signalPreparation")}</span>
        <strong>{!engineAvailable || signal.error ? t("run.unavailable") : decoderConfigured === null ? t("run.loading")
          : !decoderConfigured ? t("run.processingNotConfigured") : missingDetector ? t("run.epochDetectorMissing")
          : !running ? t("run.engineStopped") : !signal.data ? t("run.loading")
          : signal.data.readiness.ready ? t("run.ready") : t("run.validatedBeforeReading")}</strong></div>
        <div><span>{t("run.activeDecoderChannels")}</span><strong>{!engineAvailable || signal.error ? t("run.unavailable") : !running ? t("run.none") : !signal.data ? t("run.loading") : signal.data.active_input
          ? signal.data.active_input.outputs.length ? signal.data.active_input.outputs.map((output) => output.name).join(", ") : t("run.allSourceChannels")
          : t("run.noActiveMapping")}</strong></div></div>
      {signal.error && <p className="error" role="alert">{signal.error.message}</p>}
      {decoderConfigured && !missingDetector && engineAvailable && !signal.error && signal.data && !signal.data.readiness.ready && running && <p className="meta">{t("run.prepareSignalHelp")}</p>}
    </section>
    <details className="card runtime-advanced"><summary>{t("run.advancedControls")}</summary>
    <div className="engine-card" aria-label={t("run.engineControls")}>
      <div><h2>{t("run.engine")}</h2><div className="engine-status">
        <Dot on={engineAvailable ? running : undefined} label={t("run.running")} /><Dot on={engineAvailable ? engine.data?.acquisition_alive : undefined} label={t("run.acquisitionProcess")} />
        <Dot on={engineAvailable ? engine.data?.processor_alive : undefined} label={t("run.processingProcess")} />
      </div>{engine.error && <p className="error" role="alert">{engine.error.message}</p>}</div>
      <div className="control-feedback"><div className="actions"><button className="primary" disabled={controlsBlocked || !engineAvailable || running || !selection || !!configurationError || missingRecording}
        aria-describedby={startReason ? "engine-action-reasons" : undefined}
        onClick={() => void start.run()}><Icon name="play" />{start.pending ? t("run.starting") : t("run.startEngine")}</button>
        <button className="danger" disabled={stopBlocked || !engineAvailable || !running}
          aria-describedby={stopReason ? "engine-action-reasons" : undefined} onClick={() => void stop.run()}>{t("run.stopEngine")}</button></div>
        {actionReasons("engine-action-reasons", [[t("run.startEngine"), startReason], [t("run.stopEngine"), stopReason]])}
        {actionErrors([[t("run.startEngine"), start.error], [t("run.stopEngine"), stop.error]])}
      </div>
    </div>
    <div className="actions runtime-disconnect"><button disabled={!!disconnectReason}
      aria-describedby={disconnectReason ? "connection-action-reasons" : undefined} onClick={() => void disconnect.run()}>{t("run.disconnect")}</button>
      <span className="meta">{t("run.disconnectHelp")}</span></div>
    {actionReasons("connection-action-reasons", [[t("run.disconnect"), disconnectReason]])}
    {actionErrors([[t("run.disconnect"), disconnect.error]])}
    </details>
    <EpochProcessing onResults={onResults} disabled={busy} />
    <details className="ack-panel"><summary>{t("run.acknowledgements")} <span className="count">{feed.acks.length}</span></summary>
      <p className="meta">{t("run.acknowledgementsScope", { limit: MAX_ACKS })}</p>
      {!feed.acks.length ? <p className="meta">{t("run.acknowledgementsHelp")}</p> : <ul className="acks">
        {feed.acks.map((ack, index) => <li key={index}><code>{ack.command}</code>
          {ack.target_index != null && <> → {t("run.target", { index: ack.target_index })}</>} · {ack.status === "ok" || ack.status === "error" ? t(`run.ack.${ack.status}`) : ack.status}
          {ack.message && <span className="meta"> {ack.message}</span>}</li>)}</ul>}
    </details>
  </div>;
}
