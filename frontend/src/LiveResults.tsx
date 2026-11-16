/** Compact, always-visible decoding output beside the Run pipeline. */
import type { ResultsFeed } from "./hooks/useResultsFeed";
import { useI18n } from "./i18n";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import "./LiveResults.css";

const RECENT = 10;

export function LiveResults({ feed, onResults }: { feed: ResultsFeed; onResults: () => void }) {
  const { t } = useI18n();
  const latest = feed.trials[feed.trials.length - 1];
  const recent = feed.trials.slice(-RECENT).reverse();
  const connection = feed.connection ?? "connecting";
  return <aside className="live-results" aria-label={t("live.title")}>
    <header className="live-results-heading">
      <h2>{t("live.title")}</h2>
      <span className={`live-connection live-${connection}`}><span className="status-dot" />{t(`live.connection.${connection}`)}</span>
    </header>
    {feed.error && <p className="live-error" role="alert">{feed.error}</p>}
    <div className="live-latest" aria-live="polite">
      {latest ? <>
        <span className="live-label">{t("live.latest")}</span>
        <strong className="live-target">{latest.target_id}</strong>
        <span className="live-meta">{t("live.trial", { number: latest.trial_number })}
          {latest.confidence != null && <> · {t("live.confidence", { value: latest.confidence.toFixed(2) })}</>}
          {latest.forced && <> · <em>{t("live.forced")}</em></>}</span>
      </> : <>
        <Icon name="results" />
        <strong>{t("live.none")}</strong>
        <span className="live-meta">{t("live.noneHelp")}</span>
      </>}
    </div>
    {recent.length > 1 && <section className="live-recent" aria-label={t("live.recent")}>
      <h3>{t("live.recent")}</h3>
      <ol>{recent.map((trial) => <li key={`${trial.generation}:${trial.trial_number}`}>
        <span className="live-recent-number">#{trial.trial_number}</span>
        <strong>{trial.target_id}</strong>
        <span className="live-recent-confidence">{trial.confidence == null ? "—" : trial.confidence.toFixed(2)}</span>
        {trial.forced && <span className="live-recent-forced" title={t("live.forced")}>F</span>}
      </li>)}</ol>
    </section>}
    <button type="button" className="text-button live-open" onClick={onResults}>
      {t("live.openResults")} <span className="count">{feed.trials.length}</span> <Icon name="chevron" />
    </button>
  </aside>;
}
