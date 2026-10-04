import { useEffect, useState } from "react";
import { api } from "./api/client";
import { requestApi } from "./api/transport";
import { usePoll } from "./hooks/usePoll";
import { currentConnection, desktopBridge } from "./desktop";

export function exportPath(format: "json" | "csv", sessionIds: string[]) {
  const query = new URLSearchParams({ format });
  sessionIds.forEach((id) => query.append("session_id", id));
  return `/api/v1/sessions/export?${query}`;
}

export async function saveSessionResults(format: "json" | "csv", sessionIds: string[]) {
  const bridge = desktopBridge();
  if (bridge) {
    const connection = currentConnection();
    if (!connection?.id || connection.status !== "ready") throw new Error("The backend connection is unavailable.");
    return bridge.saveResults({ connectionId: connection.id, format, sessionIds });
  }
  const blob = await requestApi(exportPath(format, sessionIds), {}, async (response) => {
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      throw new Error(error?.message ?? `Export failed (${response.status}).`);
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
  const sessions = usePoll(api.sessions);
  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<"json" | "csv">("json");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { onBusyChange(pending); }, [pending, onBusyChange]);
  useEffect(() => () => { onBusyChange(false); }, [onBusyChange]);
  return <section className="card" aria-label="Export session results">
    <h2>Export results</h2>
    <p className="meta">Choose sessions, or leave all unchecked to export every session on this backend. Running sessions export a snapshot, not final results. Raw EEG is not included.</p>
    {!!sessions.data?.sessions.length && <div className="session-export-list">
      {sessions.data.sessions.map((session) => <label key={session.session_id} className="session-export-item">
        <input type="checkbox" disabled={pending} checked={selected.includes(session.session_id)} onChange={(event) => {
          setSelected((previous) => event.target.checked ? [...previous, session.session_id] : previous.filter((id) => id !== session.session_id));
          setNotice("");
        }} />
        <span>{session.session_id} · {session.state} · {session.trial_count} trials</span>
      </label>)}
    </div>}
    <div className="result-export-actions">
      <label>Format <select value={format} disabled={pending} onChange={(event) => { setFormat(event.target.value as "json" | "csv"); setNotice(""); }}>
        <option value="json">JSON</option><option value="csv">CSV</option>
      </select></label>
      <button disabled={pending || !sessions.data || !!sessions.error} onClick={() => {
        setPending(true); setNotice(""); setError("");
        void saveSessionResults(format, selected).then((result) => {
          if (!result.canceled) setNotice(desktopBridge() ? "Results saved." : "Download requested. Confirm the file in your browser downloads.");
        }).catch((cause) => setError(String(cause))).finally(() => setPending(false));
      }}>{pending ? "Saving…" : selected.length ? `Export ${selected.length} selected` : "Export all sessions"}</button>
      {selected.length > 0 && <button disabled={pending} onClick={() => setSelected([])}>Clear selection</button>}
    </div>
    {(error || sessions.error) && <p className="error" role="alert">{error || sessions.error?.message}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
