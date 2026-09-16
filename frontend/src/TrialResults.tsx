import type { SessionInfo, Trial } from "./api/client";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";

export function SessionSummary({ session, trialCount }: { session: SessionInfo | null; trialCount: number }) {
  return <div className="session-summary" aria-label="Session summary" aria-live="polite">
    <div><span>Session</span><strong>{session?.session_id ?? "No session"}</strong></div>
    <div><span>State</span><strong>{session?.state ?? "Idle"}</strong></div>
    <div><span>Trials</span><strong>{trialCount}</strong></div>
  </div>;
}

export function TrialResults({ trials }: { trials: Trial[] }) {
  if (!trials.length) return <div className="empty-results"><Icon name="results" /><p>No trial results yet</p>
    <small>Decoded trials will appear here during a session.</small></div>;
  return <div className="table-scroll"><table aria-label="Trial results">
    <thead><tr><th>#</th><th>Target</th><th>Event</th><th>Confidence</th><th>Forced</th><th>Time (UTC)</th></tr></thead>
    <tbody>{[...trials].reverse().map((trial) => <tr key={trial.trial_number}>
      <td>{trial.trial_number}</td><td className="target">{trial.target_id}</td><td>{trial.event_id}</td>
      <td>{trial.confidence == null ? "—" : <span className="confidence"><span>{trial.confidence.toFixed(2)}</span>
        <span className="confidence-track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, trial.confidence)) * 100}%` }} /></span></span>}</td>
      <td>{trial.forced ? "Yes" : "—"}</td><td className="meta">{trial.produced_at.replace("T", " ").slice(0, 19)}</td>
    </tr>)}</tbody>
  </table></div>;
}
