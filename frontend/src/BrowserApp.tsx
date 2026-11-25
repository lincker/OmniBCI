import { useEffect, useRef, useState } from "react";
import App from "./App";
import { useI18n } from "./i18n";
import {
  acceptBrowserSession, AuthenticationRequired, closeBrowserSession, invalidateBrowserSession,
  onBrowserSessionLost, readBrowserSession, type ServerInfo,
} from "./api/browser-session";

/** A lost session keeps drafts mounted until the same Server is authenticated again. */
export default function BrowserApp() {
  const { language, setLanguage, t } = useI18n();
  const [info, setInfo] = useState<ServerInfo | null>(null);
  const [connected, setConnected] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const editor = useRef({ dirty: false });
  const alive = useRef(true);
  const updateEditor = (state: { dirty: boolean }) => { editor.current = state; };

  const accept = (next: ServerInfo) => {
    if (info && next.instance_id !== info.instance_id && editor.current.dirty
      && !window.confirm(t("connection.serverChanged"))) { setNeedsToken(false); return; }
    acceptBrowserSession(next);
    setInfo(next);
    setNeedsToken(false);
    setConnected(true);
    setError("");
  };
  const connect = async () => {
    setBusy(true); setError("");
    const credential = needsToken ? token : undefined;
    setToken("");
    try { const next = await readBrowserSession(credential); if (alive.current) accept(next); }
    catch (cause) {
      if (!alive.current) return;
      setNeedsToken(cause instanceof AuthenticationRequired);
      setError(cause instanceof AuthenticationRequired ? t("connection.invalidToken") : t("connection.unavailable"));
    } finally { if (alive.current) setBusy(false); }
  };

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    invalidateBrowserSession();
    const unsubscribe = onBrowserSessionLost(() => {
      setConnected(false); setError(t("connection.lost"));
      void readBrowserSession(undefined, controller.signal).then(() => {
        if (!controller.signal.aborted) setNeedsToken(false);
      }).catch((cause) => {
        if (!controller.signal.aborted) setNeedsToken(cause instanceof AuthenticationRequired);
      });
    });
    void readBrowserSession(undefined, controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      acceptBrowserSession(next); setInfo(next); setConnected(true);
    }).catch((cause) => {
      if (controller.signal.aborted) return;
      setNeedsToken(cause instanceof AuthenticationRequired);
      if (!(cause instanceof AuthenticationRequired)) setError(t("connection.unavailable"));
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { alive.current = false; controller.abort(); unsubscribe(); };
  }, []);

  const disconnect = async () => {
    if (editor.current.dirty && !window.confirm(t("app.discardQuestion"))) return;
    setBusy(true);
    try {
      await closeBrowserSession();
      setInfo(null); setConnected(false); setNeedsToken(info?.auth_enabled ?? false); setError("");
      editor.current = { dirty: false };
    } catch { setError(t("connection.unavailable")); }
    finally { setBusy(false); }
  };

  return <div className="desktop-shell">
    {connected && <div className="desktop-connection-bar" aria-label={t("desktop.connection")}>
      <strong>{t("desktop.existing")}</strong>
      <span className="desktop-server-address">{window.location.origin}</span>
      <span role="status">{t("app.connected")}</span>
      <button disabled={busy} onClick={() => void disconnect()}>{t("connection.disconnect")}</button>
    </div>}
    {connected && error && <p className="desktop-error error" role="alert">{error}</p>}
    {!connected && <main className="desktop-connect">
      <div className="desktop-connect-card">
        <p className="wordmark"><span className="brand-mark">Ω</span>OmniBCI</p>
        <label className="connection-field">{t("app.language")}
          <select aria-label={t("app.interfaceLanguage")} value={language} onChange={(event) => setLanguage(event.target.value as "en" | "zh")}>
            <option value="en">English</option><option value="zh">中文</option>
          </select>
        </label>
        <h1>{t("desktop.connectTitle")}</h1>
        <p>{t(info ? "connection.resume" : "connection.description")}</p>
        {error && <p role="alert" className="error">{error}</p>}
        <form onSubmit={(event) => { event.preventDefault(); if (!busy) void connect(); }}>
          {needsToken && <label className="connection-field">{t("desktop.token")}
            <input type="password" required disabled={busy} value={token}
              onChange={(event) => setToken(event.target.value)} autoComplete="off" />
          </label>}
          <button type="submit" className="primary" disabled={busy}>{t(busy ? "app.connecting" : "desktop.connect")}</button>
        </form>
      </div>
    </main>}
    {info && <div className="browser-workspace" hidden={!connected}>
      <App key={info.instance_id} connected={connected} onEditorStateChange={updateEditor} />
    </div>}
  </div>;
}
