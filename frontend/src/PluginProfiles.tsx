/** Plugin-owned settings, saved profiles, and explicit ordered activation. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { SchemaForm, initialSchemaValue, schemaErrors, sameParameters } from "./SchemaForm";
import { pluginConfigApi as api, selectionKey, type Activation, type BindingSelection,
  type PluginDefinition, type PluginKind, type PluginProfile } from "./api/plugin-config";
import "./PluginProfiles.css";

type Module = "acquisition" | "processing" | "stimulation" | "actuation";
type Slot = { slot: string; kind: PluginKind; label: string };
const slots: Record<Module, Slot[]> = {
  acquisition: [{ slot: "acquisition.source", kind: "acquisition", label: "Source" }],
  processing: [
    { slot: "processing.decoder", kind: "decoder", label: "Decoder" },
    { slot: "processing.epoch_detector", kind: "epoch_detector", label: "Epoch detector" },
    { slot: "processing.filters", kind: "filter", label: "Filters" },
  ],
  stimulation: [
    { slot: "stimulation.paradigm", kind: "paradigm", label: "Paradigm" },
    { slot: "stimulation.trigger", kind: "trigger", label: "Trigger" },
  ],
  actuation: [{ slot: "actuation.effector", kind: "effector", label: "Effector" }],
};
type Editor = { profile: PluginProfile | null; plugin: string; name: string; parameters: Record<string, unknown> };
const emptyEditor: Editor = { profile: null, plugin: "", name: "", parameters: {} };

function editorFor(profile: PluginProfile | undefined, definitions: PluginDefinition[]): Editor {
  if (profile) return { profile, plugin: selectionKey(profile.selection), name: profile.name,
    parameters: structuredClone(profile.parameters) };
  const definition = definitions.find((item) => item.schema && item.available) ?? definitions.find((item) => item.schema);
  return { profile: null, plugin: definition ? selectionKey(definition.selection) : "", name: "",
    parameters: definition?.schema ? initialSchemaValue(definition.schema) as Record<string, unknown> : {} };
}

export function PluginProfiles({ module, engineRunning, engineAvailable = true, navigation,
  onSelected, onDirtyChange, onBusyChange }: {
  module: Module;
  engineRunning: boolean;
  engineAvailable?: boolean;
  navigation?: ReactNode;
  onSelected?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [slotName, setSlotName] = useState(slots[module][0].slot);
  const slot = slots[module].find((item) => item.slot === slotName) ?? slots[module][0];
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [definitions, setDefinitions] = useState<PluginDefinition[]>([]);
  const [activation, setActivation] = useState<Activation | null>(null);
  const [chain, setChain] = useState<BindingSelection[]>([]);
  const [editor, setEditor] = useState<Editor>(emptyEditor);
  const [baseline, setBaseline] = useState<Editor>(emptyEditor);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const loadGeneration = useRef(0);
  const definition = definitions.find((item) => selectionKey(item.selection) === editor.plugin);
  const dirty = editor.name !== baseline.name || editor.plugin !== baseline.plugin
    || !sameParameters(editor.parameters, baseline.parameters);
  const errors = definition?.schema ? schemaErrors(definition.schema, editor.parameters) : [];
  const selectedBindings = activation?.bindings.filter((item) => item.profile_id === editor.profile?.profile_id) ?? [];
  const selectedChanged = selectedBindings.some((item) => !sameParameters(item.parameters, editor.profile?.parameters)
    || selectionKey(item.selection) !== selectionKey(editor.profile!.selection));
  const savedChain = activation?.bindings.filter((item) => item.kind === "filter")
    .map((item) => ({ slot: item.slot, profile_id: item.profile_id ?? "" })) ?? [];
  const chainDirty = JSON.stringify(chain) !== JSON.stringify(savedChain);

  useEffect(() => { onDirtyChange?.(dirty || chainDirty); }, [dirty, chainDirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);

  const resetEditor = (next: Editor) => { setEditor(next); setBaseline(structuredClone(next)); };
  const reload = useCallback(async (profileId?: string) => {
    const generation = ++loadGeneration.current;
    const [nextDefinitions, nextProfiles, nextActivation] = await Promise.all([
      api.catalog(slot.kind), api.profiles(), api.activation(),
    ]);
    if (generation !== loadGeneration.current) return;
    setDefinitions(nextDefinitions); setProfiles(nextProfiles); setActivation(nextActivation);
    setChain(nextActivation?.bindings.filter((item) => item.kind === "filter")
      .map((item) => ({ slot: item.slot, profile_id: item.profile_id ?? "" })) ?? []);
    const preferredId = profileId ?? nextActivation?.bindings.find((item) => item.slot === slot.slot)?.profile_id;
    const selected = nextProfiles.find((item) => item.kind === slot.kind && item.profile_id === preferredId)
      ?? (profileId === "new" ? undefined : nextProfiles.find((item) => item.kind === slot.kind));
    const next = editorFor(selected, nextDefinitions);
    setEditor(next); setBaseline(structuredClone(next));
  }, [slot.kind, slot.slot]);

  useEffect(() => {
    let active = true;
    setBusy(true); setError(""); setNotice("");
    void reload().catch((cause) => { if (active) setError(String(cause)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; loadGeneration.current += 1; };
  }, [reload]);

  const run = async (action: () => Promise<string | undefined>, message: string, preserveChain = false) => {
    const pendingChain = preserveChain && chainDirty ? structuredClone(chain) : null;
    setBusy(true); setError(""); setNotice("");
    try { const profileId = await action(); await reload(profileId); if (pendingChain) setChain(pendingChain); setNotice(message); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const changeEditor = (action: () => void) => {
    if (!dirty && !chainDirty) { action(); return; }
    setDialog({ title: "Discard unsaved changes?", message: "The profile or filter order has unsaved changes.",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); setChain(savedChain); action(); } });
  };
  const bindingSelections = (): BindingSelection[] => {
    if (activation?.bindings.some((item) => !item.profile_id)) throw new Error("Save a profile for every selected plugin before changing the configuration.");
    return activation?.bindings.map((item) => ({ slot: item.slot, profile_id: item.profile_id! })) ?? [];
  };
  const accept = async (bindings: BindingSelection[], refreshSlots: string[]) => {
    if (engineRunning) throw new Error("Stop the engine before selecting this configuration.");
    await api.accept(bindings, activation?.revision ?? null, refreshSlots);
    onSelected?.();
    return editor.profile?.profile_id;
  };
  const save = (copy = false, name = editor.name) => {
    if (!definition) return;
    void run(async () => {
      const saved = editor.profile && !copy
        ? await api.update(editor.profile, name, editor.parameters)
        : await api.create(slot.kind, definition.selection, name, editor.parameters);
      return saved.profile_id;
    }, "Profile saved. Select it explicitly to use these values.", true);
  };

  return <section className="settings-workspace plugin-profiles">
    <aside className="explorer profile-explorer">
      {navigation}
      <div className="explorer-heading">PLUGIN CONFIGURATION</div>
      {slots[module].map((item) => <button key={item.slot} disabled={busy}
        className={`explorer-item ${slot.slot === item.slot ? "selected" : ""}`}
        onClick={() => changeEditor(() => setSlotName(item.slot))}>{item.label}</button>)}
      <div className="explorer-heading">SAVED PROFILES</div>
      <div className="profile-list">
        {profiles.filter((item) => item.kind === slot.kind).map((profile) => <button key={profile.profile_id}
          disabled={busy} className={`explorer-item profile-item ${editor.profile?.profile_id === profile.profile_id ? "selected" : ""}`}
          onClick={() => changeEditor(() => resetEditor(editorFor(profile, definitions)))}>
          <span>{profile.name}</span>{activation?.bindings.some((item) => item.profile_id === profile.profile_id)
            && <span className="active-indicator">Selected</span>}
        </button>)}
      </div>
      <div className="explorer-footer"><button disabled={busy} onClick={() => changeEditor(() => resetEditor(editorFor(undefined, definitions)))}>New profile</button>
        <p>Save reusable values, then choose when to use them.</p></div>
    </aside>
    <div className="settings-editor">
      <header className="editor-heading"><div><p className="eyebrow">{module} / {slot.label}</p>
        <h1>{editor.profile?.name ?? "New profile"}</h1><p>Settings provided by the selected plugin.</p></div>
        <button className="primary" disabled={busy || dirty || !editor.profile || !definition?.available || (!engineAvailable && slot.kind !== "filter") || (engineRunning && slot.kind !== "filter")}
          onClick={() => {
            if (!editor.profile) return;
            if (slot.kind === "filter") {
              setChain([...chain, { slot: `processing.filter.${crypto.randomUUID()}`, profile_id: editor.profile.profile_id }]);
              setNotice("Filter instance added. Use the filter chain to select this order.");
            } else void run(() => accept([
              ...bindingSelections().filter((item) => item.slot !== slot.slot),
              { slot: slot.slot, profile_id: editor.profile!.profile_id },
            ], [slot.slot]), "Profile selected for the next engine start.");
          }}>{slot.kind === "filter" ? "Add filter instance" : "Use saved profile"}</button>
      </header>
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {notice && <p className="inline-message success" role="status">{notice}</p>}
      {engineRunning && <p className="inline-message">Profiles can be saved while the engine runs. Stop the engine to select a configuration.</p>}
      {selectedChanged && <p className="inline-message">This profile has saved changes that are not selected. The selected configuration retains its previous values.</p>}
      <div className="settings-scroll">
        <div className="setting-group"><h2>Profile</h2>
          <label className="setting-row"><span className="setting-label">Name</span>
            <input value={editor.name} maxLength={64} disabled={busy} onChange={(event) => setEditor({ ...editor, name: event.target.value })} /></label>
          <label className="setting-row"><span className="setting-label">Plugin and version</span>
            <select value={editor.plugin} disabled={busy || !!editor.profile} onChange={(event) => {
              const next = definitions.find((item) => selectionKey(item.selection) === event.target.value);
              setEditor({ ...editor, plugin: event.target.value, parameters: next?.schema ? initialSchemaValue(next.schema) as Record<string, unknown> : {} });
            }}>
              {!definition && <option value={editor.plugin}>{editor.plugin ? "Selected version unavailable" : "Select a plugin"}</option>}
              {definitions.map((item) => <option key={selectionKey(item.selection)} value={selectionKey(item.selection)} disabled={!item.schema}>
                {item.label} · {item.selection.package_id}/{item.selection.contribution_id} · {item.selection.version ?? "unversioned"}{item.available ? "" : " (unavailable on this platform)"}
              </option>)}
            </select></label>
        </div>
        {definition?.schema ? <SchemaForm schema={definition.schema} value={editor.parameters} disabled={busy}
          onChange={(parameters) => setEditor({ ...editor, parameters })} />
          : <p className="setting-description">An editable schema is required. Install or select the correct plugin version.</p>}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}
        {selectedBindings.length > 0 && <p className="setting-description">Selected profile revision: {selectedBindings.map((item) => item.profile_revision).join(", ")}. Saved revision: {editor.profile?.revision}.</p>}
        {slot.kind === "filter" && <div className="setting-group"><h2>Filter chain</h2>
          <p className="setting-row">Filters run in this order. Each instance has its own stable slot.</p>
          <ol className="filter-chain">{chain.map((item, index) => <li key={item.slot}>
            <label className="setting-label" htmlFor={item.slot}>Filter {index + 1}</label>
            <select id={item.slot} value={item.profile_id} disabled={busy} onChange={(event) => setChain(chain.map((previous) =>
              previous.slot === item.slot ? { ...previous, profile_id: event.target.value } : previous))}>
              {!profiles.some((profile) => profile.profile_id === item.profile_id) && <option value={item.profile_id}>Profile unavailable</option>}
              {profiles.filter((profile) => profile.kind === "filter").map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select><div className="actions">
              <button disabled={busy || index === 0} onClick={() => { const next = [...chain]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setChain(next); }}>Move up</button>
              <button disabled={busy || index === chain.length - 1} onClick={() => { const next = [...chain]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; setChain(next); }}>Move down</button>
              <button disabled={busy} onClick={() => setChain(chain.filter((previous) => previous.slot !== item.slot))}>Remove</button>
              <button disabled={busy || !engineAvailable || engineRunning || chainDirty} onClick={() => void run(() => accept(bindingSelections(), [item.slot]), "Saved values selected for this filter instance.")}>Use saved values</button>
            </div>
          </li>)}</ol>
          {!chain.length && <p className="setting-row">No filters selected.</p>}
          <div className="setting-row"><button className="primary" disabled={busy || !engineAvailable || engineRunning || !chainDirty || chain.some((item) => !item.profile_id)}
            onClick={() => void run(() => accept([
              ...bindingSelections().filter((item) => !item.slot.startsWith("processing.filter.")), ...chain,
            ], []), "Filter chain selected. Existing instances retain their selected values.")}>Use filter chain</button></div>
        </div>}
      </div>
      <footer className="editor-footer"><span className="save-state">{dirty || chainDirty ? "Unsaved changes" : "All changes saved"}</span>
        <div className="actions">
          <button disabled={busy} onClick={() => changeEditor(() => { void run(async () => editor.profile?.profile_id, "Profiles refreshed."); })}>Reload</button>
          {editor.profile && <button className="text-button danger" disabled={busy || selectedBindings.length > 0 || chain.some((item) => item.profile_id === editor.profile?.profile_id)}
            onClick={() => setDialog({ title: "Delete profile", message: `Delete “${editor.profile!.name}”?`, confirmLabel: "Delete profile",
              onConfirm: () => { setDialog(null); void run(async () => { await api.delete(editor.profile!); return "new"; }, "Profile deleted."); } })}>Delete</button>}
          <button disabled={busy || !dirty} onClick={() => resetEditor(structuredClone(baseline))}>Discard</button>
          <button disabled={busy || !definition?.schema || errors.length > 0} onClick={() => setDialog({ title: "Save as new profile",
            message: "Create a separate saved profile with these parameters.", confirmLabel: "Create profile", initialValue: `${editor.name || slot.label} copy`,
            onConfirm: (name) => { setDialog(null); save(true, name); } })}>Save as…</button>
          <button className="primary" disabled={busy || !definition?.schema || errors.length > 0 || !editor.name.trim() || (!dirty && !!editor.profile)} onClick={() => save()}>Save profile</button>
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
