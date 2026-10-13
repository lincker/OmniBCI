import { useEffect, useState } from "react";
import { api } from "./api/client";
import { requestApi } from "./api/transport";
import { usePoll } from "./hooks/usePoll";
import { currentConnection, desktopBridge } from "./desktop";
import { translate, useI18n, type Translate } from "./i18n";
import { translateSessionState } from "./TrialResults";

export function exportPath(format: "json" | "csv", sessionIds: string[]) {
  const query = new URLSearchParams({ format });
  sessionIds.forEach((id) => query.append("session_id", id));
  return `/api/v1/sessions/export?${query}`;
}

export async function saveSessionResults(format: "json" | "csv", sessionIds: string[], t: Translate = (key, params) => translate("en", key, params)) {
  const bridge = desktopBridge();
  if (bridge) {
    const connection = currentConnection();
    if (!connection?.id || connection.status !== "ready") throw new Error(t("results.backendUnavailable"));
    return bridge.saveResults({ connectionId: connection.id, format, sessionIds });
  }
  const blob = await requestApi(exportPath(format, sessionIds), {}, async (response) => {
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      throw new Error(error?.message ?? t("results.exportFailed", { status: response.status }));
    }
    return response.blob();
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `omnibci-results-${new Date().toISOString().replace(/[:.]/g, "-")}.${format}`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return { canceled: false };
}

export function ResultExport({ onBusyChange }: { onBusyChange: (busy: boolean) => void }) {
  const { t } = useI18n();
  const sessions = usePoll(api.sessions);
  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<"json" | "csv">("json");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState<"saved" | "download" | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { onBusyChange(pending); }, [pending, onBusyChange]);
  useEffect(() => () => { onBusyChange(false); }, [onBusyChange]);
  return <section className="card" aria-label={t("results.exportSessions")}>
    <h2>{t("results.exportTitle")}</h2>
    <p className="meta">{t("results.exportHelp")}</p>
    {!!sessions.data?.sessions.length && <div className="session-export-list">
      {sessions.data.sessions.map((session) => <label key={session.session_id} className="session-export-item">
        <input type="checkbox" disabled={pending} checked={selected.includes(session.session_id)} onChange={(event) => {
          setSelected((previous) => event.target.checked ? [...previous, session.session_id] : previous.filter((id) => id !== session.session_id));
          setNotice(null);
        }} />
        <span>{session.session_id} · {translateSessionState(session.state, t)} · {t("results.trialCount", { count: session.trial_count })}</span>
      </label>)}
    </div>}
    <div className="result-export-actions">
      <label>{t("results.format")} <select value={format} disabled={pending} onChange={(event) => { setFormat(event.target.value as "json" | "csv"); setNotice(null); }}>
        <option value="json">JSON</option><option value="csv">CSV</option>
      </select></label>
      <button disabled={pending || !sessions.data || !!sessions.error} onClick={() => {
        setPending(true); setNotice(null); setError("");
        void saveSessionResults(format, selected, t).then((result) => {
          if (!result.canceled) setNotice(desktopBridge() ? "saved" : "download");
        }).catch((cause) => setError(String(cause))).finally(() => setPending(false));
      }}>{pending ? t("results.saving") : selected.length ? t("results.exportSelected", { count: selected.length }) : t("results.exportAll")}</button>
      {selected.length > 0 && <button disabled={pending} onClick={() => setSelected([])}>{t("results.clearSelection")}</button>}
    </div>
    {(error || sessions.error) && <p className="error" role="alert">{error || sessions.error?.message}</p>}
    {notice && <p role="status">{t(notice === "saved" ? "results.saved" : "results.downloadRequested")}</p>}
  </section>;
}
