import { useEffect, useState } from "react";
import { api, type AcquisitionStatus, type ConfigData, type EngineStatus, type ParadigmInfo } from "./api/client";
import { useAction } from "./hooks/useAction";
import { usePoll } from "./hooks/usePoll";
import type { ResultsFeed } from "./hooks/useResultsFeed";
import { translateControlLabel, translatePluginName, useI18n } from "./i18n";
import { SessionSummary, TrialResults } from "./TrialResults";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";

function Dot({ on, label }: { on: boolean | undefined; label: string }) {
  return <span className={`dot ${on ? "dot-on" : "dot-off"}`}>● {label}</span>;
}

export function RunConsole({ engine, acquisition, feed, onBusyChange, onEditSetup, onResults }: {
  engine: ReturnType<typeof usePoll<EngineStatus>>;
  acquisition: ReturnType<typeof usePoll<AcquisitionStatus>>;
  feed: ResultsFeed;
  onBusyChange: (busy: boolean) => void;
  onEditSetup: () => void;
  onResults: () => void;
}) {
  const { t } = useI18n();
  const stimulation = usePoll(api.stimulationStatus);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [sourceName, setSourceName] = useState("");
  const [plugins, setPlugins] = useState<ParadigmInfo[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [controlValues, setControlValues] = useState<Record<string, string>>({});
  const [trialError, setTrialError] = useState<string | null>(null);
  const refresh = async () => { await Promise.all([engine.refresh(), acquisition.refresh(), stimulation.refresh()]); };
  const start = useAction(api.engineStart, refresh);
  const stop = useAction(api.engineStop, refresh);
  const connect = useAction(api.acqConnect, refresh);
  const disconnect = useAction(api.acqDisconnect, refresh);
  const read = useAction(api.recordingStart, refresh);
  const stopRead = useAction(api.recordingStop, refresh);
  const launch = useAction(api.stimStart, refresh);
  const stopStim = useAction(api.stimStop, refresh);
  const trigger = useAction(api.trialStart);
  const actions = [start, stop, connect, disconnect, read, stopRead, launch, stopStim, trigger];
  const busy = actions.some((action) => action.pending);
  const actionError = actions.find((action) => action.error)?.error;
  const running = engine.data?.running ?? false;
  const connected = acquisition.data?.connected ?? false;
  const reading = acquisition.data?.streaming ?? false;
  const recording = feed.session?.state === "running";
  const engineAvailable = engine.data !== null && !engine.error;
  const acquisitionAvailable = engineAvailable && acquisition.data !== null && !acquisition.error;
  const stimulationAvailable = stimulation.data !== null && !stimulation.error;
  const configured = (key: string) => String(config?.entries.find((entry) => entry.key === key)?.effective_value ?? "");
  const pluginName = (id: string) => {
    const plugin = plugins.find((item) => item.plugin_type === id);
    return id ? translatePluginName(id, plugin?.display_name ?? id, t) : "None";
  };
  const currentParadigm = stimulation.data?.paradigm || configured("STIMULATION.paradigm");
  const selectedParadigm = plugins.find((plugin) => plugin.plugin_type === currentParadigm);
  const controls = selectedParadigm?.controls ?? {};
  const hasTrialCount = Object.values(controls).some((control) => control.field === "n_trials");
  const steps = [
    { label: t("pipeline.selectPlugins"), done: (engineAvailable && running)
      || Boolean(configured("ACQUISITION.signal_source") && configured("ALGORITHM.decoder")) },
    { label: t("pipeline.engineStarted"), done: engineAvailable && running },
    { label: t("pipeline.sourceConnected"), done: acquisitionAvailable && running && connected },
    { label: t("pipeline.reading"), done: acquisitionAvailable && running && connected && reading },
  ];
  const currentStep = steps.findIndex((step) => !step.done);

  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    let alive = true;
    void Promise.all([
      api.getConfig(), api.getAcquisitionConfig(), api.listAcquisitionProfiles().catch(() => null),
      ...["paradigm", "decoder", "effector"].map((category) => api.listPlugins(category).catch(() => null)),
    ]).then(([cfg, module, profiles, ...catalogs]) => {
      if (!alive) return;
      if (!cfg || !module) throw new Error("Configuration is unavailable.");
      setConfig(cfg);
      setPlugins(catalogs.flatMap((catalog) => catalog?.paradigms ?? []));
      setSourceName(profiles?.profiles.find((profile) => profile.profile_id === module.active_profile_id)?.name
        ?? module.plugins.find((plugin) => plugin.plugin_type === module.effective_plugin_type)?.label
        ?? module.effective_plugin_type ?? "Not configured");
    }).catch((error) => { if (alive) setLoadError(String(error)); });
    return () => { alive = false; };
  }, []);

  // Trial controls follow the running paradigm, or the configured paradigm before launch.
  useEffect(() => {
    setControlValues(Object.fromEntries(Object.entries(selectedParadigm?.controls ?? {})
      .map(([name, control]) => [name, control.default == null ? "" : String(control.default)])));
    setTrialError(null);
  }, [selectedParadigm]);

  const triggerTrial = () => {
    let nTrials = hasTrialCount ? 1 : Number(controlValues.n_trials ?? 1);
    let perTrial: number | undefined;
    for (const [name, control] of Object.entries(controls)) {
      const raw = controlValues[name];
      const value = raw?.trim() ? Number(raw) : undefined;
      if (value === undefined || !Number.isFinite(value)
          || (control.field === "trial_params" && !Number.isSafeInteger(value))
          || (control.min != null && value < Number(control.min))
          || (control.max != null && value > Number(control.max))) {
        setTrialError(`Enter a valid value for ${String(control.label ?? name)}.`); return;
      }
      if (control.field === "n_trials") nTrials = value;
      if (control.field === "trial_params") perTrial = value;
    }
    if (!Number.isSafeInteger(nTrials) || nTrials < 1 || nTrials > 10000) {
      setTrialError("Trial count must be an integer between 1 and 10000."); return;
    }
    setTrialError(null);
    void trigger.run(nTrials, perTrial === undefined ? undefined : Array(nTrials).fill(perTrial));
  };

  return <div className="runtime-content">
    <div className="breadcrumbs">Run <Icon name="chevron" /> Console</div>
    <header className="editor-heading"><div><p className="eyebrow">Control center</p><h1>Run console</h1>
      <p>Connect a source, start reading, and control stimulation.</p></div>
      <span className={`status-tag ${running ? "success" : ""}`}><span className="status-dot" />
        {!engineAvailable ? "Engine unavailable" : running ? "Engine running" : "Engine stopped"}</span></header>
    <ol className="pipeline" aria-label="System progress" aria-live="polite">
      {steps.map((step, index) => <li className="step-wrap" key={step.label}
        aria-current={index === currentStep ? "step" : undefined}
        aria-label={`${step.label}: ${step.done ? "Complete" : index === currentStep ? "Current step" : "Pending"}`}>
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
    <section className="setup-summary" aria-label="Selected setup">
      <div className="summary-items">
        <div><span>Selected acquisition</span><strong>{sourceName || "Loading…"}</strong></div>
        <div><span>Decoder</span><strong>{config ? pluginName(configured("ALGORITHM.decoder")) : "—"}</strong></div>
        <div><span>Paradigm</span><strong>{config ? pluginName(configured("STIMULATION.paradigm")) : "—"}</strong></div>
        <div><span>Actuation</span><strong>{config ? pluginName(configured("ACTUATION.effector")) : "—"}</strong></div>
      </div>
      <button className="text-button" disabled={busy} onClick={onEditSetup}>Edit setup <Icon name="chevron" /></button>
      <p className="meta">Saved configuration · Engine settings apply on the next start.</p>
    </section>
    {loadError && <p className="inline-message error" role="alert">{loadError}</p>}
    <section className="card engine-card" aria-label="Engine controls">
      <div><h2>Engine</h2><div className="engine-status">
        <Dot on={running} label="Running" /><Dot on={engine.data?.acquisition_alive} label="Acquisition process" />
        <Dot on={engine.data?.processor_alive} label="Processing process" />
      </div>{engine.error && <p className="error" role="alert">{engine.error.message}</p>}</div>
      <div className="actions"><button className="primary" disabled={busy || !engineAvailable || running || !config}
        onClick={() => void start.run()}><Icon name="play" />{start.pending ? "Starting…" : "Start engine"}</button>
        <button className="danger" disabled={busy || !engineAvailable || !running} onClick={() => void stop.run()}>Stop engine</button></div>
    </section>
    <div className="runtime-grid">
      <section className="card"><h2><Icon name="signal" />Acquisition</h2>
        <p><Dot on={connected} label="Source connected" /><Dot on={reading} label="Reading" /></p>
        {acquisition.error && <p className="error" role="alert">{acquisition.error.message}</p>}
        <p className="meta">{acquisition.data?.source ?? "No source"}<br />
          {acquisition.data?.sample_rate ?? "—"} Hz · {acquisition.data?.channel_count ?? "—"} channels</p>
        <div className="actions actions-grouped">
          <div className="btn-group"><span className="btn-group-title">Source connection</span>
            <button disabled={busy || !acquisitionAvailable || !running || connected} onClick={() => void connect.run()}>Connect</button>
            <button disabled={busy || !acquisitionAvailable || !connected || reading || recording} onClick={() => void disconnect.run()}>Disconnect</button></div>
          <div className="btn-group"><span className="btn-group-title">Data reading</span>
            <button className="primary" disabled={busy || !acquisitionAvailable || !running || !connected || reading || recording}
              onClick={() => void read.run()}>Start reading</button>
            <button disabled={busy || !acquisitionAvailable || (!reading && !recording)} onClick={() => void stopRead.run()}>Stop reading</button></div>
        </div>
      </section>
      <section className="card"><h2><Icon name="system" />Stimulation</h2>
        <p><Dot on={stimulation.data?.process_alive} label="Process running" />
          <Dot on={stimulation.data?.engine_connected} label="Engine connected" /></p>
        {stimulation.error && <p className="error" role="alert">{stimulation.error.message}</p>}
        <p className="meta">{pluginName(currentParadigm)}</p>
        <div className="actions"><button disabled={busy || !engineAvailable || !running || !stimulationAvailable || !config
            || (stimulation.data?.active && (stimulation.data.process_alive || stimulation.data.engine_connected))}
          onClick={() => void launch.run()}>{launch.pending ? "Launching…" : "Launch stimulation"}</button>
          <button disabled={busy || !stimulationAvailable || !(stimulation.data?.process_alive || stimulation.data?.active || stimulation.data?.engine_connected)}
            onClick={() => void stopStim.run()}>Stop stimulation</button></div>
        <form className="actions trial-controls" onSubmit={(event) => { event.preventDefault(); triggerTrial(); }}>
          {!hasTrialCount && <label className="control"><span className="meta">Number of trials</span>
            <input aria-label="Number of trials" type="number" required min={1} max={10000} step={1} disabled={busy}
              value={controlValues.n_trials ?? "1"} onChange={(event) => setControlValues({ ...controlValues, n_trials: event.target.value })} /></label>}
          {Object.entries(controls).map(([name, control]) => <label key={name} className="control">
            <span className="meta">{translateControlLabel(name, String(control.label ?? name), t)}</span>
            <input type="number" required disabled={busy} value={controlValues[name] ?? ""}
              min={control.min == null ? (control.field === "n_trials" ? 1 : undefined) : Number(control.min)}
              max={control.max == null ? (control.field === "n_trials" ? 10000 : undefined) : Number(control.max)}
              step={control.field === "n_trials" || control.field === "trial_params" || control.type === "int" ? 1 : "any"}
              onChange={(event) => setControlValues({ ...controlValues, [name]: event.target.value })} /></label>)}
          <button type="submit" className="primary" disabled={busy || !stimulationAvailable || !stimulation.data?.active || !stimulation.data.engine_connected}>Trigger trial</button>
        </form>
      </section>
    </div>
    {(actionError || trialError) && <p className="inline-message error" role="alert">{trialError || actionError}</p>}
    <SessionSummary session={feed.session} trialCount={feed.trials.length} />
    <section className="card"><h2>Recent results <button className="text-button" disabled={busy} onClick={onResults}>View all results <Icon name="chevron" /></button></h2>
      <TrialResults trials={feed.trials.slice(-3)} /></section>
    <details className="ack-panel"><summary>Acknowledgements <span className="count">{feed.acks.length}</span></summary>
      {!feed.acks.length ? <p className="meta">Stimulation responses will appear here.</p> : <ul className="acks">
        {feed.acks.map((ack, index) => <li key={index}><code>{ack.command}</code>
          {ack.target_index != null && <> → Target {ack.target_index}</>} · {ack.status}
          {ack.message && <span className="meta"> {ack.message}</span>}</li>)}</ul>}
    </details>
  </div>;
}
