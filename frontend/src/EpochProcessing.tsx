import { useEffect, useRef, useState } from "react";
import { api, type EpochList } from "./api/client";
import { useI18n } from "./i18n";
import { resultTime } from "./result-format";
import "./EpochProcessing.css";

const PAGE_SIZE = 25;

export function EpochProcessing({ onResults, disabled = false }: { onResults?: () => void; disabled?: boolean }) {
  const { t } = useI18n();
  const [offset, setOffset] = useState(0);
  const [snapshot, setSnapshot] = useState<EpochList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const generation = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const value = await api.epochs(offset, PAGE_SIZE);
        if (!alive) return;
        if (!value) throw new Error(t("epoch.listUnavailable"));
        const changed = generation.current !== undefined && value.generation !== generation.current;
        generation.current = value.generation;
        setSnapshot(value); setError(null);
        if (changed && offset !== 0) { setOffset(0); return; }
      } catch (cause) {
        if (alive) setError(cause instanceof Error ? cause.message : String(cause));
      }
      if (alive) timer = setTimeout(() => { void refresh(); }, 1000);
    };
    void refresh();
    return () => { alive = false; clearTimeout(timer); };
  }, [offset, retry, t]);

  const current = snapshot?.offset === offset ? snapshot : null;
  return <section className="card epoch-processing" aria-labelledby="epoch-processing-title">
    <h2 id="epoch-processing-title">{t("epoch.title")} {snapshot && <span className="count">{snapshot.total}</span>}
      {onResults && <button className="text-button" disabled={disabled} onClick={onResults}>{t("epoch.decodedResults")}</button>}</h2>
    <p className="meta">{t("epoch.listHelp")}</p>
    {current?.generation && <p className="meta epoch-generation">{t("results.generation", { generation: current.generation })}</p>}
    {error && <div className="epoch-toolbar"><p className="error" role="alert">{error}</p>
      <button type="button" onClick={() => setRetry((value) => value + 1)}>{t("epoch.retry")}</button></div>}
    {error && current && <p className="meta" role="status">{t("epoch.stale")}</p>}
    {!current ? !error && <p role="status">{t("epoch.loading")}</p> : <>
      <p className="meta">{t("epoch.range", { start: current.epochs.length ? offset + 1 : 0,
        end: offset + current.epochs.length, total: current.total })}</p>
      {!current.epochs.length ? <p role="status">{t("epoch.empty")}</p> : <div className="table-scroll" tabIndex={0} role="region" aria-label={t("epoch.title")}>
        <table aria-label={t("epoch.title")}><thead><tr><th scope="col">#</th><th scope="col">{t("epoch.status")}</th>
          <th scope="col">{t("epoch.reason")}</th><th scope="col">{t("epoch.samples")}</th><th scope="col">{t("results.event")}</th><th scope="col">{t("epoch.result")}</th>
          <th scope="col">{t("epoch.completedAt")}</th></tr></thead>
          <tbody>{current.epochs.map((epoch) => <tr key={`${epoch.generation}:${epoch.epoch_number}`}>
            <td>{epoch.epoch_number}</td><td>{t(`epoch.status.${epoch.status}`)}</td>
            <td>{t(`epoch.reason.${epoch.reason}`)}{epoch.error && <span className="error epoch-error">{epoch.error}</span>}</td><td>{epoch.sample_count}</td>
            <td>{epoch.event_id ?? "—"}</td>
            <td>{epoch.result ? <>{t("run.target", { index: epoch.result.target_id })}
              <span className="meta" title={t("results.confidence")}> · {epoch.result.confidence == null ? "—" : String(epoch.result.confidence)}</span></> : t("epoch.noResult")}</td>
            <td className="meta" title={epoch.completed_at}>{resultTime(epoch.completed_at)}</td>
          </tr>)}</tbody></table>
      </div>}
      {current.total > PAGE_SIZE && <nav className="epoch-pagination" aria-label={t("epoch.pages")}>
        <span>{t("epoch.page", { page: Math.floor(offset / PAGE_SIZE) + 1, pages: Math.ceil(current.total / PAGE_SIZE) })}</span>
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>{t("epoch.previous")}</button>
        <button type="button" disabled={offset + PAGE_SIZE >= current.total} onClick={() => setOffset(offset + PAGE_SIZE)}>{t("epoch.next")}</button>
      </nav>}
    </>}
  </section>;
}
