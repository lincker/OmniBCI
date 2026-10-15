import { useState, useEffect, useCallback } from "react";
import { api } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import { useResultsFeed } from "./hooks/useResultsFeed";
import { useRuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { RuntimeConfigurationStatus, RuntimeConfigurationReview } from "./RuntimeConfiguration";
import { ConfigCard } from "./ConfigCard";
import { PluginProfiles } from "./PluginProfiles";
import { PluginMarket } from "./PluginMarket";
import { ConfigDialog } from "./ConfigDialog";
import { RunConsole } from "./RunConsole";
import { SessionSummary, TrialResults } from "./TrialResults";
import { ResultExport } from "./ResultExport";
import { desktopBridge } from "./desktop";
import type { ConnectionInfo } from "../../desktop/src/contracts";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { useI18n } from "./i18n";
import "./styles.css";

const setupPages = [
  ["acquisition", "app.acquisition", "signal"], ["processing", "app.processing", "workflow"],
  ["stimulation", "app.stimulation", "system"], ["actuation", "app.actuation", "plug"],
] as const;
const settingsPages = [["general", "app.general", "settings"]] as const;
type SetupPage = typeof setupPages[number][0];
type SettingsPage = typeof settingsPages[number][0];
type View = SetupPage | SettingsPage | "run" | "results" | "extensions";
type Appearance = "system" | "light" | "dark";

function initialView(): View {
  const hash = window.location.hash.slice(1);
  if ([...setupPages, ...settingsPages].some(([page]) => page === hash) || hash === "run" || hash === "results" || hash === "extensions") return hash as View;
  if (["engine-panel", "acquisition-panel", "stimulation-panel", "acks-panel"].includes(hash)) return "run";
  if (hash === "channels") return "processing";
  if (hash === "results-panel") return "results";
  return "acquisition";
}

export default function App({ desktopConnection }: { desktopConnection?: ConnectionInfo } = {}) {
  const { language, setLanguage, t } = useI18n();
  const [view, setView] = useState<View>(initialView);
  const [lastSetup, setLastSetup] = useState<SetupPage>("acquisition");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pendingView, setPendingView] = useState<View | null>(null);
  const [appearance, setAppearance] = useState<Appearance>(() => {
    try { const value = localStorage.getItem("omnibci-appearance"); return value === "light" || value === "dark" ? value : "system"; }
    catch { return "system"; }
  });
  const feed = useResultsFeed();
  const engine = usePoll(api.engineStatus);
  const acquisition = usePoll(api.acquisitionStatus);
  const runtime = useRuntimeConfiguration();
  const engineRunning = engine.data?.running ?? false;
  const isSetup = setupPages.some(([page]) => page === view);
  const isSettings = view === "general";
  const space = isSetup ? "app.setup" : isSettings ? "app.settings" : view === "run" ? "app.run" : view === "extensions" ? "app.extensions" : "app.results";

  useEffect(() => {
    document.documentElement.style.colorScheme = appearance === "system" ? "light dark" : appearance;
    try { localStorage.setItem("omnibci-appearance", appearance); } catch { /* Browser storage may be disabled. */ }
  }, [appearance]);
  useEffect(() => {
    window.history.replaceState(null, "", `#${view}`);
    if (setupPages.some(([page]) => page === view)) setLastSetup(view as SetupPage);
  }, [view]);
  useEffect(() => {
    if (desktopBridge() || (!dirty && !busy)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, busy]);

  useEffect(() => {
    desktopBridge()?.setEditorState({ dirty, busy: busy || runtime.inProgress });
  }, [dirty, busy, runtime.inProgress]);

  const navigate = useCallback((next: View) => {
    if (next === view || busy) return;
    if (dirty) { setPendingView(next); return; }
    setDirty(false); setView(next);
  }, [view, busy, dirty]);
  useEffect(() => {
    const onHashChange = () => {
      const next = initialView();
      window.history.replaceState(null, "", `#${view}`);
      navigate(next);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [view, navigate]);
  const navigation = <>
    <div className="explorer-heading">{isSetup ? t("app.setup") : t("app.preferences")}</div>
    <nav className="workspace-navigation" aria-label={isSetup ? t("app.setupModules") : t("app.settingsPages")}>
      {(isSetup ? setupPages : settingsPages).map(([page, label, icon]) => <button key={page}
        className={`explorer-item ${view === page ? "selected" : ""}`} aria-current={view === page ? "page" : undefined}
        disabled={busy} onClick={() => navigate(page)}><Icon name={icon} /><span>{t(label)}</span></button>)}
    </nav>
  </>;
  const preferences = <div className="setting-group"><h2>{t("app.interface")}</h2>
    <label className="setting-row"><span className="setting-label">{t("app.language")}</span>
      <select aria-label={t("app.interfaceLanguage")} value={language} onChange={(e) => setLanguage(e.target.value as "en" | "zh")}>
        <option value="en">English</option><option value="zh">中文</option>
      </select></label>
    <label className="setting-row"><span className="setting-label">{t("app.appearance")}</span>
      <select aria-label={t("app.appearance")} value={appearance} onChange={(e) => setAppearance(e.target.value as Appearance)}>
        <option value="system">{t("app.system")}</option><option value="light">{t("app.light")}</option><option value="dark">{t("app.dark")}</option>
      </select></label>
  </div>;

  return <div className="workbench" onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); navigate("general"); }
  }}>
    <header className="title-bar">
      <span className="wordmark"><span className="brand-mark">Ω</span>OmniBCI</span>
      <div className="workspace-title"><Icon name="folder" /><span>{t("app.console")}</span><span className="title-separator">/</span><span>{t(space)}</span></div>
      <span className="title-caption">{t("app.workspace")}</span>
    </header>
    <div className="workbench-body">
      <nav className="activity-bar" aria-label={t("app.workspaces")}>
        {([[lastSetup, "app.setup", "settings"], ["run", "app.run", "play"], ["results", "app.results", "results"], ["extensions", "app.extensions", "plug"]] as const).map(([page, label, icon]) =>
          <button key={page} className={`activity-item ${space === label ? "selected" : ""}`} aria-label={t(label)} title={t(label)}
            aria-current={space === label ? "page" : undefined} disabled={busy} onClick={() => navigate(page)}><Icon name={icon} /></button>)}
        <div className="activity-bottom"><button className={`activity-item ${isSettings ? "selected" : ""}`}
          aria-label={t("app.settings")} title={t("app.settings")} aria-current={isSettings ? "page" : undefined} disabled={busy}
          onClick={() => navigate("general")}><Icon name="gear" /></button></div>
      </nav>
      <main className="editor-workspace">
        <RuntimeConfigurationStatus runtime={runtime} />
        <fieldset className="desktop-control-guard" disabled={!!desktopConnection && desktopConnection.status !== "ready"}>
        <div className="editor-viewport">
          {isSetup ? <PluginProfiles key={view} module={view as SetupPage} engineRunning={engineRunning} engineAvailable={engine.data !== null && engine.error === null} navigation={navigation}
            runtime={runtime} acquisitionStatus={acquisition.error ? null : acquisition.data} onAcquisitionChange={acquisition.refresh}
            onDirtyChange={setDirty} onBusyChange={setBusy} />
            : view === "extensions" ? <PluginMarket runtimeBusy={runtime.blocked}
              onBusyChange={setBusy} onDirtyChange={setDirty} onOpenSetup={() => navigate(lastSetup)} />
            : view === "run" ? <RunConsole engine={engine} acquisition={acquisition} feed={feed}
              runtime={runtime} onBusyChange={setBusy} onEditSetup={() => navigate(lastSetup)} onResults={() => navigate("results")} />
            : view === "results" ? <div className="runtime-content">
              <div className="breadcrumbs">{t("app.results")} <Icon name="chevron" /> {t("app.currentSession")}</div>
              <header className="editor-heading"><div><p className="eyebrow">{t("app.sessionData")}</p><h1>{t("app.trialResults")}</h1>
                <p>{t("app.resultsDescription")}</p></div>
                <button onClick={() => navigate("run")}><Icon name="play" />{t("app.openRun")}</button></header>
              <ResultExport onBusyChange={setBusy} />
              <SessionSummary session={feed.session} trialCount={feed.trials.length} />
              <section className="card"><h2>{t("app.results")} <span className="count">{feed.trials.length}</span></h2><TrialResults trials={feed.trials} /></section>
            </div>
            : <ConfigCard navigation={navigation}
              preferences={preferences} onDirtyChange={setDirty} onBusyChange={setBusy}
              runtime={runtime} />}
        </div>
        </fieldset>
      </main>
    </div>
    <footer className="status-bar" aria-label={t("app.consoleStatus")}>
      <span className={`connection-status connection-${feed.connection}`}><span className="status-dot" />
        {feed.connection === "open" ? t("app.connected") : feed.connection === "connecting" ? t("app.connecting") : t("app.disconnected")}</span>
      <span>{engine.error ? t("app.engineUnavailable") : !engine.data ? t("app.checkingEngine") : engineRunning ? t("app.engineRunning") : t("app.engineStopped")}</span>
      <span className="status-spacer" />
      {acquisition.data?.sample_rate != null && <span>{acquisition.data.sample_rate} Hz</span>}
      {acquisition.data?.channel_count != null && <span className="status-source">{t("app.channels", { count: acquisition.data.channel_count })}</span>}
      <span className="status-context">{busy ? t("app.working") : dirty ? t("app.unsaved") : t(space)}</span>
    </footer>
    {pendingView && <ConfigDialog title={t("app.unsaved")} message={t("app.discardQuestion")}
      confirmLabel={t("app.discard")} onCancel={() => setPendingView(null)} onConfirm={() => {
        setDirty(false); setView(pendingView); setPendingView(null);
      }} />}
    <RuntimeConfigurationReview runtime={runtime} />
  </div>;
}
