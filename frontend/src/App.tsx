import { useState, useEffect } from "react";
import { api, ApiError, type ParadigmInfo } from "./api/client";
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

function EngineCard({ configLoaded }: { configLoaded: boolean }) {
  const { data, error, refresh } = usePoll(api.engineStatus);
  const start = useAction(api.engineStart, refresh);
  const stop = useAction(api.engineStop, refresh);
  const running = data?.running ?? false;

  return (
    <section className="card">
      <h2>引擎</h2>
      {error ? (
        <p className="error">后端不可达：{error.message}</p>
      ) : (
        <p>
          <Dot on={data?.running} label="运行中" />
          <Dot on={data?.acquisition_alive} label="采集进程" />
          <Dot on={data?.processor_alive} label="处理进程" />
        </p>
      )}
      <div className="actions">
        <button disabled={!configLoaded || running || start.pending} onClick={() => start.run()}>
          启动引擎
        </button>
        <button className="danger" disabled={!running || stop.pending} onClick={() => stop.run()}>
          停止引擎
        </button>
      </div>
      <ActionError messages={[start.error, stop.error]} />
    </section>
  );
}

function AcquisitionSessionCard({
  engineRunning,
  session,
  trialCount,
}: {
  engineRunning: boolean;
  session: { session_id: string; paradigm?: string | null; state: string } | null;
  trialCount: number;
}) {
  const { data, error, refresh } = usePoll(api.acquisitionStatus);
  const connect = useAction(api.acqConnect, refresh);
  const disconnect = useAction(api.acqDisconnect, refresh);
  const recStart = useAction(api.recordingStart, refresh); // 开始读取(建会话→开流)
  const recStop = useAction(api.recordingStop, refresh); // 停止读取(停流→排水→结束会话)
  const connected = data?.connected ?? false;
  const streaming = data?.streaming ?? false;

  return (
    <section className="card">
      <h2>采集与会话</h2>
      {error ? (
        <p className="error">不可达</p>
      ) : (
        <>
          <p>
            <Dot on={data?.connected} label="已连接" />
            <Dot on={data?.streaming} label="读取中" />
          </p>
          <p className="meta">
            数据源 {data?.source ?? "—"} ・ 采样率 {data?.sample_rate ?? "—"} Hz ・ 通道{" "}
            {data?.channel_count ?? "—"}
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
      <div className="actions">
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
        <button
          disabled={!connected || streaming || recStart.pending}
          onClick={() => recStart.run()}
        >
          开始读取
        </button>
        <button className="danger" disabled={!streaming || recStop.pending} onClick={() => recStop.run()}>
          停止读取
        </button>
      </div>
      <ActionError messages={[connect.error, disconnect.error, recStart.error, recStop.error]} />
    </section>
  );
}

function StimulationCard({ sessionActive }: { sessionActive: boolean }) {
  const [target, setTarget] = useState<string>("");
  const [paradigm, setParadigm] = useState<string>("");
  const [paradigms, setParadigms] = useState<ParadigmInfo[]>([]);
  const stimStart = useAction(api.stimStart);
  const stimStop = useAction(api.stimStop);
  const trial = useAction(api.trialStart);

  useEffect(() => {
    api.listPlugins("paradigm").then((r) => {
      if (r) setParadigms(r.paradigms);
    }).catch(() => {});
  }, []);

  return (
    <section className="card">
      <h2>刺激</h2>
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
      <div className="actions">
        <input
          type="number"
          min={0}
          placeholder="目标序号(可空)"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        />
        <button
          disabled={!sessionActive || trial.pending}
          title={sessionActive ? "" : "需要活动会话"}
          onClick={() => trial.run(target === "" ? undefined : Number(target))}
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
      await api.patchConfig({ "NEUROENGINE.effector": selected });
      setNotice("已保存");
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

// ---- 插件选择栏 ----
const PLUGIN_CATEGORIES = [
  { category: "acquisition", label: "数据源", configKey: "ACQUISITION.signal_source" as const },
  { category: "decoder",     label: "解码器", configKey: "ALGORITHM.decoder" as const },
] as const;

function PluginSelectorBar({ onLoaded, engineRunning }: { onLoaded?: () => void; engineRunning: boolean }) {
  const [options, setOptions] = useState<Record<string, ParadigmInfo[]>>({});
  const [values, setValues] = useState<Record<string, string>>({});  // 已保存生效值
  const [draft, setDraft] = useState<Record<string, string>>({});    // 本地暂选
  const [loading, setLoading] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      const [cfg, ...pluginResps] = await Promise.all([
        api.getConfig().catch(() => null),
        ...PLUGIN_CATEGORIES.map(({ category }) =>
          api.listPlugins(category).catch(() => null)
        ),
      ]);
      const vals: Record<string, string> = {};
      if (cfg) {
        for (const { configKey } of PLUGIN_CATEGORIES) {
          if (!configKey) continue;
          const entry = cfg.entries.find((e) => e.key === configKey);
          if (entry?.effective_value != null) vals[configKey] = String(entry.effective_value);
        }
      }
      setValues(vals);
      setDraft(vals);  // draft 初始与 values 一致
      const opts: Record<string, ParadigmInfo[]> = {};
      PLUGIN_CATEGORIES.forEach(({ category }, i) => {
        const resp = pluginResps[i];
        if (resp) opts[category] = resp.paradigms;
      });
      setOptions(opts);
    }
    void load();
  }, []);

  const handleLoad = async () => {
    const updates: Record<string, string> = {};
    for (const { configKey } of PLUGIN_CATEGORIES) {
      if (!configKey) continue;
      const v = draft[configKey];
      if (v && v !== values[configKey]) updates[configKey] = v;
    }
    setLoading(true);
    setSaveError(null);
    setNotice(null);
    try {
      if (Object.keys(updates).length > 0) {
        const result = await api.patchConfig(updates);
        setValues((v) => ({ ...v, ...updates }));
        setNotice(result?.engine_restart_required ? "已保存 — 重启引擎后生效" : "已加载");
      } else {
        setNotice("已加载");
      }
      onLoaded?.();
    } catch (e) {
      setSaveError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="plugin-bar">
      {PLUGIN_CATEGORIES.map(({ category, label, configKey }) => {
        const isDirty = !!configKey && draft[configKey] !== values[configKey];
        return (
          <label key={category} className="plugin-bar-item">
            <span className="plugin-bar-label">{label}</span>
            <select
              disabled={!configKey || loading}
              className={isDirty ? "dirty" : ""}
              value={configKey ? (draft[configKey] ?? "") : ""}
              onChange={(e) =>
                configKey &&
                setDraft((d) => ({ ...d, [configKey]: e.target.value }))
              }
            >
              {configKey && !draft[configKey] && <option value="">— 选择 —</option>}
              {!configKey && <option value="">— 暂不配置 —</option>}
              {(options[category] ?? []).map((p) => (
                <option key={p.plugin_type} value={p.plugin_type}>
                  {p.display_name}
                </option>
              ))}
            </select>
          </label>
        );
      })}
      <button
        className="plugin-bar-load"
        disabled={engineRunning || loading}
        onClick={() => void handleLoad()}
      >
        {loading ? "加载中…" : "加载"}
      </button>
      {notice && <span className="plugin-bar-notice">{notice}</span>}
      {saveError && <span className="plugin-bar-error">{saveError}</span>}
    </div>
  );
}

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
    { label: "配置加载", done: configLoaded },
    { label: "引擎启动", done: engineRunning },
    { label: "数据源连接", done: connected },
    { label: "数据读取", done: streaming },
  ];
  // 第一个未完成的步骤是"当前步"
  const currentIdx = steps.findIndex((s) => !s.done);

  return (
    <div className="pipeline">
      {steps.map((s, i) => (
        <div key={s.label} className="step-wrap">
          {i > 0 && <span className={`step-line ${steps[i - 1].done ? "done" : ""}`} />}
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
  const { connection, session, trials, acks } = useResultsFeed();
  const { data: engineStatus } = usePoll(api.engineStatus);
  const { data: acqStatus } = usePoll(api.acquisitionStatus);
  const engineRunning = engineStatus?.running ?? false;
  // 引擎已在运行说明配置必然已加载过（页面刷新场景）
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
      <PluginSelectorBar onLoaded={() => setConfigLoaded(true)} engineRunning={engineRunning} />
      <PipelineStepper
        configLoaded={effectiveConfigLoaded}
        engineRunning={engineRunning}
        connected={acqStatus?.connected ?? false}
        streaming={acqStatus?.streaming ?? false}
      />
      <div className="grid">
        <EngineCard configLoaded={effectiveConfigLoaded} />
        <AcquisitionSessionCard
          engineRunning={engineRunning}
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
