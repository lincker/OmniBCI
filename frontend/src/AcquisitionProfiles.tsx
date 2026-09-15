/** Local acquisition presets. Editing a profile does not activate it. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ModuleConfig, type ModuleProfile } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";

const CURRENT = "__current__";
const NEW = "__new__";

type Editor = {
  source: string;
  plugin: string;
  name: string;
  parameters: Record<string, string>;
};

const emptyEditor: Editor = { source: NEW, plugin: "", name: "", parameters: {} };

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

export function AcquisitionProfiles({
  engineRunning,
  onDirtyChange,
  onChanged,
}: {
  engineRunning: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onChanged: () => void;
}) {
  const [config, setConfig] = useState<ModuleConfig | null>(null);
  const [profiles, setProfiles] = useState<ModuleProfile[]>([]);
  const [editor, setEditor] = useState<Editor>(emptyEditor);
  const [baseline, setBaseline] = useState<Editor>(emptyEditor);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const request = useRef(0);
  const dirty = JSON.stringify(editor) !== JSON.stringify(baseline);
  const active = config?.active_profile_id ?? null;
  const selected = profiles.find((p) => p.profile_id === editor.source);
  const fields = config?.plugins.find((p) => p.plugin_type === editor.plugin)?.fields ?? [];
  const activeEditor = selected?.profile_id === active && active !== null;
  const parametersLocked = engineRunning && activeEditor;

  const resetEditor = useCallback((next: Editor) => {
    setEditor(next);
    setBaseline(next);
  }, []);

  const reload = useCallback(async (source?: string) => {
    const id = ++request.current;
    const [configuration, list] = await Promise.allSettled([
      api.getAcquisitionConfig(), api.listAcquisitionProfiles(),
    ]);
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
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: "Unsaved changes", message: "Discard unsaved profile changes?",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); action(); } });
  };

  const run = async (action: () => Promise<string>, success: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const source = await action();
      await reload(source);
      onChanged();
      setNotice(success);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const newEditor = (plugin: string) => {
    const defaults = config?.plugins.find((p) => p.plugin_type === plugin)?.fields ?? [];
    return { source: NEW, plugin, name: "",
      parameters: Object.fromEntries(defaults.map((field) => [field.key, String(field.default)])) };
  };

  const changeEditor = (source: string) => {
    if (!config) return;
    setError(null);
    setNotice(null);
    const profile = profiles.find((p) => p.profile_id === source);
    resetEditor(profile ? profileEditor(profile) : source === CURRENT
      ? currentEditor(config) : newEditor(config.plugins[0]?.plugin_type ?? ""));
  };

  const save = (copy: boolean, name = editor.name) => {
    const parameters: Record<string, unknown> = {};
    for (const field of fields) {
      const raw = editor.parameters[field.key] ?? "";
      if (field.type === "int") {
        const value = Number(raw);
        if (!raw.trim() || !Number.isSafeInteger(value)
            || (field.minimum != null && value < field.minimum)
            || (field.maximum != null && value > field.maximum)) {
          setError(`${field.key}: enter an integer within the allowed range.`);
          return;
        }
        parameters[field.key] = value;
      } else {
        parameters[field.key] = raw;
      }
    }
    if (!name.trim()) { setError("Enter a profile name."); return; }
    void run(async () => {
      const saved = selected && !copy
        ? await api.updateAcquisitionProfile(selected.profile_id, { name, parameters })
        : await api.createAcquisitionProfile({ name, plugin_type: editor.plugin, parameters });
      if (!saved) throw new Error("The profile was not returned by the server");
      return saved.profile_id;
    }, "Profile saved. Activate it when you are ready to use it.");
  };

  return (
    <section className="card card-wide acquisition-profiles">
      <h2>Acquisition profiles</h2>
      <p className="meta">Save device and replay settings, then select a profile before starting the engine.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {config?.selection_error && <p className="error" role="alert">{config.selection_error}</p>}
      {notice && <p className="meta" role="status">{notice}</p>}
      {config && <>
        <div className="module-profile-selectors">
          <label>
            <span>Active profile</span>
            <select aria-label="Active acquisition profile" value={active ?? ""} disabled={busy || engineRunning}
              onChange={(event) => {
                const id = event.target.value || null;
                confirmDiscard(() => void run(async () => {
                  await api.selectAcquisitionProfile(id);
                  return id ?? CURRENT;
                }, "Selection saved. It will be used at the next engine start."));
              }}>
              <option value="">Legacy configuration</option>
              {active && !profiles.some((p) => p.profile_id === active)
                && <option value={active}>Unavailable profile</option>}
              {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>
                {profile.name} — {config.plugins.find((p) => p.plugin_type === profile.plugin_type)?.label ?? profile.plugin_type}
              </option>)}
            </select>
          </label>
          <label>
            <span>Edit profile</span>
            <select aria-label="Edit acquisition profile" value={editor.source} disabled={busy}
              onChange={(event) => { const source = event.target.value; confirmDiscard(() => changeEditor(source)); }}>
              <option value={CURRENT}>Current effective settings (copy)</option>
              <option value={NEW}>New profile</option>
              {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>
                {profile.name}{profile.profile_id === active ? " (active)" : ""}
              </option>)}
            </select>
          </label>
        </div>
        {engineRunning && <p className="meta">Stop the engine to switch profiles or change active parameters. Inactive profiles remain editable.</p>}
        <div className="module-profile-selectors">
          <label>
            <span>Profile name</span>
            <input aria-label="Profile name" maxLength={64} value={editor.name} disabled={busy}
              placeholder="e.g. Scan45 Lab A" onChange={(event) => setEditor({ ...editor, name: event.target.value })} />
          </label>
          <label>
            <span>Acquisition plugin</span>
            <select aria-label="Acquisition profile plugin" value={editor.plugin} disabled={busy || !!selected}
              onChange={(event) => {
                const plugin = event.target.value;
                confirmDiscard(() => resetEditor(newEditor(plugin)));
              }}>
              {!fields.length && <option value={editor.plugin}>Select a supported plugin</option>}
              {config.plugins.map((plugin) => <option key={plugin.plugin_type} value={plugin.plugin_type}>{plugin.label}</option>)}
            </select>
          </label>
        </div>
        <div className="module-profile-fields">
          {fields.map((field) => <label key={field.key}>
            <span>{field.label}</span>
            <input aria-label={field.label} type={field.type === "int" ? "number" : "text"}
              min={field.minimum ?? undefined} max={field.maximum ?? undefined} step={field.type === "int" ? 1 : undefined}
              disabled={busy || parametersLocked || (field.minimum != null && field.minimum === field.maximum)}
              value={editor.parameters[field.key] ?? ""}
              onChange={(event) => setEditor({ ...editor,
                parameters: { ...editor.parameters, [field.key]: event.target.value } })} />
            <small className="meta">{field.description}</small>
          </label>)}
        </div>
        <div className="actions">
          <button disabled={busy || !fields.length || !dirty} onClick={() => save(false)}>Save profile</button>
          <button disabled={busy || !fields.length} onClick={() => setDialog({
            title: "Save as new", message: "Create a separate profile with all parameters currently shown.",
            confirmLabel: "Create profile", initialValue: editor.name ? `${editor.name} copy` : "",
            onConfirm: (name) => { setDialog(null); save(true, name); },
          })}>Save as new</button>
          <button disabled={busy || !dirty} onClick={() => confirmDiscard(() => resetEditor(baseline))}>Discard changes</button>
          <button disabled={busy} onClick={() => {
            confirmDiscard(() => void run(async () => editor.source, "Profiles refreshed."));
          }}>Refresh</button>
          <button className="danger" disabled={busy || !selected || activeEditor}
            onClick={() => {
              if (selected) setDialog({ title: "Delete profile", confirmLabel: "Delete",
                message: `Delete profile "${selected.name}"? Any unsaved edits will be discarded.`,
                onConfirm: () => {
                  setDialog(null);
                  void run(async () => {
                    await api.deleteAcquisitionProfile(selected.profile_id);
                    return CURRENT;
                  }, "Profile deleted.");
                },
              });
            }}>Delete profile</button>
          {dirty && <span className="meta">Unsaved changes</span>}
        </div>
      </>}
      {!config && !error && <p className="meta">Loading profiles…</p>}
      {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
    </section>
  );
}
