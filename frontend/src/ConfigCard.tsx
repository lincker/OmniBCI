/**
 * Layered configuration editor for defaults and named profiles.
 * Editing defaults writes settings.toml; editing a profile writes its overrides.
 * Saving host settings and applying them to the runtime are separate actions.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ConfigData, type ConfigEntry, type ParadigmInfo, type ProfileList } from "./api/client";
import { translateConfigDescription, translatePluginName, useI18n } from "./i18n";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";

const DEFAULT = "__default__";
const categories = [
  ["all", "All settings"], ["ACQUISITION", "Acquisition"], ["ALGORITHM", "Processing"],
  ["STIMULATION", "Stimulation"], ["ACTUATION", "Actuation"], ["NEUROENGINE", "Engine"],
];
const pluginCategories: Record<string, string> = {
  "STIMULATION.paradigm": "paradigm", "ACQUISITION.signal_source": "acquisition",
  "ALGORITHM.decoder": "decoder", "ACTUATION.effector": "effector",
};
const pages = {
  general: { title: "General settings", description: "Preferences for the console and shared engine settings.", prefix: "NEUROENGINE." },
  advanced: { title: "Advanced configuration", description: "Inspect defaults and named application configurations.", prefix: "" },
};
const fieldLabels: Record<string, string> = {
  "NEUROENGINE.queue_size": "Queue capacity",
};
function show(value: unknown): string {
  if (value == null) return "";
  return Array.isArray(value) ? value.join(", ") : String(value);
}
function parseInput(raw: string, type: string): unknown {
  if (type === "int" || type === "float") {
    const value = Number(raw);
    if (!raw.trim() || !Number.isFinite(value) || (type === "int" && !Number.isSafeInteger(value))) {
      throw new Error(`Enter a valid ${type === "int" ? "integer" : "number"}.`);
    }
    return value;
  }
  if (type === "bool") return raw === "true";
  if (type === "list[str]") return raw.split(",").map((s) => s.trim()).filter(Boolean);
  return raw;
}

export function ConfigCard({ view, engineRunning, navigation, preferences, runtime, onDirtyChange, onBusyChange }: {
  view: keyof typeof pages; engineRunning: boolean; navigation: ReactNode; preferences?: ReactNode;
  onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void;
  runtime: RuntimeConfiguration;
}) {
  const { t } = useI18n();
  const page = pages[view];
  const advanced = view === "advanced";
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [profiles, setProfiles] = useState<ProfileList | null>(null);
  const [pluginOptions, setPluginOptions] = useState<Record<string, ParadigmInfo[]>>({});
  const [selected, setSelected] = useState(DEFAULT);
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState("");
  const [modifiedOnly, setModifiedOnly] = useState(false);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const request = useRef(0);
  const dirty = Object.keys(draft).length > 0;
  const active = config?.active_profile ?? null;

  const reload = useCallback(async (scope?: string) => {
    const id = ++request.current;
    const [cfg, profs, ...plugins] = await Promise.all([
      api.getConfig(), api.listProfiles(),
      ...Object.values(pluginCategories).map((cat) => api.listPlugins(cat).catch(() => null)),
    ]);
    if (id !== request.current) return;
    if (!cfg) throw new Error("Application settings are unavailable");
    const target = scope ?? (advanced ? DEFAULT : cfg.active_profile ?? DEFAULT);
    const detail = target === DEFAULT ? null : await api.getProfile(target);
    if (id !== request.current) return;
    setConfig(cfg); setProfiles(profs); setOverrides(detail?.entries ?? {}); setSelected(target);
    const options: Record<string, ParadigmInfo[]> = {};
    Object.keys(pluginCategories).forEach((key, i) => { if (plugins[i]) options[key] = plugins[i]!.paradigms; });
    setPluginOptions(options);
  }, [advanced]);
  useEffect(() => {
    void reload().catch((e) => setError(String(e))).finally(() => setBusy(false));
    return () => { request.current += 1; };
  }, [reload]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: "Unsaved changes", message: "Discard unsaved application settings?",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); action(); } });
  };
  const run = async (action: () => Promise<string | void>, success: string) => {
    setBusy(true); setError(null); setNotice(null);
    try {
      const scope = await action();
      setDraft({});
      await reload(scope ?? selected);
      setNotice(success);
    } catch (e) { setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e)); }
    finally { setBusy(false); }
  };
  const updates = () => {
    const result: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(draft)) {
      const entry = config?.entries.find((e) => e.key === key);
      if (entry) {
        try { result[key] = parseInput(raw, entry.type); }
        catch (e) { throw new Error(`${key}: ${(e as Error).message}`); }
      }
    }
    return result;
  };
  const save = () => {
    let values: Record<string, unknown>;
    try { values = updates(); } catch (e) { setError(String(e)); return; }
    void run(async () => { await api.patchConfig(values, selected === DEFAULT ? "default" : selected); },
      "Host settings saved. Review and apply the selected settings to update the runtime.");
  };
  const reviewHostSettings = async () => {
    setBusy(true); setError(null);
    try {
      const activation = await pluginConfigApi.activation();
      if (!activation || activation.bindings.some((binding) => !binding.profile_id)) {
        throw new Error("Save profiles for the selected plugins before applying host settings.");
      }
      await runtime.review({
        bindings: activation.bindings.map((binding) => ({ slot: binding.slot, profile_id: binding.profile_id! })),
        expected_revision: activation.revision, refresh_slots: [], include_host_settings: true,
      });
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const visible = (config?.entries ?? []).filter((entry) => {
    const inPage = entry.key.startsWith(page.prefix);
    const text = `${entry.key} ${translateConfigDescription(entry.key, entry.description, t)}`.toLowerCase();
    return inPage && (category === "all" || entry.key.startsWith(`${category}.`))
      && text.includes(search.toLowerCase()) && (!modifiedOnly || draft[entry.key] !== undefined);
  });
  const renderEntry = (entry: ConfigEntry) => {
    const value = selected === DEFAULT ? entry.default_value : overrides[entry.key] ?? entry.default_value;
    const locked = (selected === DEFAULT || selected === active) && engineRunning && entry.key.startsWith("ACQUISITION.");
    const fieldId = `setting-${entry.key}`;
    const raw = draft[entry.key] ?? show(value);
    const disabled = busy || locked;
    const options = pluginOptions[entry.key] ?? [];
    const onChange = (raw: string) => setDraft((previous) => {
      const next = { ...previous };
      if (raw === show(value)) delete next[entry.key]; else next[entry.key] = raw;
      return next;
    });
    const label = advanced ? entry.key : fieldLabels[entry.key] ?? entry.key.split(".")[1].replace(/_/g, " ");
    return <div className={`setting-row ${draft[entry.key] !== undefined ? "modified" : ""}`} key={entry.key}>
      <label className="setting-label" htmlFor={fieldId}>{label}</label>
      <p className="setting-description">{translateConfigDescription(entry.key, entry.description, t)}</p>
      {entry.type === "bool" ? <select id={fieldId} aria-label={label} disabled={disabled} value={raw} onChange={(event) => onChange(event.target.value)}>
        <option value="true">True</option><option value="false">False</option>
      </select> : <input id={fieldId} aria-label={label} disabled={disabled} value={raw}
        type={entry.type === "int" || entry.type === "float" ? "number" : "text"} step={entry.type === "int" ? 1 : "any"}
        list={options.length ? `options-${entry.key}` : undefined} onChange={(event) => onChange(event.target.value)} />}
      {options.length > 0 && <datalist id={`options-${entry.key}`}>
        {options.map((plugin) => <option key={plugin.plugin_type} value={plugin.plugin_type}>{translatePluginName(plugin.plugin_type, plugin.display_name, t)}</option>)}
      </datalist>}
      {locked && <span className="setting-detail">Stop the engine to edit this setting.</span>}
      {advanced && <span className="setting-detail">Saved resolved value: <strong>{show(entry.effective_value) || "Not set"}</strong>
        <span> · Saved host setting; review before applying</span></span>}
    </div>;
  };

  return <section className="settings-workspace" aria-label={page.title} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); if (!busy && dirty && !dialog) save();
    }
  }}>
    <aside className="explorer">
      {navigation}
      <div className="explorer-footer"><span className="eyebrow">Application configuration</span><strong>{active ?? "Default configuration"}</strong>
        <p>{view === "general" ? "Console preferences" : "Saved settings for the selected module."}</p></div>
    </aside>
    <div className="settings-editor">
      <div className="breadcrumbs">Settings <Icon name="chevron" /> {advanced ? "Advanced" : "General"}</div>
      <header className="editor-heading"><div><p className="eyebrow">Preferences</p><h1>{page.title}</h1>
        <p>{page.description}</p></div><button className="icon-button" aria-label="Refresh settings" title="Refresh settings" disabled={busy}
          onClick={() => confirmDiscard(() => void run(async () => {}, "Settings refreshed."))}><Icon name="refresh" /></button></header>
      {notice && <p className="inline-message success" role="status">{notice}</p>}
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {(advanced || !!profiles?.profiles.length) && <div className="editor-search">
        {advanced && <div className="search-box"><Icon name="search" />
          <input aria-label="Search application settings" placeholder="Search configuration keys" value={search} onChange={(e) => setSearch(e.target.value)} />
          <span className="search-count">{visible.length} settings</span></div>}
        {config && <div className="settings-scope">
          <label>Edit configuration<select aria-label="Edit application configuration" disabled={busy} value={selected}
            onChange={(event) => { const scope = event.target.value; confirmDiscard(() => void run(async () => scope, "")); }}>
            <option value={DEFAULT}>Default configuration</option>
            {profiles?.profiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.name}</option>)}
          </select></label>
          {advanced && <><label>Active configuration<select aria-label="Active application configuration" disabled={busy || engineRunning || runtime.blocked} value={active ?? DEFAULT}
            onChange={(event) => { const name = event.target.value; confirmDiscard(() => void run(async () => {
              if (name === DEFAULT) await api.deactivateProfile(); else await api.activateProfile(name);
            }, "Active configuration updated.")); }}>
            <option value={DEFAULT}>Default configuration</option>
            {profiles?.profiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.name}</option>)}
          </select></label>
          <label>Category<select value={category} onChange={(event) => setCategory(event.target.value)}>
            {categories.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select></label>
          <button className={`filter-button ${modifiedOnly ? "selected" : ""}`} aria-pressed={modifiedOnly}
            onClick={() => setModifiedOnly(!modifiedOnly)}>Unsaved ({Object.keys(draft).length})</button></>}
        </div>}
      </div>}
      {config && !advanced && <p className="inline-message">{selected !== (active ?? DEFAULT) ? "Editing a configuration that is not selected. Activate it in Settings → Advanced to use its overrides."
        : "Save host settings, then review and apply them. The preview reports changes that require an application restart."}</p>}
      <div className="settings-scroll">
        {!config && <div className="empty-state"><Icon name="settings" /><h2>{busy ? "Loading settings…" : "Settings unavailable"}</h2>
          {!busy && <button onClick={() => void run(async () => {}, "Settings refreshed.")}>Try again</button>}</div>}
        {preferences}
        {config && !visible.length && <p className="empty-search">No settings match the current filters.</p>}
        {advanced ? visible.map(renderEntry) : <div className="module-fields">
          {visible.length > 0 && <div className="setting-group" data-group="Engine">
            <h2>Engine</h2>{visible.map(renderEntry)}</div>}
        </div>}
      </div>
      <footer className="editor-footer"><span className="save-state">{dirty ? <><span className="unsaved-dot" />{Object.keys(draft).length} unsaved changes</> : "All changes saved"}</span>
        <div className="actions">
          {advanced && selected !== DEFAULT && <button className="text-button danger" disabled={busy || selected === active}
            onClick={() => setDialog({ title: "Delete application profile", message: `Delete “${selected}”?`, confirmLabel: "Delete profile",
              onConfirm: () => { setDialog(null); void run(async () => { await api.deleteProfile(selected); return DEFAULT; }, "Profile deleted."); } })}>Delete</button>}
          <button disabled={busy || !dirty} onClick={() => confirmDiscard(() => setDraft({}))}>Discard</button>
          {advanced && <button disabled={busy || !dirty} onClick={() => setDialog({ title: "Save changes as a profile",
            message: "Create an application profile from the changed settings.", initialValue: "", confirmLabel: "Create profile",
            onConfirm: (name) => {
              let values: Record<string, unknown>;
              try { values = updates(); } catch (e) { setDialog(null); setError(String(e)); return; }
              setDialog(null); void run(async () => { await api.putProfile(name, values); return name; }, "Application profile created.");
            } })}>Save as…</button>}
          <button className="primary" disabled={busy || !dirty} onClick={save}>Save settings</button>
          <button disabled={busy || dirty || runtime.blocked || !config || (selected !== DEFAULT && selected !== active)}
            onClick={() => void reviewHostSettings()}>Review and apply host settings</button>
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
