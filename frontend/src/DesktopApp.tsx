import { useEffect, useState } from "react";
import App from "./App";
import { useI18n } from "./i18n";
import { desktopBridge, setDesktopConnection } from "./desktop";
import type { ConnectionInfo } from "../../desktop/src/contracts";
import "./DesktopApp.css";

const disconnected: ConnectionInfo = { id: "", kind: null, status: "disconnected" };

function ConnectionPage({ connection, onError }: { connection: ConnectionInfo; onError: (message: string) => void }) {
  const { language, setLanguage, t } = useI18n();
  const [kind, setKind] = useState<"local" | "remote">(connection.kind ?? "local");
  const [url, setUrl] = useState(connection.kind === "remote" ? connection.url ?? "" : "");
  const [token, setToken] = useState("");
  const busy = connection.status === "connecting" || connection.status === "stopping";
  return <main className="desktop-connect">
    <div className="desktop-connect-card">
      <p className="wordmark"><span className="brand-mark">Ω</span>OmniBCI</p>
      <label className="connection-field">{t("app.language")}
        <select aria-label={t("app.interfaceLanguage")} value={language} onChange={(event) => setLanguage(event.target.value as "en" | "zh")}>
          <option value="en">English</option><option value="zh">中文</option>
        </select>
      </label>
      <h1>{t("desktop.connectTitle")}</h1>
      <p>{t("desktop.connectDescription")}</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        if (busy) return;
        onError("");
        const target = kind === "local" ? { kind } as const : { kind, url: url.trim(), token } as const;
        setToken("");
        void desktopBridge()!.connect(target).catch((cause) => onError(String(cause)));
      }}>
        <fieldset disabled={busy} className="connection-options"><legend>{t("desktop.location")}</legend>
          <label><input type="radio" name="mode" checked={kind === "local"} onChange={() => setKind("local")} /> {t("desktop.local")}</label>
          <label><input type="radio" name="mode" checked={kind === "remote"} onChange={() => setKind("remote")} /> {t("desktop.remote")}</label>
        </fieldset>
        {kind === "remote" ? <>
          <label className="connection-field">{t("desktop.address")}<input type="url" required disabled={busy} placeholder="https://lab.example:8000"
            value={url} onChange={(event) => setUrl(event.target.value)} autoComplete="off" /></label>
          <label className="connection-field">{t("desktop.token")}<input type="password" required disabled={busy} value={token}
            onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
          <p className="meta">{t("desktop.tokenHelp")}</p>
          {url.trim().toLowerCase().startsWith("http:") && <p className="inline-message">{t("desktop.httpWarning")}</p>}
        </> : <p className="meta">{t("desktop.localHelp")}</p>}
        <button className="primary" type="submit" disabled={busy}>{busy ? t("app.connecting") : t("desktop.connect")}</button>
      </form>
      <button className="text-button" onClick={() => void desktopBridge()!.openLogs().catch((cause) => onError(String(cause)))}>{t("desktop.openLogs")}</button>
    </div>
  </main>;
}

/** A backend change creates a new React tree and discards all prior runtime state. */
export default function DesktopApp() {
  const { t } = useI18n();
  const bridge = desktopBridge();
  const [connection, setConnection] = useState<ConnectionInfo | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!bridge) return;
    let alive = true;
    let observedChange = false;
    const update = (next: ConnectionInfo) => {
      if (!alive) return;
      setDesktopConnection(next);
      setConnection(next);
      setError("");
    };
    const unsubscribe = bridge.onConnectionChanged((next) => { observedChange = true; update(next); });
    void bridge.getConnection().then((next) => { if (!observedChange) update(next); }).catch((cause) => {
      if (alive) { setConnection(disconnected); setError(String(cause)); }
    });
    return () => { alive = false; unsubscribe(); };
  }, [bridge]);
  if (!bridge) return <App />;
  if (!connection) return <main className="desktop-connect" role="status">{t("desktop.opening")}</main>;
  const workspace = !!connection.id && !!connection.kind && connection.status !== "disconnected" && connection.status !== "connecting";
  return <div className="desktop-shell">
    {workspace && <div className="desktop-connection-bar" aria-label={t("desktop.connection")}>
      <strong>{connection.kind === "local" ? t("desktop.local") : t("desktop.remote")}</strong>
      <span className="desktop-backend-address" title={connection.url}>{connection.url}</span>
      {connection.kind === "remote" && connection.url?.startsWith("http:") && <span>{t("desktop.http")}</span>}
      <span role="status">{connection.status === "ready" ? t("app.connected") : connection.status === "stopping" ? t("desktop.stopping") : t("desktop.lost")}</span>
      <button disabled={connection.status === "stopping"} onClick={() => {
        setError("");
        void bridge.disconnect().catch((cause) => setError(String(cause)));
      }}>{t("desktop.switch")}</button>
      <button onClick={() => void bridge.openLogs().catch((cause) => setError(String(cause)))}>{t("desktop.logs")}</button>
    </div>}
    {(error || connection.error) && <p className="desktop-error error" role="alert">{error || connection.error}</p>}
    {workspace ? <App key={connection.id} desktopConnection={connection} />
      : <ConnectionPage key={`${connection.kind}-${connection.url ?? ""}`} connection={connection} onError={setError} />}
  </div>;
}
