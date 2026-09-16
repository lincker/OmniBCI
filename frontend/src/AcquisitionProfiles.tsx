/** Local acquisition presets. Editing a profile does not activate it. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ModuleConfig, type ModuleProfile } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";

const CURRENT = "__current__";
const NEW = "__new__";
type Editor = { source: string; plugin: string; name: string; parameters: Record<string, string> };
const emptyEditor: Editor = { source: NEW, plugin: "", name: "", parameters: {} };
const fieldGroups = [
  { title: "Connection", keys: ["DATA_PROVIDER_IP", "DATA_PROVIDER_PORT"] },
  { title: "Recording", keys: ["data_file_path", "file_reader_chunk_size"] },
  { title: "Signal", keys: ["sample_rate", "channel_number"] },
];

function parametersAsText(parameters: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.entries(parameters).map(([key, value]) => [key, String(value)]));
}
function profileEditor(profile: ModuleProfile): Editor {
  return { source: profile.profile_id, plugin: profile.plugin_type, name: profile.name,
    parameters: parametersAsText(profile.parameters) };
}
function currentEditor(config: ModuleConfig): Editor {
  return { source: CURRENT, plugin: config.effective_plugin_type ?? "", name: "",
    parameters: parametersAsText(config.effective_parameters) };
}
function message(error: unknown): string {
  return error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
}

export function AcquisitionProfiles({ engineRunning, engineAvailable, navigation, onDirtyChange, onBusyChange }: {
  engineRunning: boolean; engineAvailable: boolean; navigation: ReactNode;
  onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void;
}) {
  const [config, setConfig] = useState<ModuleConfig | null>(null);
  const [profiles, setProfiles] = useState<ModuleProfile[]>([]);
  const [editor, setEditor] = useState<Editor>(emptyEditor);
  const [baseline, setBaseline] = useState<Editor>(emptyEditor);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const [profileSearch, setProfileSearch] = useState("");
  const request = useRef(0);
  const dirty = JSON.stringify(editor) !== JSON.stringify(baseline);
  const active = config?.active_profile_id ?? null;
  const selected = profiles.find((p) => p.profile_id === editor.source);
  const plugin = config?.plugins.find((p) => p.plugin_type === editor.plugin);
  const fields = plugin?.fields ?? [];
  const activeEditor = selected?.profile_id === active && active !== null;
  const selectionLocked = engineRunning || !engineAvailable;
  const parametersLocked = selectionLocked && activeEditor;
  const visibleProfiles = profiles.filter((p) => `${p.name} ${p.plugin_type}`.toLowerCase().includes(profileSearch.toLowerCase()));
  const activeName = profiles.find((p) => p.profile_id === active)?.name ?? (active ? "Unavailable profile" : "Application settings");

  const resetEditor = useCallback((next: Editor) => { setEditor(next); setBaseline(next); }, []);
  const reload = useCallback(async (source?: string) => {
    const id = ++request.current;
    const [configuration, list] = await Promise.allSettled([api.getAcquisitionConfig(), api.listAcquisitionProfiles()]);
    if (id !== request.current) return;
    if (configuration.status === "rejected") throw configuration.reason;
    const cfg = configuration.value;
    if (!cfg) throw new Error("Acquisition configuration is unavailable");
    const items = list.status === "fulfilled" ? list.value?.profiles ?? [] : [];
    if (list.status === "rejected") setError(message(list.reason));
    setConfig(cfg);
    setProfiles(items);
    const profile = items.find((p) => p.profile_id === (source ?? cfg.active_profile_id));
    resetEditor(profile ? profileEditor(profile) : currentEditor(cfg));
  }, [resetEditor]);

  useEffect(() => {
    void reload().catch((e) => setError(message(e))).finally(() => setBusy(false));
    return () => { request.current += 1; };
  }, [reload]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: "Unsaved changes", message: "Discard unsaved profile changes?",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); action(); } });
  };
  const run = async (action: () => Promise<string>, success: string) => {
    setBusy(true); setError(null); setNotice(null);
    try {
      const source = await action();
      await reload(source);
      setNotice(success);
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  const newEditor = (type: string): Editor => ({
    source: NEW, plugin: type, name: "",
    parameters: Object.fromEntries((config?.plugins.find((p) => p.plugin_type === type)?.fields ?? [])
      .map((field) => [field.key, String(field.default)])),
  });
  const changeEditor = (source: string) => {
    if (!config) return;
    setError(null); setNotice(null);
    const profile = profiles.find((p) => p.profile_id === source);
    resetEditor(profile ? profileEditor(profile) : source === CURRENT
      ? currentEditor(config) : newEditor(config.plugins[0]?.plugin_type ?? ""));
  };
  const selectProfile = (id: string | null) => confirmDiscard(() => void run(async () => {
    await api.selectAcquisitionProfile(id);
    return id ?? CURRENT;
  }, "Selection saved. It will be used at the next engine start."));
  const save = (copy: boolean, name = editor.name) => {
    const parameters: Record<string, unknown> = {};
    for (const field of fields) {
      const raw = editor.parameters[field.key] ?? "";
      if (field.type === "int") {
        const value = Number(raw);
        if (!raw.trim() || !Number.isSafeInteger(value)
            || (field.minimum != null && value < field.minimum)
            || (field.maximum != null && value > field.maximum)) {
          setError(`${field.label}: enter an integer within the allowed range.`); return;
        }
        parameters[field.key] = value;
      } else { parameters[field.key] = raw; }
    }
    if (!name.trim()) { setError("Enter a profile name."); return; }
    void run(async () => {
      const saved = selected && !copy
        ? await api.updateAcquisitionProfile(selected.profile_id, { name, parameters })
        : await api.createAcquisitionProfile({ name, plugin_type: editor.plugin, parameters });
      if (!saved) throw new Error("The profile was not returned by the server");
      resetEditor(profileEditor(saved));
      setProfileSearch("");
      return saved.profile_id;
    }, "Profile saved.");
  };

  return (
    <section className="settings-workspace" aria-label="Acquisition profiles" onKeyDown={(event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (!busy && dirty && fields.length && !dialog) save(false);
      }
    }}>
      <aside className="explorer profile-explorer">
        {navigation}
        <div className="explorer-heading"><span>Profiles</span>
          <button className="icon-button" aria-label="Refresh profiles" title="Refresh profiles" disabled={busy}
            onClick={() => confirmDiscard(() => void run(async () => editor.source, "Profiles refreshed."))}><Icon name="refresh" /></button>
        </div>
        <div className="explorer-tools">
          <button className="primary full-width" disabled={busy || !config}
            onClick={() => confirmDiscard(() => changeEditor(NEW))}><Icon name="plus" />New profile</button>
          <div className="search-box compact"><Icon name="search" />
            <input aria-label="Search profiles" placeholder="Find a profile" value={profileSearch}
              onChange={(event) => setProfileSearch(event.target.value)} /></div>
        </div>
        <button className={`explorer-item profile-item ${editor.source === CURRENT ? "selected" : ""}`}
          aria-current={editor.source === CURRENT ? "true" : undefined} disabled={busy || !config}
          onClick={() => confirmDiscard(() => changeEditor(CURRENT))}>
          <Icon name="copy" /><span><strong>Current configuration</strong><small>Copy effective settings</small></span>
        </button>
        <div className="explorer-section">Saved profiles <span className="count">{profiles.length}</span></div>
        <div className="profile-list" aria-label="Saved profiles">
          {visibleProfiles.map((profile) => <button key={profile.profile_id}
            className={`explorer-item profile-item ${editor.source === profile.profile_id ? "selected" : ""}`}
            aria-current={editor.source === profile.profile_id ? "true" : undefined}
            disabled={busy} onClick={() => confirmDiscard(() => changeEditor(profile.profile_id))}>
            <Icon name="profile" /><span><strong>{profile.name}</strong><small>{config?.plugins.find((p) => p.plugin_type === profile.plugin_type)?.label ?? profile.plugin_type}</small></span>
            {profile.profile_id === active && <span className="active-indicator" title="Selected profile" aria-label="Selected profile">●</span>}
          </button>)}
          {!visibleProfiles.length && <p className="explorer-empty">{profileSearch ? "No matching profiles." : "No saved profiles yet. Create one from your device settings."}</p>}
        </div>
        <div className="explorer-footer">
          <span className="eyebrow">Selected for acquisition</span><strong>{activeName}</strong>
          <button className="text-button" disabled={busy || selectionLocked || !active}
            onClick={() => selectProfile(null)}>Use application settings</button>
        </div>
      </aside>
      <div className="settings-editor">
        <div className="breadcrumbs">Setup <Icon name="chevron" /> Acquisition</div>
        <header className="editor-heading">
          <div><p className="eyebrow">Acquisition</p><h1>{selected?.name ?? (editor.source === NEW ? "New profile" : "Current configuration")}</h1>
            <p>Reusable settings for your devices and recordings.</p></div>
          {activeEditor ? <span className="status-tag success"><Icon name="check" />Selected</span>
            : selected && <button className="primary" disabled={busy || selectionLocked || dirty}
              title={dirty ? "Save or discard changes before using this profile" : "Use this profile on the next engine start"}
              onClick={() => selectProfile(selected.profile_id)}><Icon name="check" />Use profile</button>}
        </header>
        {error && <p className="inline-message error" role="alert">{error}</p>}
        {config?.selection_error && <p className="inline-message error" role="alert">{config.selection_error}</p>}
        {notice && <p className="inline-message success" role="status">{notice}</p>}
        {engineRunning && <p className="inline-message">The engine is running. Stop it to switch profiles or edit active parameters.</p>}
        {!config && <div className="empty-state"><Icon name="settings" /><h2>{busy ? "Loading profiles…" : "Profiles unavailable"}</h2>
          {!busy && <button onClick={() => void run(async () => CURRENT, "Profiles refreshed.")}>Try again</button>}</div>}
        {config && <>
          <div className="settings-scroll">
            <div className="profile-fields"><div className="setting-group profile-identity"><h2>Profile</h2>
              <label className={`setting-row ${editor.name !== baseline.name ? "modified" : ""}`}>
                <span className="setting-label">Name</span><span className="setting-description">A recognizable name for this acquisition setup.</span>
                <input aria-label="Profile name" maxLength={64} value={editor.name} disabled={busy}
                  placeholder="e.g. Scan45 · Lab A" onChange={(event) => setEditor({ ...editor, name: event.target.value })} />
              </label>
              <label className="setting-row"><span className="setting-label">Acquisition plugin</span>
                <span className="setting-description">The device or file reader used by this profile.</span>
                <select aria-label="Acquisition profile plugin" value={editor.plugin} disabled={busy || !!selected}
                  onChange={(event) => { const type = event.target.value; confirmDiscard(() => resetEditor(newEditor(type))); }}>
                  {!fields.length && <option value={editor.plugin}>Select a supported plugin</option>}
                  {config.plugins.map((p) => <option key={p.plugin_type} value={p.plugin_type}>{p.label}</option>)}
                </select>
              </label>
            </div>
            {fieldGroups.map((group) => {
              const items = fields.filter((f) => group.keys.includes(f.key));
              return items.length > 0 && <div className="setting-group" key={group.title}><h2>{group.title}</h2>
                {items.map((field) => <label key={field.key}
                  className={`setting-row ${editor.parameters[field.key] !== baseline.parameters[field.key] ? "modified" : ""}`}>
                  <span className="setting-label">{field.label}
                    {field.minimum != null && field.minimum === field.maximum && <span className="setting-badge">Fixed</span>}</span>
                  <span className="setting-description">{field.description}</span>
                  <input aria-label={field.label} type={field.type === "int" ? "number" : "text"}
                    min={field.minimum ?? undefined} max={field.maximum ?? undefined} step={field.type === "int" ? 1 : undefined}
                    disabled={busy || parametersLocked || (field.minimum != null && field.minimum === field.maximum)}
                    value={editor.parameters[field.key] ?? ""}
                    onChange={(event) => setEditor({ ...editor, parameters: { ...editor.parameters, [field.key]: event.target.value } })} />
                </label>)}
              </div>;
            })}
            </div>
          </div>
          <footer className="editor-footer">
            <span className="save-state">{dirty ? <><span className="unsaved-dot" />Unsaved changes</> : selected ? "All changes saved" : "Save a copy to create a reusable profile"}</span>
            <div className="actions">
              {selected && <button className="text-button danger" disabled={busy || activeEditor}
                onClick={() => setDialog({ title: "Delete profile", confirmLabel: "Delete profile",
                  message: `Delete “${selected.name}”? Any unsaved edits will be discarded.`, onConfirm: () => {
                    setDialog(null); void run(async () => { await api.deleteAcquisitionProfile(selected.profile_id); return CURRENT; }, "Profile deleted.");
                  } })}>Delete</button>}
              <button disabled={busy || !dirty} onClick={() => confirmDiscard(() => resetEditor(baseline))}>Discard</button>
              <button disabled={busy || !fields.length} onClick={() => setDialog({ title: "Save as new profile",
                message: "Create a separate profile using the settings shown here.", confirmLabel: "Create profile",
                initialValue: editor.name ? `${editor.name} copy` : "", onConfirm: (name) => { setDialog(null); save(true, name); } })}>Save as…</button>
              <button className="primary" disabled={busy || !fields.length || !dirty} onClick={() => save(false)}>Save profile</button>
            </div>
          </footer>
        </>}
      </div>
      {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
    </section>
  );
}
