import type { ReactNode } from "react";
import { api } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { useI18n } from "./i18n";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";

export function BackendLogs({ navigation }: { navigation: ReactNode }) {
  const { t } = useI18n();
  const logs = usePoll(api.backendLogs, 5000);

  return <section className="settings-workspace" aria-label={t("app.logs")}>
    <aside className="explorer">
      {navigation}
      <div className="explorer-footer"><span className="eyebrow">{t("logs.backend")}</span>
        <p>{t("logs.description")}</p></div>
    </aside>
    <div className="settings-editor">
      <div className="breadcrumbs">{t("app.settings")} <Icon name="chevron" /> {t("app.logs")}</div>
      <header className="editor-heading"><div><p className="eyebrow">{t("logs.backend")}</p><h1>{t("app.logs")}</h1>
        <p>{t("logs.description")}</p></div>
        <button onClick={() => void logs.refresh()}><Icon name="refresh" />{t("logs.refresh")}</button></header>
      {logs.error && <p className="inline-message error" role="alert">{t("logs.unavailable")}</p>}
      {logs.data?.truncated && <p className="inline-message">{t("logs.truncated")}</p>}
      <div className="settings-scroll">
        {logs.data ? <>
          <p className="meta"><code>{logs.data.filename}</code></p>
          {logs.data.lines.length > 0
            ? <pre className="backend-log-output" tabIndex={0} role="region" aria-label={t("logs.output")}>{logs.data.lines.join("\n")}</pre>
            : <div className="empty-state"><Icon name="profile" /><h2>{t("logs.empty")}</h2></div>}
        </> : !logs.error && <div className="empty-state" role="status"><h2>{t("logs.loading")}</h2></div>}
      </div>
      <footer className="editor-footer"><span className="save-state">{t("logs.autoRefresh")}</span></footer>
    </div>
  </section>;
}
