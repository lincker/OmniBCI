import { api } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { useResultsFeed } from "./hooks/useResultsFeed";
import "./styles.css";

function Dot({ on, label }: { on: boolean | undefined; label: string }) {
  return (
    <span className={`dot ${on ? "dot-on" : "dot-off"}`} title={label}>
      ● {label}
    </span>
  );
}

function EngineCard() {
  const { data, error } = usePoll(api.engineStatus);
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
    </section>
  );
}

function AcquisitionCard() {
  const { data, error } = usePoll(api.acquisitionStatus);
  return (
    <section className="card">
      <h2>采集</h2>
      {error ? (
        <p className="error">不可达</p>
      ) : (
        <>
          <p>
            <Dot on={data?.connected} label="已连接" />
            <Dot on={data?.streaming} label="流传输" />
          </p>
          <p className="meta">
            数据源 {data?.source ?? "—"} ・ 采样率 {data?.sample_rate ?? "—"} Hz ・ 通道{" "}
            {data?.channel_count ?? "—"}
          </p>
        </>
      )}
    </section>
  );
}

export default function App() {
  const { connection, session, trials, acks } = useResultsFeed();

  return (
    <main>
      <header>
        <h1>OmniBCI 控制台</h1>
        <span className={`badge badge-${connection}`}>
          {connection === "open" ? "实时" : connection === "connecting" ? "连接中…" : "已断开，重连中…"}
        </span>
      </header>

      <div className="grid">
        <EngineCard />
        <AcquisitionCard />

        <section className="card">
          <h2>会话</h2>
          {session ? (
            <>
              <p className="meta">
                <code>{session.session_id}</code>
              </p>
              <p>
                范式 {session.paradigm ?? "—"} ・ 状态{" "}
                {session.state === "running" ? "进行中" : "已结束"} ・ 试次 {trials.length}
              </p>
            </>
          ) : (
            <p className="meta">无活动会话（引擎待命中——经 POST /sessions 开启）</p>
          )}
        </section>

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
    </main>
  );
}
