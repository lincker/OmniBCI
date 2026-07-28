import { useState, useEffect } from "react";
import {
  api,
  ApiError,
  type AcquisitionStatus,
  type ParadigmInfo,
} from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { useAction } from "./hooks/useAction";
import { useResultsFeed } from "./hooks/useResultsFeed";
import { ConfigCard } from "./ConfigCard";
import {
  translateControlLabel,
  translatePluginName,
  useI18n,
} from "./i18n";
import "./styles.css";

function Dot({ on, label }: { on: boolean | undefined; label: string }) {
  return (
    <span className={`dot ${on ? "dot-on" : "dot-off"}`} title={label}>
      ● {label}
    </span>
  );
}

function ActionError({ messages }: { messages: (string | null)[] }) {
  const msg = messages.find((m) => m != null);
  return msg ? <p className="error action-error">{msg}</p> : null;
}

function EngineCard({
  onLoaded,
  onDirtyChange,
}: {
  onLoaded?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const { data, error, refresh } = usePoll(api.engineStatus);
  const stop = useAction(api.engineStop, refresh);
  const running = data?.running ?? false;

  // Data-source and decoder selectors (formerly in the load panel), embedded in the engine panel.
  const [options, setOptions] = useState<Record<string, ParadigmInfo[]>>({});
  const [values, setValues] = useState<Record<string, string>>({}); // Applied configuration values.
  const [draft, setDraft] = useState<Record<string, string>>({});   // Local pending selections.
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      const [cfg, ...pluginResps] = await Promise.all([
        api.getConfig().catch(() => null),
        ...PLUGIN_CATEGORIES.map(({ category }) => api.listPlugins(category).catch(() => null)),
      ]);
      const vals: Record<string, string> = {};
      if (cfg) {
        for (const { configKey } of PLUGIN_CATEGORIES) {
          const entry = cfg.entries.find((e) => e.key === configKey);
          if (entry?.effective_value != null) vals[configKey] = String(entry.effective_value);
        }
      }
      setValues(vals);
      setDraft(vals);
      const opts: Record<string, ParadigmInfo[]> = {};
      PLUGIN_CATEGORIES.forEach(({ category }, i) => {
        const resp = pluginResps[i];
        if (resp) opts[category] = resp.paradigms;
      });
      setOptions(opts);
    }
    void load();
  }, []);

  // Report whether the selectors differ from the applied configuration so the pipeline can update.
  const dirty = PLUGIN_CATEGORIES.some(
    ({ configKey }) => !!draft[configKey] && draft[configKey] !== values[configKey],
  );
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // Starting the engine first persists changed selections, then starts the engine.
  const handleStart = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const updates: Record<string, string> = {};
      for (const { configKey } of PLUGIN_CATEGORIES) {
        const v = draft[configKey];
        if (v && v !== values[configKey]) updates[configKey] = v;
      }
      if (Object.keys(updates).length > 0) {
        await api.patchConfig(updates);
        setValues((v) => ({ ...v, ...updates }));
      }
      onLoaded?.();
      await api.engineStart();
      refresh();
    } catch (e) {
      setStartError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
    } finally {
      setStarting(false);
    }
  };

  return (
    <section className="card card-stack">
      <h2>{t("engine.title")}</h2>
      {/* Keep status indicators at the top. */}
      <div className="engine-status">
        {error ? (
          <p className="error">
            {t("engine.backendUnreachable", { message: error.message })}
          </p>
        ) : (
          <>
            <Dot on={data?.running} label={t("engine.running")} />
            <Dot on={data?.acquisition_alive} label={t("engine.acquisitionProcess")} />
            <Dot on={data?.processor_alive} label={t("engine.processingProcess")} />
            <Dot
              on={data?.stimulation_connected}
              label={t("engine.stimulationConnected")}
            />
          </>
        )}
      </div>
      {/* Lower panels: source/decoder selectors on the left, engine controls on the right. */}
      <div className="engine-body">
        <div className="engine-selectors">
          {PLUGIN_CATEGORIES.map(({ category, labelKey, configKey }) => (
            <label key={category}>
              <span className="plugin-bar-label">{t(labelKey)}</span>
              <select
                disabled={running || starting}
                value={draft[configKey] ?? ""}
                onChange={(e) => setDraft((d) => ({ ...d, [configKey]: e.target.value }))}
              >
                {!draft[configKey] && <option value="">{t("common.select")}</option>}
                {(options[category] ?? []).map((p) => (
                  <option key={p.plugin_type} value={p.plugin_type}>
                    {translatePluginName(p.plugin_type, p.display_name, t)}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        <div className="actions engine-buttons">
          <button disabled={running || starting} onClick={() => void handleStart()}>
            {starting ? t("engine.starting") : t("engine.start")}
          </button>
          <button className="danger" disabled={!running || stop.pending} onClick={() => stop.run()}>
            {t("engine.stop")}
          </button>
        </div>
      </div>
      <ActionError messages={[startError, stop.error]} />
    </section>
  );
}

function AcquisitionSessionCard({
  engineRunning,
  acquisitionStatus,
  acquisitionError,
  refreshAcquisitionStatus,
  session,
  trialCount,
}: {
  engineRunning: boolean;
  acquisitionStatus: AcquisitionStatus | null;
  acquisitionError: Error | null;
  refreshAcquisitionStatus: () => Promise<void>;
  session: { session_id: string; paradigm?: string | null; state: string } | null;
  trialCount: number;
}) {
  const { t } = useI18n();
  const connect = useAction(api.acqConnect, refreshAcquisitionStatus);
  const disconnect = useAction(api.acqDisconnect, refreshAcquisitionStatus);
  const recStart = useAction(api.recordingStart, refreshAcquisitionStatus); // Create a session, then start the stream.
  const recStop = useAction(api.recordingStop, refreshAcquisitionStatus); // Stop the stream, drain it, then end the session.
  const connected = acquisitionStatus?.connected ?? false;
  const streaming = acquisitionStatus?.streaming ?? false;
  const recordingActive = session?.state === "running";

  return (
    <section className="card card-stack">
      <h2>{t("acquisition.title")}</h2>
      {acquisitionError ? (
        <p className="error">{t("common.unreachable")}</p>
      ) : (
        <>
          <p>
            <Dot
              on={acquisitionStatus?.connected}
              label={t("acquisition.connected")}
            />
            <Dot
              on={acquisitionStatus?.streaming}
              label={t("acquisition.streaming")}
            />
          </p>
          <p className="meta">
            {t("acquisition.metadata", {
              source: acquisitionStatus?.source ?? "—",
              sampleRate: acquisitionStatus?.sample_rate ?? "—",
              channelCount: acquisitionStatus?.channel_count ?? "—",
            })}
          </p>
        </>
      )}
      {session ? (
        <p className="meta">
          {t("acquisition.session", {
            sessionId: session.session_id,
            paradigm: session.paradigm ?? "—",
            state:
              session.state === "running"
                ? t("acquisition.sessionRunning")
                : t("acquisition.sessionEnded"),
            trialCount,
          })}
        </p>
      ) : (
        <p className="meta">{t("acquisition.noActiveSession")}</p>
      )}
      <div className="actions actions-grouped">
        <div className="btn-group">
          <span className="btn-group-title">{t("acquisition.sourceConnection")}</span>
          <button
            disabled={!engineRunning || connected || connect.pending}
            onClick={() => connect.run()}
          >
            {t("acquisition.connect")}
          </button>
          <button
            className="danger"
            disabled={!connected || disconnect.pending}
            onClick={() => disconnect.run()}
          >
            {t("acquisition.disconnect")}
          </button>
        </div>
        <div className="btn-group">
          <span className="btn-group-title">{t("acquisition.dataReading")}</span>
          <button
            disabled={!connected || streaming || recordingActive || recStart.pending}
            onClick={() => recStart.run()}
          >
            {t("acquisition.startReading")}
          </button>
          <button
            className="danger"
            disabled={!recordingActive || recStop.pending}
            onClick={() => recStop.run()}
          >
            {t("acquisition.stopReading")}
          </button>
        </div>
      </div>
      <ActionError messages={[connect.error, disconnect.error, recStart.error, recStop.error]} />
    </section>
  );
}

function StimulationCard({ sessionActive }: { sessionActive: boolean }) {
  const { t } = useI18n();
  const [controlValues, setControlValues] = useState<Record<string, string>>({});
  const [paradigm, setParadigm] = useState<string>("");
  const [paradigms, setParadigms] = useState<ParadigmInfo[]>([]);
  const { data: status } = usePoll(api.stimulationStatus);
  const stimStart = useAction(api.stimStart);
  const stimStop = useAction(api.stimStop);
  const trial = useAction(api.trialStart);

  useEffect(() => {
    api.listPlugins("paradigm").then((r) => {
      if (r) setParadigms(r.paradigms);
    }).catch(() => {});
  }, []);

  const selectedParadigm = paradigms.find((p) => p.plugin_type === paradigm);
  const controls = selectedParadigm?.controls ?? {};

  // Seed each editable control with its manifest `default` when the selection changes.
  useEffect(() => {
    const init: Record<string, string> = {};
    for (const [name, desc] of Object.entries(controls)) {
      init[name] = desc?.default != null ? String(desc.default) : "";
    }
    setControlValues(init);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedParadigm]);

  // Build the trial request from control values, routed by each control's `field`.
  const triggerTrial = () => {
    let nTrials = 1;
    let perTrial: number | undefined;
    for (const [name, desc] of Object.entries(controls)) {
      const raw = controlValues[name];
      const num = raw === "" || raw == null ? undefined : Number(raw);
      if (desc?.field === "n_trials") nTrials = num ?? 1;
      else if (desc?.field === "trial_params") perTrial = num;
    }
    nTrials = Math.max(1, nTrials);
    const trialParams =
      perTrial != null ? Array(nTrials).fill(perTrial) : undefined;
    trial.run(nTrials, trialParams);
  };

  return (
    <section className="card">
      <h2>{t("stimulation.title")}</h2>
      <p>
        <Dot on={status?.process_alive} label={t("stimulation.processRunning")} />
        <Dot
          on={status?.engine_connected}
          label={t("stimulation.engineConnected")}
        />
      </p>
      {status?.paradigm && (
        <p className="meta">
          {t("stimulation.runningParadigm", {
            paradigm: (() => {
              const active = paradigms.find(
                (p) => p.plugin_type === status.paradigm,
              );
              return active
                ? translatePluginName(active.plugin_type, active.display_name, t)
                : status.paradigm;
            })(),
          })}
        </p>
      )}
      <div className="actions">
        <select value={paradigm} onChange={(e) => setParadigm(e.target.value)}>
          <option value="">{t("stimulation.useConfigured")}</option>
          {paradigms.map((p) => (
            <option key={p.plugin_type} value={p.plugin_type}>
              {translatePluginName(p.plugin_type, p.display_name, t)}
            </option>
          ))}
        </select>
        <button disabled={stimStart.pending} onClick={() => stimStart.run(paradigm || undefined)}>
          {t("stimulation.start")}
        </button>
        <button className="danger" disabled={stimStop.pending} onClick={() => stimStop.run()}>
          {t("stimulation.stop")}
        </button>
      </div>
      {selectedParadigm && Object.keys(selectedParadigm.params ?? {}).length > 0 && (
        <table className="params">
          <tbody>
            {Object.entries(selectedParadigm.params ?? {}).map(([k, v]) => (
              <tr key={k}>
                <td className="meta">{k}</td>
                <td>{String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="actions">
        {Object.entries(controls).map(([name, desc]) => (
          <label key={name} className="control">
            <span className="meta">
              {translateControlLabel(name, String(desc?.label ?? name), t)}
            </span>
            <input
              type="number"
              min={desc?.min != null ? Number(desc.min) : undefined}
              max={desc?.max != null ? Number(desc.max) : undefined}
              value={controlValues[name] ?? ""}
              onChange={(e) =>
                setControlValues((v) => ({ ...v, [name]: e.target.value }))
              }
            />
          </label>
        ))}
        <button
          disabled={!sessionActive || trial.pending}
          title={sessionActive ? "" : t("stimulation.sessionRequired")}
          onClick={triggerTrial}
        >
          {t("stimulation.triggerTrial")}
        </button>
      </div>
      <ActionError messages={[stimStart.error, stimStop.error, trial.error]} />
    </section>
  );
}

function EffectorCard() {
  const { t } = useI18n();
  const [effectors, setEffectors] = useState<ParadigmInfo[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    api.listPlugins("effector").then((r) => {
      if (r) setEffectors(r.paradigms);
    }).catch(() => {});
  }, []);

  const handleLoad = async () => {
    if (!selected) return;
    setSaving(true);
    setNotice(null);
    setSaveError(null);
    try {
      const result = await api.patchConfig({ "ACTUATION.effector": selected });
      setNotice(
        result?.engine_restart_required
          ? t("common.savedRestart")
          : t("common.saved"),
      );
    } catch (e) {
      setSaveError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card">
      <h2>{t("effector.title")}</h2>
      <div className="actions">
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">{t("effector.select")}</option>
          {effectors.map((e) => (
            <option key={e.plugin_type} value={e.plugin_type}>
              {translatePluginName(e.plugin_type, e.display_name, t)}
            </option>
          ))}
        </select>
        <button disabled={!selected || saving} onClick={() => void handleLoad()}>
          {saving ? t("common.saving") : t("common.load")}
        </button>
      </div>
      {notice && <p className="meta">{notice}</p>}
      {saveError && <p className="error action-error">{saveError}</p>}
    </section>
  );
}

// Plugin selector categories embedded in the engine panel.
const PLUGIN_CATEGORIES = [
  {
    category: "acquisition",
    labelKey: "engine.dataSource",
    configKey: "ACQUISITION.signal_source" as const,
  },
  {
    category: "decoder",
    labelKey: "engine.decoder",
    configKey: "ALGORITHM.decoder" as const,
  },
] as const;

function PipelineStepper({
  configLoaded,
  engineRunning,
  connected,
  streaming,
}: {
  configLoaded: boolean;
  engineRunning: boolean;
  connected: boolean;
  streaming: boolean;
}) {
  const { t } = useI18n();
  const steps = [
    { label: t("pipeline.selectPlugins"), done: configLoaded },
    { label: t("pipeline.engineStarted"), done: engineRunning },
    { label: t("pipeline.sourceConnected"), done: connected },
    { label: t("pipeline.reading"), done: streaming },
  ];
  // The first incomplete step is the current step.
  const currentIdx = steps.findIndex((s) => !s.done);

  return (
    <div className="pipeline">
      {steps.map((s, i) => (
        <div key={s.label} className="step-wrap">
          {i > 0 && (
            <span className="step-line">
              <span className={`step-line-half ${steps[i - 1].done ? "done" : ""}`} />
              <span className={`step-line-half ${steps[i].done ? "done" : ""}`} />
            </span>
          )}
          <div className={`step ${s.done ? "done" : i === currentIdx ? "current" : ""}`}>
            <span className="step-dot">{s.done ? "✓" : i + 1}</span>
            <span className="step-label">{s.label}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

type Tab = "system" | "config";

export default function App() {
  const { language, setLanguage, t } = useI18n();
  const [tab, setTab] = useState<Tab>("system");
  const [configLoaded, setConfigLoaded] = useState(false);
  const [dataSourceDirty, setDataSourceDirty] = useState(false);
  const { connection, session, trials, acks } = useResultsFeed();
  const { data: engineStatus } = usePoll(api.engineStatus);
  const {
    data: acqStatus,
    error: acqError,
    refresh: refreshAcqStatus,
  } = usePoll(api.acquisitionStatus);
  const engineRunning = engineStatus?.running ?? false;
  // A running engine implies the configuration was already loaded before a page refresh.
  const effectiveConfigLoaded = configLoaded || engineRunning;
  const sessionActive = session != null && session.state === "running";

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>
          <span className="brand-mark">Ω</span>
          {t("app.title")}
        </h1>
        <nav>
          {(["system", "config"] as Tab[]).map((navTab) => (
            <button
              key={navTab}
              className={navTab === tab ? "nav-item active" : "nav-item"}
              onClick={() => setTab(navTab)}
            >
              {navTab === "system" ? (
                <svg className="nav-ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <rect x="1.5" y="1.5" width="13" height="13" rx="2.5" />
                  <path d="M4.5 9.5 7 6.5l2 2.5 2.5-3.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : (
                <svg className="nav-ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <circle cx="8" cy="8" r="2.2" />
                  <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" strokeLinecap="round" />
                </svg>
              )}
              {navTab === "system" ? t("nav.system") : t("nav.config")}
              {navTab === "system" && (
                <span className={`nav-dot ${engineRunning ? "dot-on" : "dot-off"}`}>●</span>
              )}
            </button>
          ))}
        </nav>
        <label className="language-switcher">
          <span>{t("language.selector")}</span>
          <select
            aria-label={t("language.selector")}
            value={language}
            onChange={(event) => setLanguage(event.target.value as "zh" | "en")}
          >
            <option value="zh">{t("language.chinese")}</option>
            <option value="en">{t("language.english")}</option>
          </select>
        </label>
        <span className={`badge badge-${connection}`}>
          {connection === "open"
            ? t("connection.live")
            : connection === "connecting"
              ? t("connection.connecting")
              : t("connection.reconnecting")}
        </span>
      </aside>

      <main className="content">
        <header className="content-head">
          <h1>{tab === "system" ? t("nav.system") : t("nav.config")}</h1>
        </header>

        {tab === "config" ? (
          <div className="grid">
            <ConfigCard />
          </div>
        ) : (
      <>
      <PipelineStepper
        configLoaded={effectiveConfigLoaded || dataSourceDirty}
        engineRunning={engineRunning}
        connected={acqStatus?.connected ?? false}
        streaming={acqStatus?.streaming ?? false}
      />
      <div className="grid">
        <EngineCard onLoaded={() => setConfigLoaded(true)} onDirtyChange={setDataSourceDirty} />
        <AcquisitionSessionCard
          engineRunning={engineRunning}
          acquisitionStatus={acqStatus}
          acquisitionError={acqError}
          refreshAcquisitionStatus={refreshAcqStatus}
          session={session}
          trialCount={trials.length}
        />
        <StimulationCard sessionActive={sessionActive} />
        <EffectorCard />

        <section className="card card-wide">
          <h2>{t("results.title")}</h2>
          {trials.length === 0 ? (
            <p className="meta">{t("results.empty")}</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>{t("results.target")}</th>
                  <th>{t("results.event")}</th>
                  <th>{t("results.confidence")}</th>
                  <th>{t("results.forced")}</th>
                  <th>{t("results.time")}</th>
                </tr>
              </thead>
              <tbody>
                {[...trials].reverse().map((trialResult) => (
                  <tr key={trialResult.trial_number}>
                    <td>{trialResult.trial_number}</td>
                    <td className="target">{trialResult.target_id}</td>
                    <td>{trialResult.event_id}</td>
                    <td>
                      {trialResult.confidence != null
                        ? trialResult.confidence.toFixed(2)
                        : "—"}
                    </td>
                    <td>{trialResult.forced ? t("common.yes") : ""}</td>
                    <td className="meta">
                      {trialResult.produced_at.replace("T", " ").slice(0, 19)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card card-wide">
          <h2>{t("acks.title")}</h2>
          {acks.length === 0 ? (
            <p className="meta">{t("acks.empty")}</p>
          ) : (
            <ul className="acks">
              {acks.map((a, i) => (
                <li key={i}>
                  <code>{a.command}</code>
                  {a.target_index != null && (
                    <> → {t("acks.target", { target: a.target_index })}</>
                  )}{" "}
                  ・ {a.status}
                  {a.message && <span className="meta"> {a.message}</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      </>
        )}
      </main>
    </div>
  );
}
