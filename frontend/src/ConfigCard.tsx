/** Shared Engine settings apply directly; server settings take effect on restart. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ConfigData, type ConfigEntry } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { useI18n, type TranslationKey } from "./i18n";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";

type EngineConfiguration = Awaited<ReturnType<typeof pluginConfigApi.engine>>;
type EngineDraft = Record<string, string | boolean>;
const engineFields = [
  { key: "NEUROENGINE.queue_size", label: "settings.queueCapacity", type: "int", fallback: 100 },
  { key: "NEUROENGINE.startup_timeout_s", label: "settings.startupTimeout", type: "float", fallback: 5 },
  { key: "debug", label: "settings.debug", type: "bool", fallback: false },
] as const;
const serverLabels: Record<string, TranslationKey> = {
  "LOGGING.level": "settings.logLevel", "LOGGING.file_path": "settings.logPath",
  "NEUROENGINE.command_timeout_s": "settings.commandTimeout", "PLUGINS.user_dir": "settings.pluginDirectory",
};

export function ConfigCard({ navigation, preferences, runtime, onDirtyChange, onBusyChange }: {
  navigation: ReactNode; preferences?: ReactNode;
  onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void;
  runtime: RuntimeConfiguration;
}) {
  const { t } = useI18n();
  const translateRef = useRef(t); translateRef.current = t;
  const [engine, setEngine] = useState<EngineConfiguration | null>(null);
  const engineRef = useRef(engine); engineRef.current = engine;
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [engineDraft, setEngineDraft] = useState<EngineDraft>({});
  const engineDraftRef = useRef(engineDraft); engineDraftRef.current = engineDraft;
  const [serverDraft, setServerDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const serverRequest = useRef(0), engineRequest = useRef(0);
  const reviewed = useRef<{ planId: string; settings: Record<string, unknown> } | null>(null);
  const saveFocus = useRef<HTMLElement | null>(null);
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const dirtyCount = Object.keys(engineDraft).length + Object.keys(serverDraft).length;
  const dirty = dirtyCount > 0;
  const updateEngineDraft = (value: EngineDraft) => { engineDraftRef.current = value; setEngineDraft(value); };
  const engineValues = (): EngineDraft => Object.fromEntries(engineFields.map(({ key, type, fallback }) => {
    const value = engineRef.current?.settings[key] ?? fallback;
    return [key, engineDraftRef.current[key] ?? (type === "bool" ? value === true : String(value))];
  }));
  const reportError = (cause: unknown) => {
    const message = cause instanceof ApiError ? `[${cause.code}] ${cause.message}` : String(cause);
    setError(cause instanceof ApiError && cause.status === 409 ? t("settings.conflict", { message }) : message);
  };
  const reloadServer = useCallback(async (replaceDraft = false) => {
    const id = ++serverRequest.current;
    const value = await api.getConfig();
    if (id !== serverRequest.current) return;
    if (!value) throw new Error(translateRef.current("settings.unavailableError"));
    setConfig(value);
    if (replaceDraft) setServerDraft({});
  }, []);
  const reloadEngine = useCallback(async (replaceDraft = false) => {
    const id = ++engineRequest.current;
    const value = await pluginConfigApi.engine();
    if (id !== engineRequest.current) return;
    if (replaceDraft || !engineRef.current || !Object.keys(engineDraftRef.current).length) {
      engineRef.current = value; setEngine(value);
      if (replaceDraft) { engineDraftRef.current = {}; setEngineDraft({}); }
    }
  }, []);
  const { completedOperation: completed, inProgress } = runtime.state("engine");
  useEffect(() => {
    const submitted = reviewed.current;
    if (!submitted || completed?.status !== "succeeded" || submitted.planId !== completed.plan_id) return;
    const current = engineValues(), remaining: EngineDraft = {};
    for (const { key, type } of engineFields) {
      const value = type === "bool" ? current[key] : Number(current[key]);
      if (value !== submitted.settings[key]) remaining[key] = current[key];
    }
    ++engineRequest.current;
    const value = { revision: completed.target_revisions.engine, settings: submitted.settings };
    engineRef.current = value; setEngine(value); updateEngineDraft(remaining);
    reviewed.current = null; setNotice("settings.engineApplied");
  }, [completed?.operation_id, completed?.status]);
  useEffect(() => {
    if (inProgress && engineRef.current) { ++engineRequest.current; return; }
    void reloadEngine().catch((cause) => setError(String(cause)));
  }, [reloadEngine, runtime.status?.accepted.engine, inProgress]);
  useEffect(() => {
    void reloadServer().catch((cause) => setError(String(cause))).finally(() => setBusy(false));
    return () => { ++serverRequest.current; ++engineRequest.current; };
  }, [reloadServer]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    if (busy || !saveFocus.current) return;
    const field = saveFocus.current; saveFocus.current = null;
    if (field.isConnected && field.getClientRects().length) field.focus(); else editorHeading.current?.focus();
  }, [busy]);
  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: t("settings.unsaved"), message: t("settings.discardPrompt"),
      cancelLabel: t("profiles.keepEditing"), confirmLabel: t("settings.discardChanges"),
      onConfirm: () => { setDialog(null); action(); } });
  };
  const refresh = async () => {
    setBusy(true); setError(null); setNotice(null);
    try {
      for (const result of await Promise.allSettled([reloadServer(true), reloadEngine(true)])) {
        if (result.status === "rejected") throw result.reason;
      }
      setNotice("settings.refreshed");
    }
    catch (cause) { reportError(cause); }
    finally { setBusy(false); }
  };
  const positiveNumber = (raw: string, integer: boolean, label: TranslationKey) => {
    const value = Number(raw);
    if (!raw.trim() || !Number.isFinite(value) || value <= 0 || integer && !Number.isSafeInteger(value)) {
      throw new Error(`${t(label)}: ${t(integer ? "settings.positiveInteger" : "settings.positiveNumber")}`);
    }
    return value;
  };
  const reviewReason = busy ? "feedback.busy" : runtimeBlockReason(runtime, "engine")
    ?? (!engine ? "settings.unavailableError" : engine.revision && !Object.keys(engineDraft).length ? "settings.noEngineChanges" : null);
  const reviewEngine = async () => {
    if (reviewReason || !engine) return;
    setError(null); setNotice(null);
    try {
      const raw = engineValues();
      const settings = Object.fromEntries(engineFields.map(({ key, type, label }) =>
        [key, type === "bool" ? raw[key] : positiveNumber(String(raw[key]), type === "int", label)]));
      setBusy(true);
      const plan = await runtime.review({ module: "engine", bindings: [], expected_revision: engine.revision, settings });
      if (plan) reviewed.current = { planId: plan.plan_id, settings };
    } catch (cause) { reportError(cause); }
    finally { setBusy(false); }
  };
  const saveServer = async (keepFocus = false) => {
    if (!config || busy || !Object.keys(serverDraft).length) return;
    setError(null); setNotice(null);
    try {
      const updates = Object.fromEntries(Object.entries(serverDraft).map(([key, raw]) => {
        if (key === "NEUROENGINE.command_timeout_s") return [key, positiveNumber(raw, false, serverLabels[key])];
        if (key === "LOGGING.file_path" && !raw.trim()) throw new Error(t("settings.logPathRequired"));
        return [key, raw];
      }));
      if (keepFocus) saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
      setBusy(true);
      await api.patchConfig(updates, config.revision);
      await reloadServer(true); setNotice("settings.saved");
    } catch (cause) { reportError(cause); }
    finally { setBusy(false); }
  };
  const renderServer = (entry: ConfigEntry) => {
    const label = serverLabels[entry.key], id = `setting-${entry.key}`;
    const saved = String(entry.value ?? ""), value = serverDraft[entry.key] ?? saved;
    const onChange = (raw: string) => setServerDraft((previous) => {
      const next = { ...previous }; if (raw === saved) delete next[entry.key]; else next[entry.key] = raw; return next;
    });
    return <div className={`setting-row ${serverDraft[entry.key] !== undefined ? "modified" : ""}`} key={entry.key}>
      <label className="setting-label" htmlFor={id}>{t(label)}</label>
      <p className="setting-description">{entry.description}{entry.restart_required && <> {t("settings.activeValue", { value: String(entry.active_value ?? "") })}</>}</p>
      {entry.key === "LOGGING.level" ? <select id={id} aria-label={t(label)} disabled={busy} value={value} onChange={(event) => onChange(event.target.value)}>
        {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((level) => <option key={level} value={level}>{level}</option>)}
      </select> : <input id={id} aria-label={t(label)} disabled={busy} value={value}
        type={entry.type === "float" ? "number" : "text"} step="any" onChange={(event) => onChange(event.target.value)} />}
    </div>;
  };
  return <section className="settings-workspace" aria-label={t("settings.general")} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "s" && !event.defaultPrevented) {
      event.preventDefault();
      if (!event.repeat && !event.nativeEvent?.isComposing && !busy && dirty && !dialog && !runtime.plan) {
        const group = (event.target as HTMLElement | null)?.closest?.("[data-group]")?.getAttribute("data-group");
        if (group === "Server" || !Object.keys(engineDraft).length) void saveServer(true); else void reviewEngine();
      }
    }
  }}>
    <aside className="explorer">{navigation}<div className="explorer-footer"><span className="eyebrow">{t("settings.host")}</span>
      <p>{t("settings.explorerHint")}</p></div></aside>
    <div className="settings-editor">
      <div className="breadcrumbs">{t("settings.settings")} <Icon name="chevron" /> {t("settings.generalShort")}</div>
      <header className="editor-heading"><div><p className="eyebrow">{t("settings.preferences")}</p><h1 ref={editorHeading} tabIndex={-1}>{t("settings.general")}</h1>
        <p>{t("settings.description")}</p></div><button className="icon-button" aria-label={t("settings.refresh")} title={t("settings.refresh")} disabled={busy}
          onClick={() => confirmDiscard(() => void refresh())}><Icon name="refresh" /></button></header>
      {notice && <p className="inline-message success" role="status">{t(notice)}</p>}
      {error && <p className="inline-message error" role="alert">{error}</p>}
      <div className="settings-scroll">{preferences}<div className="module-fields">
        <div className="setting-group" data-group="Engine"><h2>{t("settings.engine")}</h2>
          <p className="setting-description">{t("settings.applyHint")}</p>
          {engineFields.map(({ key, label, type, fallback }) => {
            const id = `setting-${key}`, value = engineValues()[key];
            const change = (raw: string | boolean) => {
              const baseline = engine?.settings[key] ?? fallback;
              const saved = type === "bool" ? baseline === true : String(baseline);
              const next = { ...engineDraftRef.current }; if (raw === saved) delete next[key]; else next[key] = raw;
              updateEngineDraft(next);
            };
            return <div className={`setting-row ${key in engineDraft ? "modified" : ""}`} key={key}>
              <label className="setting-label" htmlFor={id}>{t(label)}</label>
              {type === "bool" ? <input id={id} aria-label={t(label)} type="checkbox" disabled={busy || !engine} checked={value === true}
                onChange={(event) => change(event.target.checked)} />
                : <input id={id} aria-label={t(label)} type="number" step={type === "int" ? 1 : "any"} disabled={busy || !engine}
                  value={String(value)} onChange={(event) => change(event.target.value)} />}
            </div>;
          })}
          <p className="action-help" id="settings-review-help">{t(reviewReason ?? "settings.applyHint")}</p>
          <button disabled={!!reviewReason} aria-describedby="settings-review-help" onClick={() => void reviewEngine()}>{t("settings.review")}</button>
          <RuntimeConfigurationProgress runtime={runtime} module="engine" />
        </div>
        <div className="setting-group" data-group="Server"><h2>{t("settings.server")}</h2>
          <p className="setting-description">{t("settings.saveHelp")}</p>
          {(config?.entries ?? []).filter((entry) => serverLabels[entry.key]).map(renderServer)}
          {config?.entries.some((entry) => entry.restart_required) && <p className="inline-message" role="status">{t("settings.serverRestart")}</p>}
          <button disabled={busy || !config || !Object.keys(serverDraft).length} aria-keyshortcuts="Control+s Meta+s"
            onClick={() => void saveServer()}>{t("settings.save")}</button>
        </div>
      </div></div>
      <footer className="editor-footer"><div className="editor-feedback"><span className="save-state" role="status">{dirty
        ? <><span className="unsaved-dot" />{t("settings.unsavedCount", { count: dirtyCount })}</>
        : t(!config || !engine ? busy ? "settings.loading" : "settings.unavailable" : "settings.allSaved")}</span></div>
        <div className="actions"><button disabled={busy || !dirty} onClick={() => confirmDiscard(() => { updateEngineDraft({}); setServerDraft({}); })}>{t("settings.discard")}</button></div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
