import { useEffect, useState } from "react";
import App from "./App";
import { desktopBridge, setDesktopConnection } from "./desktop";
import type { ConnectionInfo } from "../../desktop/src/contracts";
import "./DesktopApp.css";

const disconnected: ConnectionInfo = { id: "", kind: null, status: "disconnected" };

function ConnectionPage({ connection, onError }: { connection: ConnectionInfo; onError: (message: string) => void }) {
  const [kind, setKind] = useState<"local" | "remote">(connection.kind ?? "local");
  const [url, setUrl] = useState(connection.kind === "remote" ? connection.url ?? "" : "");
  const [token, setToken] = useState("");
  const busy = connection.status === "connecting" || connection.status === "stopping";
  return <main className="desktop-connect">
    <div className="desktop-connect-card">
      <p className="wordmark"><span className="brand-mark">Ω</span>OmniBCI</p>
      <h1>Connect your workspace</h1>
      <p>Run on this computer or connect to a backend in your laboratory.</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        if (busy) return;
        onError("");
        const target = kind === "local" ? { kind } as const : { kind, url: url.trim(), token } as const;
        setToken("");
        void desktopBridge()!.connect(target).catch((cause) => onError(String(cause)));
      }}>
        <fieldset disabled={busy} className="connection-options"><legend>Run location</legend>
          <label><input type="radio" name="mode" checked={kind === "local"} onChange={() => setKind("local")} /> This computer</label>
          <label><input type="radio" name="mode" checked={kind === "remote"} onChange={() => setKind("remote")} /> Remote backend</label>
        </fieldset>
        {kind === "remote" ? <>
          <label className="connection-field">Backend address<input type="url" required disabled={busy} placeholder="https://lab.example:8000"
            value={url} onChange={(event) => setUrl(event.target.value)} autoComplete="off" /></label>
          <label className="connection-field">Access token<input type="password" required disabled={busy} value={token}
            onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
          <p className="meta">The token is kept in memory only. Files, plugins, and configuration belong to the remote computer.</p>
          {url.trim().toLowerCase().startsWith("http:") && <p className="inline-message">HTTP is unencrypted. Use only on a trusted laboratory network or VPN.</p>}
        </> : <p className="meta">OmniBCI includes its own runtime. Connecting will not start acquisition or stimulation.</p>}
        <button className="primary" type="submit" disabled={busy}>{busy ? "Connecting…" : "Connect"}</button>
      </form>
      <button className="text-button" onClick={() => void desktopBridge()!.openLogs().catch((cause) => onError(String(cause)))}>Open logs</button>
    </div>
  </main>;
}

/** A backend change creates a new React tree and discards all prior runtime state. */
export default function DesktopApp() {
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
  if (!connection) return <main className="desktop-connect" role="status">Opening OmniBCI…</main>;
  const workspace = !!connection.id && !!connection.kind && connection.status !== "disconnected" && connection.status !== "connecting";
  return <div className="desktop-shell">
    {workspace && <div className="desktop-connection-bar" aria-label="Backend connection">
      <strong>{connection.kind === "local" ? "This computer" : "Remote backend"}</strong>
      <span className="desktop-backend-address" title={connection.url}>{connection.url}</span>
      {connection.kind === "remote" && connection.url?.startsWith("http:") && <span>Unencrypted HTTP</span>}
      <span role="status">{connection.status === "ready" ? "Connected" : connection.status === "stopping" ? "Stopping…" : "Connection lost · state unknown"}</span>
      <button disabled={connection.status === "stopping"} onClick={() => {
        setError("");
        void bridge.disconnect().catch((cause) => setError(String(cause)));
      }}>Switch connection</button>
      <button onClick={() => void bridge.openLogs().catch((cause) => setError(String(cause)))}>Logs</button>
    </div>}
    {(error || connection.error) && <p className="desktop-error error" role="alert">{error || connection.error}</p>}
    {workspace ? <App key={connection.id} desktopConnection={connection} />
      : <ConnectionPage key={`${connection.kind}-${connection.url ?? ""}`} connection={connection} onError={setError} />}
  </div>;
}
