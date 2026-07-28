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
      <h2>引擎</h2>
      {/* Keep status indicators at the top. */}
      <div className="engine-status">
        {error ? (
          <p className="error">后端不可达：{error.message}</p>
        ) : (
          <>
            <Dot on={data?.running} label="运行中" />
            <Dot on={data?.acquisition_alive} label="采集进程" />
            <Dot on={data?.processor_alive} label="处理进程" />
            <Dot on={data?.stimulation_connected} label="刺激已连接" />
          </>
        )}
      </div>
      {/* Lower panels: source/decoder selectors on the left, engine controls on the right. */}
      <div className="engine-body">
        <div className="engine-selectors">
          {PLUGIN_CATEGORIES.map(({ category, label, configKey }) => (
            <label key={category}>
              <span className="plugin-bar-label">{label}</span>
              <select
                disabled={running || starting}
                value={draft[configKey] ?? ""}
                onChange={(e) => setDraft((d) => ({ ...d, [configKey]: e.target.value }))}
              >
                {!draft[configKey] && <option value="">— 选择 —</option>}
                {(options[category] ?? []).map((p) => (
                  <option key={p.plugin_type} value={p.plugin_type}>
                    {p.display_name}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        <div className="actions engine-buttons">
          <button disabled={running || starting} onClick={() => void handleStart()}>
            {starting ? "启动中…" : "启动引擎"}
          </button>
          <button className="danger" disabled={!running || stop.pending} onClick={() => stop.run()}>
            停止引擎
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
  const connect = useAction(api.acqConnect, refreshAcquisitionStatus);
  const disconnect = useAction(api.acqDisconnect, refreshAcquisitionStatus);
  const recStart = useAction(api.recordingStart, refreshAcquisitionStatus); // Create a session, then start the stream.
  const recStop = useAction(api.recordingStop, refreshAcquisitionStatus); // Stop the stream, drain it, then end the session.
  const connected = acquisitionStatus?.connected ?? false;
  const streaming = acquisitionStatus?.streaming ?? false;
  const recordingActive = session?.state === "running";

  return (
    <section className="card card-stack">
      <h2>采集</h2>
      {acquisitionError ? (
        <p className="error">不可达</p>
      ) : (
        <>
          <p>
            <Dot on={acquisitionStatus?.connected} label="已连接" />
            <Dot on={acquisitionStatus?.streaming} label="读取中" />
          </p>
          <p className="meta">
            数据源 {acquisitionStatus?.source ?? "—"} ・ 采样率{" "}
            {acquisitionStatus?.sample_rate ?? "—"} Hz ・ 通道{" "}
            {acquisitionStatus?.channel_count ?? "—"}
          </p>
        </>
      )}
      {session ? (
        <p className="meta">
          会话 <code>{session.session_id}</code> ・ 范式 {session.paradigm ?? "—"} ・{" "}
          {session.state === "running" ? "进行中" : "已结束"} ・ 试次 {trialCount}
        </p>
      ) : (
        <p className="meta">无活动会话</p>
      )}
      <div className="actions actions-grouped">
        <div className="btn-group">
          <span className="btn-group-title">数据源连接</span>
          <button
            disabled={!engineRunning || connected || connect.pending}
            onClick={() => connect.run()}
          >
            连接
          </button>
          <button
            className="danger"
            disabled={!connected || disconnect.pending}
            onClick={() => disconnect.run()}
          >
            断开
          </button>
        </div>
        <div className="btn-group">
          <span className="btn-group-title">数据读取</span>
          <button
            disabled={!connected || streaming || recordingActive || recStart.pending}
            onClick={() => recStart.run()}
          >
            开始读取
          </button>
          <button
            className="danger"
            disabled={!recordingActive || recStop.pending}
            onClick={() => recStop.run()}
          >
            停止读取
          </button>
        </div>
      </div>
      <ActionError messages={[connect.error, disconnect.error, recStart.error, recStop.error]} />
    </section>
  );
}

function StimulationCard({ sessionActive }: { sessionActive: boolean }) {
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
      <h2>刺激</h2>
      <p>
        <Dot on={status?.process_alive}    label="进程运行" />
        <Dot on={status?.engine_connected} label="引擎已连接" />
      </p>
      {status?.paradigm && (
        <p className="meta">
          运行范式{" "}
          {paradigms.find((p) => p.plugin_type === status.paradigm)?.display_name ??
            status.paradigm}
        </p>
      )}
      <div className="actions">
        <select value={paradigm} onChange={(e) => setParadigm(e.target.value)}>
          <option value="">— 用配置值 —</option>
          {paradigms.map((p) => (
            <option key={p.plugin_type} value={p.plugin_type}>
              {p.display_name}
            </option>
          ))}
        </select>
        <button disabled={stimStart.pending} onClick={() => stimStart.run(paradigm || undefined)}>
          启动刺激
        </button>
        <button className="danger" disabled={stimStop.pending} onClick={() => stimStop.run()}>
          停止刺激
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
            <span className="meta">{String(desc?.label ?? name)}</span>
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
          title={sessionActive ? "" : "需要活动会话"}
          onClick={triggerTrial}
        >
          触发试次
        </button>
      </div>
      <ActionError messages={[stimStart.error, stimStop.error, trial.error]} />
    </section>
  );
}

function EffectorCard() {
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
          ? "已保存——停止→启动引擎后生效"
          : "已保存",
      );
    } catch (e) {
      setSaveError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card">
      <h2>控制输出</h2>
      <div className="actions">
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">— 选择控制输出 —</option>
          {effectors.map((e) => (
            <option key={e.plugin_type} value={e.plugin_type}>
              {e.display_name}
            </option>
          ))}
        </select>
        <button disabled={!selected || saving} onClick={() => void handleLoad()}>
          {saving ? "保存中…" : "加载"}
        </button>
      </div>
      {notice && <p className="meta">{notice}</p>}
      {saveError && <p className="error action-error">{saveError}</p>}
    </section>
  );
}

// Plugin selector categories embedded in the engine panel.
const PLUGIN_CATEGORIES = [
  { category: "acquisition", label: "数据源", configKey: "ACQUISITION.signal_source" as const },
  { category: "decoder",     label: "解码器", configKey: "ALGORITHM.decoder" as const },
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
  const steps = [
    { label: "选择数据源与解码器", done: configLoaded },
    { label: "引擎启动", done: engineRunning },
    { label: "数据源连接", done: connected },
    { label: "数据读取", done: streaming },
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

type Tab = "系统" | "配置";

export default function App() {
  const [tab, setTab] = useState<Tab>("系统");
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
          OmniBCI 控制台
        </h1>
        <nav>
          {(["系统", "配置"] as Tab[]).map((t) => (
            <button
              key={t}
              className={t === tab ? "nav-item active" : "nav-item"}
              onClick={() => setTab(t)}
            >
              {t === "系统" ? (
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
              {t}
              {t === "系统" && (
                <span className={`nav-dot ${engineRunning ? "dot-on" : "dot-off"}`}>●</span>
              )}
            </button>
          ))}
        </nav>
        <span className={`badge badge-${connection}`}>
          {connection === "open" ? "实时" : connection === "connecting" ? "连接中…" : "已断开，重连中…"}
        </span>
      </aside>

      <main className="content">
        <header className="content-head">
          <h1>{tab}</h1>
        </header>

        {tab === "配置" ? (
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
          <h2>试次结果</h2>
          {trials.length === 0 ? (
            <p className="meta">暂无结果</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>目标</th>
                  <th>事件</th>
                  <th>置信度</th>
                  <th>强制</th>
                  <th>时间 (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {[...trials].reverse().map((t) => (
                  <tr key={t.trial_number}>
                    <td>{t.trial_number}</td>
                    <td className="target">{t.target_id}</td>
                    <td>{t.event_id}</td>
                    <td>{t.confidence != null ? t.confidence.toFixed(2) : "—"}</td>
                    <td>{t.forced ? "是" : ""}</td>
                    <td className="meta">{t.produced_at.replace("T", " ").slice(0, 19)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card card-wide">
          <h2>刺激回执</h2>
          {acks.length === 0 ? (
            <p className="meta">暂无回执</p>
          ) : (
            <ul className="acks">
              {acks.map((a, i) => (
                <li key={i}>
                  <code>{a.command}</code>
                  {a.target_index != null && <> → 目标 {a.target_index}</>} ・ {a.status}
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
