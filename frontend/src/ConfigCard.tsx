/**
 * Host configuration editor for shared engine settings.
 * Editing settings writes settings.toml.
 * Saving host settings and applying them to the runtime are separate actions.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, type ConfigData, type ConfigEntry } from "./api/client";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import { pluginConfigApi } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";

const fieldLabels: Record<string, string> = {
  "ALGORITHM.baseline_duration": "Baseline duration (s)",
  "NEUROENGINE.queue_size": "Queue capacity",
};
function parseInput(raw: string, type: string): number {
  const value = Number(raw);
  if (!raw.trim() || !Number.isFinite(value) || (type === "int" && !Number.isSafeInteger(value))) {
    throw new Error(`Enter a valid ${type === "int" ? "integer" : "number"}.`);
  }
  return value;
}

export function ConfigCard({ navigation, preferences, runtime, onDirtyChange, onBusyChange }: {
  navigation: ReactNode; preferences?: ReactNode;
  onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void;
  runtime: RuntimeConfiguration;
}) {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const request = useRef(0);
  const dirty = Object.keys(draft).length > 0;

  const reload = useCallback(async () => {
    const id = ++request.current;
    const cfg = await api.getConfig();
    if (id !== request.current) return;
    if (!cfg) throw new Error("Host settings are unavailable");
    setConfig(cfg);
  }, []);
  useEffect(() => {
    void reload().catch((e) => setError(String(e))).finally(() => setBusy(false));
    return () => { request.current += 1; };
  }, [reload]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  const confirmDiscard = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: "Unsaved changes", message: "Discard unsaved host settings?",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); action(); } });
  };
  const run = async (action: () => Promise<void>, success: string) => {
    setBusy(true); setError(null); setNotice(null);
    try {
      await action();
      await reload();
      setDraft({});
      setNotice(success);
    } catch (e) {
      const message = e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e);
      setError(e instanceof ApiError && e.status === 409
        ? `${message}. Your edits are preserved. Refresh settings before saving again.` : message);
    }
    finally { setBusy(false); }
  };
  const save = () => {
    if (!config) return;
    const updates: Record<string, unknown> = {};
    try {
      for (const entry of config.entries) {
        const raw = draft[entry.key];
        if (fieldLabels[entry.key] && raw !== undefined) {
          try { updates[entry.key] = parseInput(raw, entry.type); }
          catch (e) { throw new Error(`${fieldLabels[entry.key]}: ${(e as Error).message}`); }
        }
      }
    } catch (e) { setError(String(e)); return; }
    void run(async () => { await api.patchConfig(updates, config.revision); },
      "Host settings saved. Review and apply them to update the runtime.");
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
  const entries = (config?.entries ?? []).filter((entry) => fieldLabels[entry.key]);
  const renderEntry = (entry: ConfigEntry) => {
    const fieldId = `setting-${entry.key}`;
    const saved = String(entry.value ?? "");
    const raw = draft[entry.key] ?? saved;
    const onChange = (raw: string) => setDraft((previous) => {
      const next = { ...previous };
      if (raw === saved) delete next[entry.key]; else next[entry.key] = raw;
      return next;
    });
    const label = fieldLabels[entry.key];
    return <div className={`setting-row ${draft[entry.key] !== undefined ? "modified" : ""}`} key={entry.key}>
      <label className="setting-label" htmlFor={fieldId}>{label}</label>
      <p className="setting-description">{entry.description}</p>
      <input id={fieldId} aria-label={label} disabled={busy} value={raw} type="number"
        step={entry.type === "int" ? 1 : "any"} onChange={(event) => onChange(event.target.value)} />
    </div>;
  };

  return <section className="settings-workspace" aria-label="General settings" onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); if (!busy && dirty && !dialog) save();
    }
  }}>
    <aside className="explorer">
      {navigation}
      <div className="explorer-footer"><span className="eyebrow">Host settings</span>
        <p>Console preferences and shared engine settings. Configure plugins in Setup.</p></div>
    </aside>
    <div className="settings-editor">
      <div className="breadcrumbs">Settings <Icon name="chevron" /> General</div>
      <header className="editor-heading"><div><p className="eyebrow">Preferences</p><h1>General settings</h1>
        <p>Preferences for the console and shared engine settings.</p></div><button className="icon-button" aria-label="Refresh settings" title="Refresh settings" disabled={busy}
          onClick={() => confirmDiscard(() => void run(async () => {}, "Settings refreshed."))}><Icon name="refresh" /></button></header>
      {notice && <p className="inline-message success" role="status">{notice}</p>}
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {config && <p className="inline-message">Save host settings, then review and apply them to the runtime.</p>}
      <div className="settings-scroll">
        {!config && <div className="empty-state"><Icon name="settings" /><h2>{busy ? "Loading settings…" : "Settings unavailable"}</h2>
          {!busy && <button onClick={() => void run(async () => {}, "Settings refreshed.")}>Try again</button>}</div>}
        {preferences}
        <div className="module-fields">
          {entries.length > 0 && <div className="setting-group" data-group="Engine">
            <h2>Engine</h2>{entries.map(renderEntry)}</div>}
        </div>
      </div>
      <footer className="editor-footer"><span className="save-state">{dirty ? <><span className="unsaved-dot" />{Object.keys(draft).length} unsaved changes</> : "All changes saved"}</span>
        <div className="actions">
          <button disabled={busy || !dirty} onClick={() => confirmDiscard(() => setDraft({}))}>Discard</button>
          <button className="primary" disabled={busy || !dirty} onClick={save}>Save settings</button>
          <button disabled={busy || dirty || runtime.blocked || !config}
            onClick={() => void reviewHostSettings()}>Review and apply host settings</button>
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
