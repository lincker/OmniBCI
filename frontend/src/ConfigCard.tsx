/** Shared Engine settings apply directly; backend settings take effect on restart. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ConfigData, type ConfigEntry } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { useI18n, type TranslationKey } from "./i18n";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";

type SystemConfiguration = Awaited<ReturnType<typeof pluginConfigApi.system>>;
type EngineDraft = Record<string, string | boolean>;
const engineFields = [
  { key: "NEUROENGINE.queue_size", label: "settings.queueCapacity", type: "int", fallback: 100 },
  { key: "NEUROENGINE.startup_timeout_s", label: "settings.startupTimeout", type: "float", fallback: 5 },
  { key: "debug", label: "settings.debug", type: "bool", fallback: false },
] as const;
const backendLabels: Record<string, TranslationKey> = {
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
  const [system, setSystem] = useState<SystemConfiguration | null>(null);
  const systemRef = useRef(system); systemRef.current = system;
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [engineDraft, setEngineDraft] = useState<EngineDraft>({});
  const engineDraftRef = useRef(engineDraft); engineDraftRef.current = engineDraft;
  const [backendDraft, setBackendDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const backendRequest = useRef(0), systemRequest = useRef(0);
  const reviewed = useRef<{ planId: string; settings: Record<string, unknown> } | null>(null);
  const saveFocus = useRef<HTMLElement | null>(null);
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const dirtyCount = Object.keys(engineDraft).length + Object.keys(backendDraft).length;
  const dirty = dirtyCount > 0;
  const updateEngineDraft = (value: EngineDraft) => { engineDraftRef.current = value; setEngineDraft(value); };
  const engineValues = (): EngineDraft => Object.fromEntries(engineFields.map(({ key, type, fallback }) => {
    const value = systemRef.current?.settings[key] ?? fallback;
    return [key, engineDraftRef.current[key] ?? (type === "bool" ? value === true : String(value))];
  }));
  const reportError = (cause: unknown) => {
    const message = cause instanceof ApiError ? `[${cause.code}] ${cause.message}` : String(cause);
    setError(cause instanceof ApiError && cause.status === 409 ? t("settings.conflict", { message }) : message);
  };
  const reloadBackend = useCallback(async (replaceDraft = false) => {
    const id = ++backendRequest.current;
    const value = await api.getConfig();
    if (id !== backendRequest.current) return;
    if (!value) throw new Error(translateRef.current("settings.unavailableError"));
    setConfig(value);
    if (replaceDraft) setBackendDraft({});
  }, []);
  const reloadSystem = useCallback(async (replaceDraft = false) => {
    const id = ++systemRequest.current;
    const value = await pluginConfigApi.system();
    if (id !== systemRequest.current) return;
    if (replaceDraft || !systemRef.current || !Object.keys(engineDraftRef.current).length) {
      systemRef.current = value; setSystem(value);
      if (replaceDraft) { engineDraftRef.current = {}; setEngineDraft({}); }
    }
  }, []);
  const { completedOperation: completed, inProgress } = runtime.state("system");
  useEffect(() => {
    const submitted = reviewed.current;
    if (!submitted || completed?.status !== "succeeded" || submitted.planId !== completed.plan_id) return;
    const current = engineValues(), remaining: EngineDraft = {};
    for (const { key, type } of engineFields) {
      const value = type === "bool" ? current[key] : Number(current[key]);
      if (value !== submitted.settings[key]) remaining[key] = current[key];
    }
    ++systemRequest.current;
    const value = { revision: completed.target_revisions.system, settings: submitted.settings };
    systemRef.current = value; setSystem(value); updateEngineDraft(remaining);
    reviewed.current = null; setNotice("settings.engineApplied");
  }, [completed?.operation_id, completed?.status]);
  useEffect(() => {
    if (inProgress && systemRef.current) { ++systemRequest.current; return; }
    void reloadSystem().catch((cause) => setError(String(cause)));
  }, [reloadSystem, runtime.status?.accepted.system, inProgress]);
  useEffect(() => {
    void reloadBackend().catch((cause) => setError(String(cause))).finally(() => setBusy(false));
    return () => { ++backendRequest.current; ++systemRequest.current; };
  }, [reloadBackend]);
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
      for (const result of await Promise.allSettled([reloadBackend(true), reloadSystem(true)])) {
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
  const reviewReason = busy ? "feedback.busy" : runtimeBlockReason(runtime, "system")
    ?? (!system ? "settings.unavailableError" : system.revision && !Object.keys(engineDraft).length ? "settings.noEngineChanges" : null);
  const reviewEngine = async () => {
    if (reviewReason || !system) return;
    setError(null); setNotice(null);
    try {
      const raw = engineValues();
      const settings = Object.fromEntries(engineFields.map(({ key, type, label }) =>
        [key, type === "bool" ? raw[key] : positiveNumber(String(raw[key]), type === "int", label)]));
      setBusy(true);
      const plan = await runtime.review({ module: "system", bindings: [], expected_revision: system.revision, settings });
      if (plan) reviewed.current = { planId: plan.plan_id, settings };
    } catch (cause) { reportError(cause); }
    finally { setBusy(false); }
  };
  const saveBackend = async (keepFocus = false) => {
    if (!config || busy || !Object.keys(backendDraft).length) return;
    setError(null); setNotice(null);
    try {
      const updates = Object.fromEntries(Object.entries(backendDraft).map(([key, raw]) => {
        if (key === "NEUROENGINE.command_timeout_s") return [key, positiveNumber(raw, false, backendLabels[key])];
        if (key === "LOGGING.file_path" && !raw.trim()) throw new Error(t("settings.logPathRequired"));
        return [key, raw];
      }));
      if (keepFocus) saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
      setBusy(true);
      await api.patchConfig(updates, config.revision);
      await reloadBackend(true); setNotice("settings.saved");
    } catch (cause) { reportError(cause); }
    finally { setBusy(false); }
  };
  const renderBackend = (entry: ConfigEntry) => {
    const label = backendLabels[entry.key], id = `setting-${entry.key}`;
    const saved = String(entry.value ?? ""), value = backendDraft[entry.key] ?? saved;
    const onChange = (raw: string) => setBackendDraft((previous) => {
      const next = { ...previous }; if (raw === saved) delete next[entry.key]; else next[entry.key] = raw; return next;
    });
    return <div className={`setting-row ${backendDraft[entry.key] !== undefined ? "modified" : ""}`} key={entry.key}>
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
        if (group === "Backend" || !Object.keys(engineDraft).length) void saveBackend(true); else void reviewEngine();
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
              const baseline = system?.settings[key] ?? fallback;
              const saved = type === "bool" ? baseline === true : String(baseline);
              const next = { ...engineDraftRef.current }; if (raw === saved) delete next[key]; else next[key] = raw;
              updateEngineDraft(next);
            };
            return <div className={`setting-row ${key in engineDraft ? "modified" : ""}`} key={key}>
              <label className="setting-label" htmlFor={id}>{t(label)}</label>
              {type === "bool" ? <input id={id} aria-label={t(label)} type="checkbox" disabled={busy || !system} checked={value === true}
                onChange={(event) => change(event.target.checked)} />
                : <input id={id} aria-label={t(label)} type="number" step={type === "int" ? 1 : "any"} disabled={busy || !system}
                  value={String(value)} onChange={(event) => change(event.target.value)} />}
            </div>;
          })}
          <p className="action-help" id="settings-review-help">{t(reviewReason ?? "settings.applyHint")}</p>
          <button disabled={!!reviewReason} aria-describedby="settings-review-help" onClick={() => void reviewEngine()}>{t("settings.review")}</button>
          <RuntimeConfigurationProgress runtime={runtime} module="system" />
        </div>
        <div className="setting-group" data-group="Backend"><h2>{t("settings.backend")}</h2>
          <p className="setting-description">{t("settings.saveHelp")}</p>
          {(config?.entries ?? []).filter((entry) => backendLabels[entry.key]).map(renderBackend)}
          {config?.entries.some((entry) => entry.restart_required) && <p className="inline-message" role="status">{t("settings.backendRestart")}</p>}
          <button disabled={busy || !config || !Object.keys(backendDraft).length} aria-keyshortcuts="Control+s Meta+s"
            onClick={() => void saveBackend()}>{t("settings.save")}</button>
        </div>
      </div></div>
      <footer className="editor-footer"><div className="editor-feedback"><span className="save-state" role="status">{dirty
        ? <><span className="unsaved-dot" />{t("settings.unsavedCount", { count: dirtyCount })}</>
        : t(!config || !system ? busy ? "settings.loading" : "settings.unavailable" : "settings.allSaved")}</span></div>
        <div className="actions"><button disabled={busy || !dirty} onClick={() => confirmDiscard(() => { updateEngineDraft({}); setBackendDraft({}); })}>{t("settings.discard")}</button></div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
