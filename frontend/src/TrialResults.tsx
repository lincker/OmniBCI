import { useState } from "react";
import type { Trial } from "./api/client";
import { MAX_TRIALS } from "./hooks/useResultsFeed";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { useI18n } from "./i18n";
import { resultTime } from "./result-format";
import "./TrialResults.css";

export function TrialResults({ trials, generation, stale = false }: {
  trials: Trial[]; generation?: string | null; stale?: boolean;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [forced, setForced] = useState("all");
  const search = query.trim().toLowerCase();
  const filtered = trials.filter((trial) => (forced === "all" || trial.forced === (forced === "forced"))
    && (!search || [trial.trial_number, trial.target_id, trial.event_id, trial.config_revision]
      .some((value) => value != null && String(value).toLowerCase().includes(search))));
  const currentGeneration = generation ?? trials[0]?.generation;
  return <div className="trial-results">
    <p className="meta" id="results-scope">{t("results.scope", { limit: MAX_TRIALS })}</p>
    {currentGeneration && <p className="meta results-generation">{t("results.generation", { generation: currentGeneration })}</p>}
    {stale && <p className="notice" role="status">{t("results.stale")}</p>}
    <div className="results-toolbar">
      <label>{t("results.search")}<input type="search" aria-label={t("results.search")} value={query}
        placeholder={t("results.searchHint")} onChange={(event) => setQuery(event.target.value)} /></label>
      <label>{t("results.forced")}<select aria-label={t("results.forcedFilter")} value={forced}
        onChange={(event) => setForced(event.target.value)}>
        <option value="all">{t("results.all")}</option><option value="forced">{t("results.forcedOnly")}</option>
        <option value="unforced">{t("results.unforcedOnly")}</option>
      </select></label>
      {(query || forced !== "all") && <button type="button" onClick={() => { setQuery(""); setForced("all"); }}>{t("results.clearFilters")}</button>}
    </div>
    <p className="meta" role="status">{t("results.visibleCount", { visible: filtered.length, total: trials.length })}</p>
    {!trials.length ? <div className="empty-results"><Icon name="results" /><p>{t("results.noTrialResults")}</p>
      <small>{t("results.emptyHelp")}</small></div>
      : !filtered.length ? <p className="empty-results">{t("results.noMatch")}</p>
      : <div className="table-scroll" tabIndex={0} role="region" aria-label={t("results.trialResults")} aria-describedby="results-scope"><table aria-label={t("results.trialResults")}>
    <thead><tr><th scope="col">#</th><th scope="col">{t("results.target")}</th><th scope="col">{t("results.event")}</th><th scope="col">{t("results.confidence")}</th><th scope="col">{t("results.forced")}</th><th scope="col">{t("results.configuration")}</th><th scope="col">{t("results.timeUtc")}</th></tr></thead>
    <tbody>{[...filtered].reverse().map((trial) => <tr key={`${trial.generation}:${trial.trial_number}`}>
      <td>{trial.trial_number}</td><td className="target">{trial.target_id}</td><td>{trial.event_id}</td>
      <td>{trial.confidence == null ? <span title={t("results.notProvided")}>—</span> : <span className="confidence"><span>{String(trial.confidence)}</span>
        <span className="confidence-track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, trial.confidence)) * 100}%` }} /></span></span>}</td>
      <td>{trial.forced ? t("results.yes") : t("results.no")}</td><td className="meta" title={trial.config_revision ?? ""}>{trial.config_revision?.slice(0, 12) ?? "—"}</td>
      <td className="meta" title={trial.produced_at}>{resultTime(trial.produced_at)}</td>
    </tr>)}</tbody>
  </table></div>}
    <p className="meta">{t("results.precisionHelp")}</p>
  </div>;
}
