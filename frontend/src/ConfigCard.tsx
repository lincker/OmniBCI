/**
 * Host configuration editor for shared engine settings.
 * Editing settings writes settings.toml.
 * Saving host settings and applying them to the runtime are separate actions.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ConfigData, type ConfigEntry } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { useI18n, type Translate, type TranslationKey } from "./i18n";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi, type ModulesResponse } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import type { RuntimePlanRequest } from "./api/runtime-config";
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";

const sectionKeys = {
  processing: ["ALGORITHM.baseline_duration"],
  stimulation: ["STIMULATION.renderer", "STIMULATION.auto_spawn"],
  actuation: ["ACTUATION.server_ip", "ACTUATION.server_port", "ACTUATION.watchdog_timeout_s", "ACTUATION.max_command_age_s", "ACTUATION.auto_spawn"],
  system: ["NEUROENGINE.queue_size", "NEUROENGINE.message_server_ip", "NEUROENGINE.message_server_port", "NEUROENGINE.startup_timeout_s", "debug"],
};
type Section = keyof typeof sectionKeys;
const fieldLabels: Record<string, TranslationKey> = {
  "ALGORITHM.baseline_duration": "settings.baselineDuration",
  "NEUROENGINE.queue_size": "settings.queueCapacity",
};
function parseInput(raw: string, type: string, t: Translate): number {
  const value = Number(raw);
  if (!raw.trim() || !Number.isFinite(value) || (type === "int" && !Number.isSafeInteger(value))) {
    throw new Error(t(type === "int" ? "settings.validInteger" : "settings.validNumber"));
  }
  return value;
}

export function ConfigCard({ navigation, preferences, runtime, onDirtyChange, onBusyChange }: {
  navigation: ReactNode; preferences?: ReactNode;
  onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void;
  runtime: RuntimeConfiguration;
}) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [activation, setActivation] = useState<ModulesResponse | null>(null);
  const [section, setSection] = useState<Section>("system");
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const request = useRef(0);
  const saveFocus = useRef<HTMLElement | null>(null);
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const dirty = Object.keys(draft).length > 0;

  const reload = useCallback(async () => {
    const id = ++request.current;
    const cfg = await api.getConfig();
    if (id !== request.current) return;
    if (!cfg) throw new Error(translateRef.current("settings.unavailableError"));
    setConfig(cfg);
  }, []);
  useEffect(() => {
    void reload().catch((e) => setError(String(e))).finally(() => setBusy(false));
    return () => { request.current += 1; };
  }, [reload]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    if (busy || !saveFocus.current) return;
    const field = saveFocus.current;
    saveFocus.current = null;
    if (field.isConnected && field.getClientRects().length) field.focus();
    else editorHeading.current?.focus();
  }, [busy]);

  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: t("settings.unsaved"), message: t("settings.discardPrompt"),
      cancelLabel: t("profiles.keepEditing"),
      confirmLabel: t("settings.discardChanges"), onConfirm: () => { setDialog(null); action(); } });
  };
  const run = async (action: () => Promise<void>, success: TranslationKey) => {
    setBusy(true); setError(null); setNotice(null);
    try {
      await action();
      await reload();
      setDraft({});
      setNotice(success);
    } catch (e) {
      const message = e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e);
      setError(e instanceof ApiError && e.status === 409
        ? t("settings.conflict", { message }) : message);
    }
    finally { setBusy(false); }
  };
  const save = (keepFocus = false) => {
    if (!config) return;
    const updates: Record<string, unknown> = {};
    try {
      for (const entry of config.entries) {
        const raw = draft[entry.key];
        if (fieldLabels[entry.key] && raw !== undefined) {
          try { updates[entry.key] = parseInput(raw, entry.type, t); }
          catch (e) { throw new Error(`${t(fieldLabels[entry.key])}: ${(e as Error).message}`); }
        }
      }
    } catch (e) { setError(String(e)); return; }
    if (keepFocus) saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
    void run(async () => { await api.patchConfig(updates, config.revision); },
      "settings.saved");
  };
  useEffect(() => {
    let active = true;
    void pluginConfigApi.modules().then((value) => { if (active) setActivation(value); })
      .catch((cause) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [JSON.stringify(runtime.status?.accepted), config]);
  const changedSections = (Object.keys(sectionKeys) as Section[]).filter((module) => {
    const accepted = module === "system" ? activation?.system.settings : activation?.modules[module]?.settings;
    return config?.entries.some((entry) => sectionKeys[module].includes(entry.key) && JSON.stringify(entry.value) !== JSON.stringify(accepted?.[entry.key]));
  });
  const selectedSection = changedSections.includes(section) ? section : changedSections[0];
  const reviewHostSettings = async () => {
    if (!selectedSection) return;
    setBusy(true); setError(null);
    try {
      const current = await pluginConfigApi.modules();
      const selected = selectedSection === "system" ? current.system : current.modules[selectedSection];
      const bindings: RuntimePlanRequest["bindings"] = selectedSection === "system" ? [] : current.modules[selectedSection].bindings.map((binding) => {
        if (binding.profile_id) return { slot: binding.slot, profile_id: binding.profile_id };
        return { slot: binding.slot, selection: binding.selection, parameters: binding.parameters,
          configuration_version: binding.configuration_version,
          ...(binding.slot === "processing.decoder" ? { processing_input: binding.processing_input } : {}) };
      });
      await runtime.review({ module: selectedSection, bindings, expected_revision: selected.revision, refresh_slots: [] });
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const entries = (config?.entries ?? []).filter((entry) => fieldLabels[entry.key]);
  const reviewReason = busy ? "feedback.busy" : dirty ? "settings.saveBeforeReview"
    : runtimeBlockReason(runtime) ?? (!config || !activation ? "settings.unavailableError" : !selectedSection ? "settings.noChanges" : null);
  const renderEntry = (entry: ConfigEntry) => {
    const fieldId = `setting-${entry.key}`;
    const saved = String(entry.value ?? "");
    const raw = draft[entry.key] ?? saved;
    const onChange = (raw: string) => setDraft((previous) => {
      const next = { ...previous };
      if (raw === saved) delete next[entry.key]; else next[entry.key] = raw;
      return next;
    });
    const label = t(fieldLabels[entry.key]);
    return <div className={`setting-row ${draft[entry.key] !== undefined ? "modified" : ""}`} key={entry.key}>
      <label className="setting-label" htmlFor={fieldId}>{label}</label>
      <p className="setting-description">{entry.key === "NEUROENGINE.queue_size" ? t("settings.queueDescription") : t("settings.preTriggerDescription")}</p>
      <input id={fieldId} aria-label={label} disabled={busy} value={raw} type="number"
        step={entry.type === "int" ? 1 : "any"} onChange={(event) => onChange(event.target.value)} />
    </div>;
  };

  return <section className="settings-workspace" aria-label={t("settings.general")} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "s" && !event.defaultPrevented) {
      event.preventDefault();
      if (!event.repeat && !event.nativeEvent?.isComposing && !busy && dirty && !dialog && !runtime.plan) save(true);
    }
  }}>
    <aside className="explorer">
      {navigation}
      <div className="explorer-footer"><span className="eyebrow">{t("settings.host")}</span>
        <p>{t("settings.explorerHint")}</p></div>
    </aside>
    <div className="settings-editor">
      <div className="breadcrumbs">{t("settings.settings")} <Icon name="chevron" /> {t("settings.generalShort")}</div>
      <header className="editor-heading"><div><p className="eyebrow">{t("settings.preferences")}</p><h1 ref={editorHeading} tabIndex={-1}>{t("settings.general")}</h1>
        <p>{t("settings.description")}</p></div><button className="icon-button" aria-label={t("settings.refresh")} title={t("settings.refresh")} disabled={busy}
          onClick={() => confirmDiscard(() => void run(async () => {}, "settings.refreshed"))}><Icon name="refresh" /></button></header>
      {notice && <p className="inline-message success" role="status">{t(notice)}</p>}
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {config && <p className="inline-message">{t("settings.applyHint")}</p>}
      <div className="settings-scroll">
        {!config && <div className="empty-state"><Icon name="settings" /><h2>{busy ? t("settings.loading") : t("settings.unavailable")}</h2>
          {!busy && <button onClick={() => void run(async () => {}, "settings.refreshed")}>{t("settings.tryAgain")}</button>}</div>}
        {preferences}
        <div className="module-fields">
          {entries.length > 0 && <div className="setting-group" data-group="Engine">
            <h2>{t("settings.engine")}</h2>{entries.map(renderEntry)}</div>}
        </div>
      </div>
      <footer className="editor-footer"><div className="editor-feedback">
        <span className="save-state" role="status">{dirty ? <><span className="unsaved-dot" />{t("settings.unsavedCount", { count: Object.keys(draft).length })}</>
          : t(config ? "settings.savedOnly" : "settings.unavailable")}</span>
        <p className="action-help" id="settings-save-help">{t(busy ? "feedback.busy" : dirty ? "settings.saveHelp" : "settings.noChanges")}</p>
        <p className="action-help" id="settings-review-help">{t(reviewReason ?? "settings.applyHint")}</p>
        <RuntimeConfigurationProgress runtime={runtime} />
      </div>
        <div className="actions">
          <button disabled={busy || !dirty} onClick={() => confirmDiscard(() => setDraft({}))}>{t("settings.discard")}</button>
          <button className="primary" disabled={busy || !dirty} aria-describedby="settings-save-help" aria-keyshortcuts="Control+s Meta+s" onClick={() => save()}>{t("settings.save")}</button>
          {changedSections.length > 0 && <select aria-label={t("runtime.affectedModules")} value={selectedSection} disabled={busy || runtime.blocked}
            onChange={(event) => setSection(event.target.value as Section)}>{changedSections.map((module) => <option key={module} value={module}>{module}</option>)}</select>}
          <button disabled={!!reviewReason} aria-describedby="settings-review-help"
            onClick={() => void reviewHostSettings()}>{t("settings.review")}</button>
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
