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
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { useI18n } from "./i18n";
import "./styles.css";

const setupPages = [
  ["acquisition", "Acquisition", "signal"], ["processing", "Processing", "workflow"],
  ["stimulation", "Stimulation", "system"], ["actuation", "Actuation", "plug"],
] as const;
const settingsPages = [["general", "General", "settings"], ["advanced", "Advanced", "profile"]] as const;
type SetupPage = typeof setupPages[number][0];
type SettingsPage = typeof settingsPages[number][0];
type View = SetupPage | SettingsPage | "run" | "results" | "extensions";
type Appearance = "system" | "light" | "dark";

function initialView(): View {
  const hash = window.location.hash.slice(1);
  if ([...setupPages, ...settingsPages].some(([page]) => page === hash) || hash === "run" || hash === "results" || hash === "extensions") return hash as View;
  if (["engine-panel", "acquisition-panel", "stimulation-panel", "acks-panel"].includes(hash)) return "run";
  if (hash === "results-panel") return "results";
  return "acquisition";
}

export default function App() {
  const { language, setLanguage } = useI18n();
  const [view, setView] = useState<View>(initialView);
  const [lastSetup, setLastSetup] = useState<SetupPage>("acquisition");
  const [lastSettings, setLastSettings] = useState<SettingsPage>("general");
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
  const engineLocked = engine.data === null || engine.error !== null || engineRunning;
  const isSetup = setupPages.some(([page]) => page === view);
  const isSettings = view === "general" || view === "advanced";
  const space = isSetup ? "Setup" : isSettings ? "Settings" : view === "run" ? "Run" : view === "extensions" ? "Extensions" : "Results";

  useEffect(() => {
    document.documentElement.style.colorScheme = appearance === "system" ? "light dark" : appearance;
    try { localStorage.setItem("omnibci-appearance", appearance); } catch { /* Browser storage may be disabled. */ }
  }, [appearance]);
  useEffect(() => {
    window.history.replaceState(null, "", `#${view}`);
    if (setupPages.some(([page]) => page === view)) setLastSetup(view as SetupPage);
    if (view === "general" || view === "advanced") setLastSettings(view);
  }, [view]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, busy]);

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
    <div className="explorer-heading">{isSetup ? "Setup" : "Preferences"}</div>
    <nav className="workspace-navigation" aria-label={isSetup ? "Setup modules" : "Settings pages"}>
      {(isSetup ? setupPages : settingsPages).map(([page, label, icon]) => <button key={page}
        className={`explorer-item ${view === page ? "selected" : ""}`} aria-current={view === page ? "page" : undefined}
        disabled={busy} onClick={() => navigate(page)}><Icon name={icon} /><span>{label}</span></button>)}
    </nav>
  </>;
  const preferences = <div className="setting-group"><h2>Interface</h2>
    <label className="setting-row"><span className="setting-label">Language</span>
      <select aria-label="Interface language" value={language} onChange={(e) => setLanguage(e.target.value as "en" | "zh")}>
        <option value="en">English</option><option value="zh">中文</option>
      </select></label>
    <label className="setting-row"><span className="setting-label">Appearance</span>
      <select aria-label="Appearance" value={appearance} onChange={(e) => setAppearance(e.target.value as Appearance)}>
        <option value="system">Follow system</option><option value="light">Light</option><option value="dark">Dark</option>
      </select></label>
  </div>;

  return <div className="workbench" onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); navigate("general"); }
  }}>
    <header className="title-bar">
      <span className="wordmark"><span className="brand-mark">Ω</span>OmniBCI</span>
      <div className="workspace-title"><Icon name="folder" /><span>Console</span><span className="title-separator">/</span><span>{space}</span></div>
      <span className="title-caption">BCI workspace</span>
    </header>
    <div className="workbench-body">
      <nav className="activity-bar" aria-label="Main workspaces">
        {([[lastSetup, "Setup", "settings"], ["run", "Run", "play"], ["results", "Results", "results"], ["extensions", "Extensions", "plug"]] as const).map(([page, label, icon]) =>
          <button key={label} className={`activity-item ${space === label ? "selected" : ""}`} aria-label={label} title={label}
            aria-current={space === label ? "page" : undefined} disabled={busy} onClick={() => navigate(page)}><Icon name={icon} /></button>)}
        <div className="activity-bottom"><button className={`activity-item ${isSettings ? "selected" : ""}`}
          aria-label="Settings" title="Settings" aria-current={isSettings ? "page" : undefined} disabled={busy}
          onClick={() => navigate(lastSettings)}><Icon name="gear" /></button></div>
      </nav>
      <main className="editor-workspace">
        <RuntimeConfigurationStatus runtime={runtime} />
        <div className="editor-viewport">
          {isSetup ? <PluginProfiles key={view} module={view as SetupPage} engineRunning={engineRunning} engineAvailable={engine.data !== null && engine.error === null} navigation={navigation}
            runtime={runtime} onDirtyChange={setDirty} onBusyChange={setBusy} />
            : view === "extensions" ? <PluginMarket runtimeBusy={runtime.blocked}
              onBusyChange={setBusy} onDirtyChange={setDirty} onOpenSetup={() => navigate(lastSetup)} />
            : view === "run" ? <RunConsole engine={engine} acquisition={acquisition} feed={feed}
              runtime={runtime} onBusyChange={setBusy} onEditSetup={() => navigate(lastSetup)} onResults={() => navigate("results")} />
            : view === "results" ? <div className="runtime-content">
              <div className="breadcrumbs">Results <Icon name="chevron" /> Current session</div>
              <header className="editor-heading"><div><p className="eyebrow">Session data</p><h1>Trial results</h1>
                <p>Decoded trials from the current or most recent session.</p></div>
                <button onClick={() => navigate("run")}><Icon name="play" />Open Run</button></header>
              <SessionSummary session={feed.session} trialCount={feed.trials.length} />
              <section className="card"><h2>Results <span className="count">{feed.trials.length}</span></h2><TrialResults trials={feed.trials} /></section>
            </div>
            : <ConfigCard key={view} view={view as SettingsPage} engineRunning={engineLocked} navigation={navigation}
              preferences={view === "general" ? preferences : undefined} onDirtyChange={setDirty} onBusyChange={setBusy}
              runtime={runtime} />}
        </div>
      </main>
    </div>
    <footer className="status-bar" aria-label="Console status">
      <span className={`connection-status connection-${feed.connection}`}><span className="status-dot" />
        {feed.connection === "open" ? "Connected" : feed.connection === "connecting" ? "Connecting…" : "Disconnected"}</span>
      <span>{engine.error ? "Engine unavailable" : !engine.data ? "Checking engine…" : engineRunning ? "Engine running" : "Engine stopped"}</span>
      <span className="status-spacer" />
      {acquisition.data?.sample_rate != null && <span>{acquisition.data.sample_rate} Hz</span>}
      {acquisition.data?.channel_count != null && <span className="status-source">{acquisition.data.channel_count} channels</span>}
      <span className="status-context">{busy ? "Working…" : dirty ? "Unsaved changes" : space}</span>
    </footer>
    {pendingView && <ConfigDialog title="Unsaved changes" message="Discard unsaved configuration changes?"
      confirmLabel="Discard changes" onCancel={() => setPendingView(null)} onConfirm={() => {
        setDirty(false); setView(pendingView); setPendingView(null);
      }} />}
    <RuntimeConfigurationReview runtime={runtime} />
  </div>;
}
