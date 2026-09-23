/** Plugin-owned settings, saved profiles, and explicit ordered activation. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { SchemaForm, initialSchemaValue, schemaErrors, sameParameters } from "./SchemaForm";
import { pluginConfigApi as api, selectionKey, type Activation, type BindingSelection,
  type PluginDefinition, type PluginKind, type PluginProfile } from "./api/plugin-config";
import "./PluginProfiles.css";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";

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
type FilterInstance = BindingSelection & { plugin: string };
const emptyEditor: Editor = { profile: null, plugin: "", name: "", parameters: {} };
const filterInstances = (activation: Activation | null): FilterInstance[] => activation?.bindings
  .filter((item) => item.kind === "filter").map((item) => ({ slot: item.slot,
    profile_id: item.profile_id ?? "", plugin: selectionKey(item.selection) })) ?? [];

function editorFor(profile: PluginProfile | undefined, definitions: PluginDefinition[], plugin?: string): Editor {
  if (profile) return { profile, plugin: selectionKey(profile.selection), name: profile.name,
    parameters: structuredClone(profile.parameters) };
  const definition = plugin !== undefined ? definitions.find((item) => selectionKey(item.selection) === plugin)
    : definitions.find((item) => item.schema && item.available) ?? definitions.find((item) => item.schema);
  return { profile: null, plugin: plugin ?? (definition ? selectionKey(definition.selection) : ""), name: "",
    parameters: definition?.schema ? initialSchemaValue(definition.schema) as Record<string, unknown> : {} };
}

export function PluginProfiles({ module, engineRunning, engineAvailable = true, navigation,
  runtime, onSelected, onDirtyChange, onBusyChange }: {
  module: Module;
  engineRunning: boolean;
  engineAvailable?: boolean;
  navigation?: ReactNode;
  runtime: RuntimeConfiguration;
  onSelected?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [slotName, setSlotName] = useState(slots[module][0].slot);
  const slot = slots[module].find((item) => item.slot === slotName) ?? slots[module][0];
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [definitions, setDefinitions] = useState<PluginDefinition[]>([]);
  const [activation, setActivation] = useState<Activation | null>(null);
  const [chain, setChain] = useState<FilterInstance[]>([]);
  const [editor, setEditor] = useState<Editor>(emptyEditor);
  const [baseline, setBaseline] = useState<Editor>(emptyEditor);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const loadGeneration = useRef(0);
  const definition = definitions.find((item) => selectionKey(item.selection) === editor.plugin);
  const profilesFor = (plugin: string) => profiles.filter((item) => item.kind === slot.kind
    && selectionKey(item.selection) === plugin);
  const pluginProfiles = profilesFor(editor.plugin);
  const missingPlugins = [...new Map(profiles.filter((item) => item.kind === slot.kind
    && !definitions.some((definition) => selectionKey(definition.selection) === selectionKey(item.selection)))
    .map((item) => [selectionKey(item.selection), item.selection])).entries()];
  const selectedBinding = activation?.bindings.find((item) => item.slot === slot.slot);
  const selectedProfile = profiles.find((item) => item.profile_id === selectedBinding?.profile_id);
  const selectedPlugin = definitions.find((item) => selectedBinding
    && selectionKey(item.selection) === selectionKey(selectedBinding.selection));
  const selectionLabel = !engineRunning ? "Selected for next start"
    : activation && runtime.status?.applied_revision === activation.revision ? "Applied" : "Selected";
  const dirty = editor.name !== baseline.name || editor.plugin !== baseline.plugin
    || !sameParameters(editor.parameters, baseline.parameters);
  const errors = definition?.schema ? schemaErrors(definition.schema, editor.parameters) : [];
  const selectedBindings = activation?.bindings.filter((item) => item.profile_id === editor.profile?.profile_id) ?? [];
  const selectedChanged = selectedBindings.some((item) => !sameParameters(item.parameters, editor.profile?.parameters)
    || selectionKey(item.selection) !== selectionKey(editor.profile!.selection));
  const savedChain = filterInstances(activation);
  const chainDirty = JSON.stringify(chain) !== JSON.stringify(savedChain);
  const chainDirtyRef = useRef(chainDirty);
  chainDirtyRef.current = chainDirty;

  useEffect(() => {
    let active = true;
    void api.activation().then((next) => {
      if (!active) return;
      setActivation(next);
      if (!chainDirtyRef.current) setChain(filterInstances(next));
      onSelected?.();
    }).catch((cause) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [runtime.status?.accepted_revision, onSelected]);

  useEffect(() => { onDirtyChange?.(dirty || chainDirty); }, [dirty, chainDirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);

  const resetEditor = (next: Editor) => { setEditor(next); setBaseline(structuredClone(next)); setNotice(""); };
  const reload = useCallback(async (profileId?: string, plugin?: string) => {
    const generation = ++loadGeneration.current;
    const [nextDefinitions, nextProfiles, nextActivation] = await Promise.all([
      api.catalog(slot.kind), api.profiles(), api.activation(),
    ]);
    if (generation !== loadGeneration.current) return;
    setDefinitions(nextDefinitions); setProfiles(nextProfiles); setActivation(nextActivation);
    setChain(filterInstances(nextActivation));
    const preferredId = profileId ?? nextActivation?.bindings.find((item) => item.slot === slot.slot)?.profile_id;
    const candidates = nextProfiles.filter((item) => item.kind === slot.kind
      && (plugin === undefined || selectionKey(item.selection) === plugin));
    const selected = profileId === "new" ? undefined : candidates.find((item) => item.profile_id === preferredId) ?? candidates[0];
    const next = editorFor(selected, nextDefinitions, plugin);
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
    try { const profileId = await action(); await reload(profileId, editor.plugin); if (pendingChain) setChain(pendingChain); setNotice(message); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const changeEditor = (action: () => void, discardChain = false) => {
    if (!dirty && (!discardChain || !chainDirty)) { action(); return; }
    setDialog({ title: "Discard unsaved changes?", message: discardChain
      ? "The profile or filter chain has unsaved changes." : "The profile has unsaved changes.",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); if (discardChain) setChain(savedChain); action(); } });
  };
  const choosePlugin = (plugin: string) => changeEditor(() => {
    const candidates = profilesFor(plugin);
    const preferred = candidates.find((item) => item.profile_id === selectedBinding?.profile_id) ?? candidates[0];
    resetEditor(editorFor(preferred, definitions, plugin));
  });
  const newProfile = (plugin = editor.plugin) => changeEditor(() => {
    resetEditor(editorFor(undefined, definitions, plugin));
  });
  const bindingSelections = (): BindingSelection[] => {
    if (activation?.bindings.some((item) => !item.profile_id)) throw new Error("Save a profile for every selected plugin before changing the configuration.");
    return activation?.bindings.map((item) => ({ slot: item.slot, profile_id: item.profile_id! })) ?? [];
  };
  const reviewSelection = async (makeBindings: () => BindingSelection[], refreshSlots: string[]) => {
    setBusy(true); setError(""); setNotice("");
    try { await runtime.review({ bindings: makeBindings(), expected_revision: activation?.revision ?? null,
      refresh_slots: refreshSlots, include_host_settings: false }); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const save = (copy = false, name = editor.name) => {
    if (!definition) return;
    void run(async () => {
      const saved = editor.profile && !copy
        ? await api.update(editor.profile, name, editor.parameters)
        : await api.create(slot.kind, definition.selection, name, editor.parameters);
      return saved.profile_id;
    }, "Profile saved. Review and apply it to change the selected configuration.", true);
  };
  const pluginOptions = <>
    {!definitions.length && !missingPlugins.length && <option value="">No plugins available</option>}
    {definitions.map((item) => <option key={selectionKey(item.selection)} value={selectionKey(item.selection)}>
      {item.label} · {item.selection.version ?? "unversioned"} · {item.selection.package_id}/{item.selection.contribution_id}
      {item.available ? "" : " (unavailable on this platform)"}{item.schema ? "" : " (no configuration schema)"}
    </option>)}
    {missingPlugins.map(([key, selection]) => <option key={key} value={key}>
      {selection.package_id}/{selection.contribution_id} · {selection.version ?? "unversioned"} (unavailable)
    </option>)}
  </>;

  return <section className="settings-workspace plugin-profiles">
    <aside className="explorer profile-explorer">
      {navigation}
      <div className="explorer-heading">PLUGIN CONFIGURATION</div>
      {slots[module].map((item) => <button key={item.slot} disabled={busy}
        className={`explorer-item ${slot.slot === item.slot ? "selected" : ""}`}
        onClick={() => changeEditor(() => setSlotName(item.slot), true)}>{item.label}</button>)}
      <div className="explorer-heading">PROFILES FOR THIS PLUGIN</div>
      <div className="profile-list">
        {pluginProfiles.map((profile) => <button key={profile.profile_id}
          disabled={busy} className={`explorer-item profile-item ${editor.profile?.profile_id === profile.profile_id ? "selected" : ""}`}
          onClick={() => changeEditor(() => resetEditor(editorFor(profile, definitions)))}>
          <span>{profile.name}</span>{activation?.bindings.some((item) => item.profile_id === profile.profile_id)
            && <span className="active-indicator">Selected</span>}
        </button>)}
      </div>
      <div className="explorer-footer"><p>Choose a plugin, then one of its saved profiles. Manage installed packages in Extensions.</p></div>
    </aside>
    <div className="settings-editor">
      <header className="editor-heading"><div><p className="eyebrow">{module} / {slot.label}</p>
        <h1>{slot.label === "Source" ? "Acquisition source" : slot.label}</h1><p>Choose a plugin and a reusable configuration.</p></div>
        <button className="primary" disabled={busy || dirty || !editor.profile || !definition?.available || (!engineAvailable && slot.kind !== "filter") || (runtime.blocked && slot.kind !== "filter")}
          onClick={() => {
            if (!editor.profile) return;
            if (slot.kind === "filter") {
              setChain([...chain, { slot: `processing.filter.${crypto.randomUUID()}`, profile_id: editor.profile.profile_id, plugin: editor.plugin }]);
              setNotice("Filter instance added. Review the filter chain before applying this order.");
            } else void reviewSelection(() => [
              ...bindingSelections().filter((item) => item.slot !== slot.slot),
              { slot: slot.slot, profile_id: editor.profile!.profile_id },
            ], [slot.slot]);
          }}>{slot.kind === "filter" ? "Add filter instance" : "Review and apply profile"}</button>
      </header>
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {notice && <p className="inline-message success" role="status">{notice}</p>}
      {engineRunning && <p className="inline-message">Profiles can be saved while the engine runs. Review the impact before applying changes.</p>}
      {selectedBinding && <div className="binding-summary" aria-label="Selected module configuration">
        <span>{selectionLabel}: <strong>{selectedPlugin?.label ?? selectedBinding.selection.package_id}</strong>
          {" / "}{selectedProfile?.name ?? selectedBinding.profile_id ?? "Profile unavailable"} · revision {selectedBinding.profile_revision}</span>
        {selectedProfile && selectedProfile.profile_id !== editor.profile?.profile_id && <button disabled={busy}
          onClick={() => changeEditor(() => resetEditor(editorFor(selectedProfile, definitions)))}>View selected profile</button>}
      </div>}
      {selectedChanged && <p className="inline-message">This profile has saved changes that are not selected. The selected configuration retains its previous values.</p>}
      <div className="settings-scroll">
        <div className="setting-group profile-selectors"><h2>Plugin and profile</h2>
          <label className="setting-row"><span className="setting-label">Plugin and version</span>
            <select value={editor.plugin} disabled={busy} onChange={(event) => choosePlugin(event.target.value)}>{pluginOptions}</select></label>
          <label className="setting-row"><span className="setting-label">Profile</span>
            <select value={editor.profile?.profile_id ?? ""} disabled={busy || !editor.plugin}
              onChange={(event) => { const profile = pluginProfiles.find((item) => item.profile_id === event.target.value);
                changeEditor(() => resetEditor(editorFor(profile, definitions, editor.plugin))); }}>
              {!editor.profile && <option value="">New profile (not saved)</option>}
              {pluginProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select></label>
          <div className="profile-tools actions">
            <button disabled={busy || !definition?.schema} onClick={() => newProfile()}>New profile</button>
            <button disabled={busy || !definition?.schema || errors.length > 0} onClick={() => setDialog({ title: "Duplicate profile",
              message: "Save a separate copy of the current parameters. The original profile and running configuration stay unchanged.",
              confirmLabel: "Create profile", initialValue: `${editor.name || definition?.label || slot.label} copy`,
              onConfirm: (name) => { setDialog(null); save(true, name); } })}>Duplicate profile</button>
          </div>
          {!pluginProfiles.length && <p className="setting-description">No saved profiles for this plugin version. Name and save the default configuration below.</p>}
        </div>
        <div className="setting-group"><h2>{editor.profile ? "Edit profile" : "New profile"}</h2>
          <label className="setting-row"><span className="setting-label">Name</span>
            <input value={editor.name} maxLength={64} disabled={busy} onChange={(event) => {
              setEditor({ ...editor, name: event.target.value }); setNotice("");
            }} /></label>
        </div>
        {definition?.schema ? <SchemaForm schema={definition.schema} value={editor.parameters} disabled={busy}
          onChange={(parameters) => { setEditor({ ...editor, parameters }); setNotice(""); }} />
          : <p className="setting-description">An editable schema is required. Install or select the correct plugin version.</p>}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}
        {selectedBindings.length > 0 && <p className="setting-description">Selected profile revision: {selectedBindings.map((item) => item.profile_revision).join(", ")}. Saved revision: {editor.profile?.revision}.</p>}
        {slot.kind === "filter" && <div className="setting-group"><h2>Filter chain</h2>
          <p className="setting-row">Filters run in this order. Each instance has its own stable slot.</p>
          <ol className="filter-chain">{chain.map((item, index) => <li key={item.slot}>
            <label className="setting-label" htmlFor={`${item.slot}-plugin`}>Filter {index + 1} plugin</label>
            <select id={`${item.slot}-plugin`} value={item.plugin} disabled={busy} onChange={(event) => {
              const plugin = event.target.value;
              setChain(chain.map((previous) => previous.slot === item.slot
                ? { ...previous, plugin, profile_id: profilesFor(plugin)[0]?.profile_id ?? "" } : previous));
            }}>{pluginOptions}</select>
            <label className="setting-label" htmlFor={item.slot}>Filter {index + 1} profile</label>
            <select id={item.slot} value={item.profile_id} disabled={busy} onChange={(event) => setChain(chain.map((previous) =>
              previous.slot === item.slot ? { ...previous, profile_id: event.target.value } : previous))}>
              {!profiles.some((profile) => profile.profile_id === item.profile_id) && <option value={item.profile_id}>Profile unavailable</option>}
              {profilesFor(item.plugin).map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select><div className="actions">
              {!profilesFor(item.plugin).length && <button disabled={busy} onClick={() => newProfile(item.plugin)}>Create profile</button>}
              <button disabled={busy || index === 0} onClick={() => { const next = [...chain]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setChain(next); }}>Move up</button>
              <button disabled={busy || index === chain.length - 1} onClick={() => { const next = [...chain]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; setChain(next); }}>Move down</button>
              <button disabled={busy} onClick={() => setChain(chain.filter((previous) => previous.slot !== item.slot))}>Remove</button>
              <button disabled={busy || !engineAvailable || runtime.blocked || chainDirty} onClick={() => void reviewSelection(bindingSelections, [item.slot])}>Review saved values</button>
            </div>
          </li>)}</ol>
          {!chain.length && <p className="setting-row">No filters selected.</p>}
          <div className="setting-row"><button className="primary" disabled={busy || !engineAvailable || runtime.blocked || !chainDirty || chain.some((item) => !item.profile_id)}
            onClick={() => void reviewSelection(() => [
              ...bindingSelections().filter((item) => !item.slot.startsWith("processing.filter.")),
              ...chain.map(({ slot, profile_id }) => ({ slot, profile_id })),
            ], [])}>Review and apply filter chain</button></div>
        </div>}
      </div>
      <footer className="editor-footer"><span className="save-state">{dirty || chainDirty ? "Unsaved changes"
        : !editor.profile && definition?.schema ? "New profile not saved" : "All changes saved"}</span>
        <div className="actions">
          <button disabled={busy} onClick={() => changeEditor(() => { void run(async () => editor.profile?.profile_id, "Profiles refreshed."); }, true)}>Reload</button>
          {editor.profile && <button className="text-button danger" disabled={busy || selectedBindings.length > 0 || chain.some((item) => item.profile_id === editor.profile?.profile_id)}
            onClick={() => setDialog({ title: "Delete profile", message: `Delete “${editor.profile!.name}”?`, confirmLabel: "Delete profile",
              onConfirm: () => { setDialog(null); void run(async () => { await api.delete(editor.profile!); return "new"; }, "Profile deleted.", true); } })}>Delete</button>}
          <button disabled={busy || (!dirty && !chainDirty)} onClick={() => {
            resetEditor(structuredClone(baseline)); setChain(savedChain);
          }}>Discard</button>
          <button className="primary" disabled={busy || !definition?.schema || errors.length > 0 || !editor.name.trim() || (!dirty && !!editor.profile)} onClick={() => save()}>Save profile</button>
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
