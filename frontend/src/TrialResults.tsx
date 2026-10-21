import type { Trial } from "./api/client";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { useI18n } from "./i18n";

export function TrialResults({ trials }: { trials: Trial[] }) {
  const { t } = useI18n();
  if (!trials.length) return <div className="empty-results"><Icon name="results" /><p>{t("results.noTrialResults")}</p>
    <small>{t("results.emptyHelp")}</small></div>;
  return <div className="table-scroll"><table aria-label={t("results.trialResults")}>
    <thead><tr><th>#</th><th>{t("results.target")}</th><th>{t("results.event")}</th><th>{t("results.confidence")}</th><th>{t("results.forced")}</th><th>{t("results.configuration")}</th><th>{t("results.timeUtc")}</th></tr></thead>
    <tbody>{[...trials].reverse().map((trial) => <tr key={`${trial.generation}:${trial.trial_number}`}>
      <td>{trial.trial_number}</td><td className="target">{trial.target_id}</td><td>{trial.event_id}</td>
      <td>{trial.confidence == null ? "—" : <span className="confidence"><span>{trial.confidence.toFixed(2)}</span>
        <span className="confidence-track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, trial.confidence)) * 100}%` }} /></span></span>}</td>
      <td>{trial.forced ? t("results.yes") : "—"}</td><td className="meta" title={trial.config_revision ?? ""}>{trial.config_revision?.slice(0, 12) ?? "—"}</td>
      <td className="meta">{trial.produced_at.replace("T", " ").slice(0, 19)}</td>
    </tr>)}</tbody>
  </table></div>;
}
