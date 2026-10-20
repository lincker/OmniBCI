/** Plugin-owned settings, saved profiles, and explicit ordered activation. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { SchemaForm, initialSchemaValue, schemaErrors, sameParameters } from "./SchemaForm";
import { pluginConfigApi as api, selectionKey, type Activation, type BindingSelection,
  type PluginDefinition, type PluginKind, type PluginProfile } from "./api/plugin-config";
import { useI18n, translatePluginName, type TranslationKey } from "./i18n";
import "./PluginProfiles.css";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { AcquisitionMetadata } from "./AcquisitionMetadata";
import type { ProcessingInput as DecoderInput } from "./api/signal-config";
import { ProcessingInput, emptyProcessingInput, processingInputErrors } from "./ProcessingInput";
import { api as controlApi, type AcquisitionStatus, type DataSourceMetadata } from "./api/client";
import { runtimeConfigApi, type RuntimePlanRequest } from "./api/runtime-config";

type Module = "acquisition" | "processing" | "stimulation" | "actuation";
type Slot = { slot: string; kind: PluginKind; label: TranslationKey };
const slots: Record<Module, Slot[]> = {
  acquisition: [{ slot: "acquisition.source", kind: "acquisition", label: "profiles.slotSource" }],
  processing: [
    { slot: "processing.decoder", kind: "decoder", label: "profiles.slotDecoder" },
    { slot: "processing.epoch_detector", kind: "epoch_detector", label: "profiles.slotEpochDetector" },
    { slot: "processing.filters", kind: "filter", label: "profiles.slotFilters" },
  ],
  stimulation: [
    { slot: "stimulation.paradigm", kind: "paradigm", label: "profiles.slotParadigm" },
    { slot: "stimulation.trigger", kind: "trigger", label: "profiles.slotTrigger" },
  ],
  actuation: [{ slot: "actuation.effector", kind: "effector", label: "profiles.slotEffector" }],
};
type Editor = { profile: PluginProfile | null; plugin: string; name: string; parameters: Record<string, unknown>; processingInput: DecoderInput | null };
type FilterInstance = BindingSelection & { plugin: string };
const emptyEditor: Editor = { profile: null, plugin: "", name: "", parameters: {}, processingInput: null };
const connectionParameters = (parameters: Record<string, unknown>) => Object.fromEntries(
  Object.entries(parameters).filter(([key]) => key !== "sample_rate" && key !== "channel_number"));
const filterInstances = (activation: Activation | null): FilterInstance[] => activation?.bindings
  .filter((item) => item.kind === "filter").map((item) => ({ slot: item.slot,
    profile_id: item.profile_id ?? "", plugin: selectionKey(item.selection) })) ?? [];

function editorFor(profile: PluginProfile | undefined, definitions: PluginDefinition[], plugin?: string): Editor {
  if (profile) return { profile, plugin: selectionKey(profile.selection), name: profile.name,
    parameters: structuredClone(profile.parameters),
    processingInput: profile.processing_input ? { ...structuredClone(profile.processing_input),
      outputs: structuredClone(profile.processing_input.outputs ?? []) } : null };
  const definition = plugin !== undefined ? definitions.find((item) => selectionKey(item.selection) === plugin)
    : definitions.find((item) => item.schema && item.available) ?? definitions.find((item) => item.schema);
  return { profile: null, plugin: plugin ?? (definition ? selectionKey(definition.selection) : ""), name: "",
    parameters: definition?.schema ? initialSchemaValue(definition.schema) as Record<string, unknown> : {},
    processingInput: definition?.kind === "decoder" ? emptyProcessingInput() : null };
}

export function PluginProfiles({ module, engineRunning, engineAvailable = true, navigation,
  runtime, acquisitionStatus = null, onAcquisitionChange, onSelected, onDirtyChange, onBusyChange }: {
  module: Module;
  engineRunning: boolean;
  engineAvailable?: boolean;
  navigation?: ReactNode;
  runtime: RuntimeConfiguration;
  acquisitionStatus?: AcquisitionStatus | null;
  onAcquisitionChange?: () => Promise<void>;
  onSelected?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const { t } = useI18n();
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
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const [sourceStatus, setSourceStatus] = useState(acquisitionStatus);
  const [sourceMetadata, setSourceMetadata] = useState<DataSourceMetadata | null>(null);
  const sourceMode = editor.profile ? "saved" : "new";
  const metadataGeneration = useRef(0);
  const loadGeneration = useRef(0);
  const definition = definitions.find((item) => selectionKey(item.selection) === editor.plugin);
  const noTrigger = slot.kind === "trigger" && editor.plugin === "";
  const sourceParametersChanged = !!editor.profile && !sameParameters(editor.parameters, editor.profile.parameters);
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
  const selectionLabel = !engineRunning ? t("profiles.nextStart")
    : activation && runtime.status?.applied_revision === activation.revision ? t("profiles.applied") : t("profiles.selected");
  const dirty = editor.name !== baseline.name || editor.plugin !== baseline.plugin
    || !sameParameters(editor.parameters, baseline.parameters)
    || !sameParameters(editor.processingInput, baseline.processingInput);
  const errors = [...(definition?.schema ? schemaErrors(definition.schema, editor.parameters, undefined, t) : []),
    ...(slot.kind === "decoder" ? processingInputErrors(editor.processingInput, t) : [])];
  const selectedBindings = activation?.bindings.filter((item) => item.profile_id === editor.profile?.profile_id) ?? [];
  const selectedChanged = selectedBindings.some((item) => !sameParameters(item.parameters, editor.profile?.parameters)
    || selectionKey(item.selection) !== selectionKey(editor.profile!.selection)
    || !sameParameters(item.processing_input ?? null, editor.profile?.processing_input ?? null));
  const legacyDecoder = slot.kind === "decoder" && !!editor.profile && !editor.processingInput;
  const savedChain = filterInstances(activation);
  const chainDirty = JSON.stringify(chain) !== JSON.stringify(savedChain);
  const chainDirtyRef = useRef(chainDirty);
  chainDirtyRef.current = chainDirty;
  const reportedSource = ["neuroscan_curry8_reader", "file_reader"].includes(definition?.plugin_type ?? "");
  const sourceParameters = sourceMode === "new" && reportedSource ? connectionParameters(editor.parameters) : editor.parameters;
  const sourceParameterKey = JSON.stringify(sourceParameters);
  const sourceMatches = !!selectedBinding && (editor.profile ? selectedBinding.profile_id === editor.profile.profile_id
    : sourceMode === "new" && !selectedBinding.profile_id)
    && selectionKey(selectedBinding.selection) === editor.plugin
    && sameParameters(selectedBinding.parameters, sourceParameters)
    && runtime.status?.running === true && runtime.status.applied_revision === activation?.revision;
  const canReadSource = !!sourceStatus?.connected && !sourceStatus.streaming && sourceMatches
    && !sourceParametersChanged && !runtime.blocked;
  const metadataRevision = metadataGeneration.current;
  const canUseObserved = canReadSource && reportedSource && !!sourceMetadata?.descriptor
    && !!sourceMetadata.comparison && sourceMetadata.comparison.status !== "unverified"
    && sourceMetadata.connection_id === sourceStatus?.connection_id
    && sourceMetadata.descriptor.connection_id === sourceMetadata.connection_id
    && sourceMetadata.descriptor.source_id === sourceMetadata.source_id;

  useEffect(() => { setSourceStatus(acquisitionStatus); }, [acquisitionStatus]);
  useEffect(() => {
    metadataGeneration.current += 1;
    setSourceMetadata(null);
    if (sourceMode === "new" && reportedSource) {
      setEditor((current) => current.profile || !("sample_rate" in current.parameters || "channel_number" in current.parameters)
        ? current : { ...current, parameters: connectionParameters(current.parameters) });
      setNotice((current) => current === "profiles.observedDraftFilled" ? null : current);
    }
  }, [sourceMode, editor.plugin, editor.profile?.profile_id, sourceParameterKey, sourceStatus?.connected, sourceStatus?.connection_id, sourceStatus?.streaming, sourceMatches, runtime.status?.generation, runtime.blocked]);

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

  const resetEditor = (next: Editor) => {
    metadataGeneration.current += 1; setSourceMetadata(null);
    setEditor(next); setBaseline(structuredClone(next)); setNotice(null);
  };
  const reload = useCallback(async (profileId?: string, plugin?: string) => {
    const generation = ++loadGeneration.current;
    const [nextDefinitions, nextProfiles, nextActivation] = await Promise.all([
      api.catalog(slot.kind), api.profiles(slot.kind), api.activation(),
    ]);
    if (generation !== loadGeneration.current) return;
    setDefinitions(nextDefinitions); setProfiles(nextProfiles); setActivation(nextActivation);
    setChain(filterInstances(nextActivation));
    const nextBinding = nextActivation?.bindings.find((item) => item.slot === slot.slot);
    const preferredId = profileId ?? nextBinding?.profile_id;
    const candidates = nextProfiles.filter((item) => item.kind === slot.kind
      && (plugin === undefined || selectionKey(item.selection) === plugin));
    const selected = profileId === "new" ? undefined : candidates.find((item) => item.profile_id === preferredId) ?? candidates[0];
    const next = slot.kind === "trigger" && (plugin === "" || (plugin === undefined && !nextBinding))
      ? structuredClone(emptyEditor) : editorFor(selected, nextDefinitions, plugin);
    setEditor(next); setBaseline(structuredClone(next));
  }, [slot.kind, slot.slot]);

  useEffect(() => {
    let active = true;
    setBusy(true); setError(""); setNotice(null);
    void reload().catch((cause) => { if (active) setError(String(cause)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; loadGeneration.current += 1; };
  }, [reload]);

  const run = async (action: () => Promise<string | undefined>, message: TranslationKey, preserveChain = false) => {
    const pendingChain = preserveChain && chainDirty ? structuredClone(chain) : null;
    setBusy(true); setError(""); setNotice(null);
    try { const profileId = await action(); await reload(profileId, editor.plugin); if (pendingChain) setChain(pendingChain);
      setNotice(message); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const changeEditor = (action: () => void, discardChain = false) => {
    if (!dirty && (!discardChain || !chainDirty)) { action(); return; }
    setDialog({ title: t("profiles.discardPrompt"), message: discardChain
      ? t("profiles.discardChainMessage") : t("profiles.discardMessage"),
      confirmLabel: t("profiles.discardChanges"), onConfirm: () => { setDialog(null); if (discardChain) setChain(savedChain); action(); } });
  };
  const choosePlugin = (plugin: string) => changeEditor(() => {
    const candidates = profilesFor(plugin);
    const preferred = candidates.find((item) => item.profile_id === selectedBinding?.profile_id) ?? candidates[0];
    resetEditor(editorFor(preferred, definitions, plugin));
  });
  const newProfile = (plugin = editor.plugin) => changeEditor(() => {
    resetEditor(editorFor(undefined, definitions, plugin));
  });
  const bindingSelections = (): RuntimePlanRequest["bindings"] => {
    return activation?.bindings.filter((item) => item.slot.startsWith(`${module}.`)).map((item) => {
      if (item.profile_id) return { slot: item.slot, profile_id: item.profile_id };
      if (item.slot === "acquisition.source") return { slot: "acquisition.source" as const, selection: item.selection, parameters: item.parameters };
      throw new Error(t("profiles.saveEveryProfile"));
    }) ?? [];
  };
  const reviewSelection = async (makeBindings: () => RuntimePlanRequest["bindings"], refreshSlots: string[]) => {
    setBusy(true); setError(""); setNotice(null);
    try { await runtime.review({ module, bindings: makeBindings(), expected_revision: activation?.revision ?? null,
      refresh_slots: refreshSlots, include_host_settings: false }); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const persistProfile = async (copy = false, name = editor.name) => {
    if (!definition) throw new Error(t("profiles.schemaRequired"));
    const processingInput = slot.kind === "decoder" ? editor.processingInput ?? undefined : undefined;
    return editor.profile && !copy
      ? api.update(editor.profile, name, editor.parameters, processingInput)
      : api.create(slot.kind, definition.selection, name, editor.parameters, processingInput);
  };
  const save = (copy = false, name = editor.name) => {
    void run(async () => {
      const saved = await persistProfile(copy, name);
      return saved.profile_id;
    }, "profiles.saved", true);
  };
  const connectSource = async () => {
    if (busy || runtime.blocked || !definition?.available || errors.length) return;
    metadataGeneration.current += 1; setSourceMetadata(null);
    if (sourceMode === "new" && reportedSource) setEditor((current) => ({ ...current, parameters: connectionParameters(current.parameters) }));
    setBusy(true); setError(""); setNotice(null);
    try {
      const current = await controlApi.acquisitionStatus();
      if (!current) throw new Error(t("profiles.sourceUnavailable"));
      if (current.streaming) throw new Error(t("profiles.stopBeforeConnect"));
      const saved = editor.profile && dirty ? await persistProfile(false, editor.name.trim() || definition.label) : editor.profile;
      if (saved) {
        resetEditor(editorFor(saved, definitions));
        setProfiles((previous) => [...previous.filter((item) => item.profile_id !== saved.profile_id), saved]);
      }
      const selected = await api.activation();
      const sourceSelection = saved?.selection ?? definition.selection;
      const parameters = saved?.parameters ?? sourceParameters;
      const sourceBinding = saved ? { slot: "acquisition.source", profile_id: saved.profile_id }
        : { slot: "acquisition.source" as const, selection: sourceSelection, parameters };
      const applied = await runtime.applySelection({ module: "acquisition", bindings: [sourceBinding],
        expected_revision: selected?.revision ?? null, refresh_slots: ["acquisition.source"], include_host_settings: false });
      if (!applied) throw new Error(t("profiles.connectApplyFailed"));
      if (!(await runtimeConfigApi.status()).running) await controlApi.engineStart();
      const [confirmed, status] = await Promise.all([api.activation(), runtimeConfigApi.status()]);
      const source = confirmed?.bindings.find((item) => item.slot === "acquisition.source");
      if (!source || (source.profile_id ?? null) !== (saved?.profile_id ?? null) || selectionKey(source.selection) !== selectionKey(sourceSelection)
          || !sameParameters(source.parameters, parameters)
          || !status.running || status.recovery_required || status.accepted_revision !== confirmed?.revision
          || status.applied_revision !== confirmed?.revision || ["queued", "applying"].includes(status.operation?.status ?? "")) {
        throw new Error(t("profiles.sourceChanged"));
      }
      await controlApi.acqConnect();
      setSourceStatus(await controlApi.acquisitionStatus());
      if (saved) await reload(saved.profile_id, editor.plugin);
      else setActivation(confirmed);
      await onAcquisitionChange?.();
      setNotice("profiles.sourceConnected");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { await runtime.refresh(); setBusy(false); }
  };
  const disconnectSource = async () => {
    setBusy(true); setError(""); setNotice(null);
    try {
      await controlApi.acqDisconnect();
      setSourceStatus(await controlApi.acquisitionStatus());
      await onAcquisitionChange?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const readSourceMetadata = async () => {
    if (!canReadSource || busy) return;
    const generation = ++metadataGeneration.current;
    setBusy(true); setError(""); setNotice(null); setSourceMetadata(null);
    if (sourceMode === "new" && reportedSource) setEditor((current) => ({ ...current, parameters: connectionParameters(current.parameters) }));
    try {
      const metadata = await controlApi.acqMetadata();
      if (generation !== metadataGeneration.current) return;
      setSourceMetadata(metadata);
      const output = metadata?.descriptor;
      if (sourceMode === "new" && reportedSource && metadata && output
          && ["device-reported", "file-reported"].includes(output.provenance)
          && metadata.connection_id === sourceStatus?.connection_id
          && output.connection_id === metadata.connection_id && output.source_id === metadata.source_id) {
        setEditor((current) => ({ ...current, parameters: { ...current.parameters,
          sample_rate: output.sample_rate, channel_number: output.output_row_count } }));
        setNotice("profiles.observedDraftFilled");
      }
      await onAcquisitionChange?.();
    } catch (cause) { if (generation === metadataGeneration.current) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const changeParameters = (parameters: Record<string, unknown>) => {
    setEditor({ ...editor, parameters: sourceMode === "new" && reportedSource ? connectionParameters(parameters) : parameters }); setNotice(null);
  };
  const useObservedValues = () => {
    if (busy || !canUseObserved || metadataRevision !== metadataGeneration.current) return;
    const output = sourceMetadata!.descriptor!;
    setEditor((current) => ({ ...current, parameters: { ...current.parameters,
      sample_rate: output.sample_rate, channel_number: output.output_row_count } }));
    setNotice("profiles.observedValuesCopied");
  };
  const saveObservedProfile = () => {
    if (busy || !canUseObserved || !definition || metadataRevision !== metadataGeneration.current) return;
    const output = sourceMetadata!.descriptor!;
    const parameters = { ...structuredClone(editor.parameters),
      sample_rate: output.sample_rate, channel_number: output.output_row_count };
    setDialog({ title: t("metadata.saveObservedProfile"), message: t("profiles.saveObservedProfileMessage"),
      confirmLabel: t("profiles.save"),
      initialValue: t("profiles.copyName", { name: editor.name || definition.label }).slice(0, 64),
      onConfirm: (name) => {
        setDialog(null);
        if (metadataRevision !== metadataGeneration.current) {
          setError(t("profiles.observedValuesExpired"));
          return;
        }
        void run(async () => {
          const saved = await api.create("acquisition", definition.selection, name, parameters);
          return saved.profile_id;
        }, "profiles.observedProfileSaved", true);
      } });
  };
  const dimensionFields = new Set(["sample_rate", "channel_number"]);
  const sourceSchema = slot.kind === "acquisition" && definition?.schema
    ? { ...definition.schema, properties: Object.fromEntries(Object.entries(definition.schema.properties ?? {})
      .filter(([key]) => !dimensionFields.has(key))) } : definition?.schema;
  const dimensionSchema = slot.kind === "acquisition" && definition?.schema
    ? { ...definition.schema, properties: Object.fromEntries(Object.entries(definition.schema.properties ?? {})
      .filter(([key]) => dimensionFields.has(key))) } : null;
  const pluginOptions = <>
    {slot.kind === "trigger" ? <option value="">{t("profiles.noTrigger")}</option>
      : !definitions.length && !missingPlugins.length && <option value="">{t("profiles.noPlugins")}</option>}
    {definitions.map((item) => <option key={selectionKey(item.selection)} value={selectionKey(item.selection)}>
      {translatePluginName(item.plugin_type, item.label, t)} · {item.selection.version ?? t("profiles.unversioned")} · {item.selection.package_id}/{item.selection.contribution_id}
      {item.available ? "" : t("profiles.platformUnavailable")}{item.schema ? "" : t("profiles.noSchemaSuffix")}
    </option>)}
    {missingPlugins.map(([key, selection]) => <option key={key} value={key}>
      {selection.package_id}/{selection.contribution_id} · {selection.version ?? t("profiles.unversioned")} {t("profiles.unavailable")}
    </option>)}
  </>;

  const ProfileSection = slot.kind === "acquisition" ? "details" : "div";
  const ProfileHeading = slot.kind === "acquisition" ? "summary" : "h2";
  const profileAction = <button className="primary" disabled={busy || dirty || (!noTrigger && (!editor.profile || !definition?.available)) || (!engineAvailable && slot.kind !== "filter") || (runtime.blocked && slot.kind !== "filter")}
          onClick={() => {
            if (noTrigger) {
              void reviewSelection(() => bindingSelections().filter((item) => item.slot !== slot.slot), []);
              return;
            }
            if (!editor.profile) return;
            if (slot.kind === "filter") {
              setChain([...chain, { slot: `processing.filter.${crypto.randomUUID()}`, profile_id: editor.profile.profile_id, plugin: editor.plugin }]);
              setNotice("profiles.filterAdded");
            } else void reviewSelection(() => [
              ...bindingSelections().filter((item) => item.slot !== slot.slot),
              { slot: slot.slot, profile_id: editor.profile!.profile_id },
            ], [slot.slot]);
          }}>{slot.kind === "filter" ? t("profiles.addFilter") : t("profiles.reviewProfile")}</button>;

  return <section className="settings-workspace plugin-profiles">
    <aside className="explorer profile-explorer">
      {navigation}
      <div className="explorer-heading">{t("profiles.pluginConfiguration")}</div>
      {slots[module].map((item) => <button key={item.slot} disabled={busy}
        className={`explorer-item ${slot.slot === item.slot ? "selected" : ""}`}
        onClick={() => changeEditor(() => setSlotName(item.slot), true)}>{t(item.label)}</button>)}
      <div className="explorer-heading">{t("profiles.pluginProfiles")}</div>
      <div className="profile-list">
        {pluginProfiles.map((profile) => <button key={profile.profile_id}
          disabled={busy} className={`explorer-item profile-item ${editor.profile?.profile_id === profile.profile_id ? "selected" : ""}`}
          onClick={() => changeEditor(() => resetEditor(editorFor(profile, definitions)))}>
          <span>{profile.name}</span>{activation?.bindings.some((item) => item.profile_id === profile.profile_id)
            && <span className="active-indicator">{t("profiles.selected")}</span>}
        </button>)}
      </div>
      <div className="explorer-footer"><p>{t("profiles.explorerHint")}</p></div>
    </aside>
    <div className="settings-editor">
      <header className="editor-heading"><div><p className="eyebrow">{t(`profiles.module.${module}`)} / {t(slot.label)}</p>
        <h1>{slot.kind === "acquisition" ? t("profiles.acquisitionSource") : t(slot.label)}</h1><p>{t(slot.kind === "acquisition" ? "profiles.connectHint" : "profiles.chooseHint")}</p></div>
        {profileAction}
      </header>
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {notice && <p className="inline-message success" role="status">{t(notice)}</p>}
      {engineRunning && slot.kind !== "acquisition" && <p className="inline-message">{t("profiles.engineRunning")}</p>}
      {selectedBinding && <div className="binding-summary" aria-label={t("profiles.selectedConfiguration")}>
        <span>{selectionLabel}: <strong>{selectedPlugin ? translatePluginName(selectedPlugin.plugin_type, selectedPlugin.label, t) : selectedBinding.selection.package_id}</strong>
          {" / "}{selectedProfile?.name ?? selectedBinding.profile_id ?? t("profiles.newUnsaved")}
          {selectedBinding.profile_id && <> · {t("profiles.revision", { revision: selectedBinding.profile_revision })}</>}</span>
        {selectedProfile && selectedProfile.profile_id !== editor.profile?.profile_id && <button disabled={busy}
          onClick={() => changeEditor(() => resetEditor(editorFor(selectedProfile, definitions)))}>{t("profiles.viewSelected")}</button>}
      </div>}
      {slot.kind === "trigger" && activation && !selectedBinding && <div className="binding-summary" aria-label={t("profiles.selectedConfiguration")}>
        <span>{selectionLabel}: <strong>{t("profiles.noTrigger")}</strong></span>
      </div>}
      {selectedChanged && <p className="inline-message">{t("profiles.savedChanges")}</p>}
      <div className="settings-scroll">
        <div className="setting-group profile-selectors"><h2>{t("profiles.pluginAndProfile")}</h2>
          <label className="setting-row"><span className="setting-label">{t("profiles.pluginAndVersion")}</span>
            <select value={editor.plugin} disabled={busy} onChange={(event) => choosePlugin(event.target.value)}>{pluginOptions}</select></label>
          {!noTrigger && <><label className="setting-row"><span className="setting-label">{t("profiles.profile")}</span>
            <select value={editor.profile?.profile_id ?? ""} disabled={busy || !editor.plugin}
              onChange={(event) => { const profile = pluginProfiles.find((item) => item.profile_id === event.target.value);
                changeEditor(() => resetEditor(editorFor(profile, definitions, editor.plugin))); }}>
              {!editor.profile && <option value="">{t("profiles.newUnsaved")}</option>}
              {pluginProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select></label>
          <div className="profile-tools actions">
            <button disabled={busy || !definition?.schema} onClick={() => newProfile()}>{t("profiles.newProfile")}</button>
            {(slot.kind !== "acquisition" || editor.profile) && <button disabled={busy || !definition?.schema || errors.length > 0 || legacyDecoder} onClick={() => setDialog({ title: t("profiles.duplicate"),
              message: t("profiles.duplicateMessage"),
              confirmLabel: t("profiles.create"), initialValue: t("profiles.copyName", { name: editor.name || (definition ? translatePluginName(definition.plugin_type, definition.label, t) : t(slot.label)) }),
              onConfirm: (name) => { setDialog(null); save(true, name); } })}>{t("profiles.duplicate")}</button>}
          </div></>}
          {noTrigger && <p className="setting-description">{t("profiles.noTriggerHint")}</p>}
          {slot.kind === "acquisition" && <p className="setting-description">{t(sourceMode === "new"
            ? reportedSource ? "profiles.newSourceHint" : "profiles.newDeclaredSourceHint" : "profiles.savedSourceHint")}</p>}
          {legacyDecoder && <p className="setting-description">{t("profiles.configureDecoder")}</p>}
          {!noTrigger && !pluginProfiles.length && slot.kind !== "acquisition" && <p className="setting-description">{t("profiles.noSavedProfiles")}</p>}
        </div>
        {!noTrigger && <><ProfileSection className="setting-group" {...(slot.kind === "acquisition" && sourceMode === "new" ? { open: true } : {})}>
          <ProfileHeading>{editor.profile ? t("profiles.edit") : t("profiles.newProfile")}</ProfileHeading>
          <label className="setting-row"><span className="setting-label">{t("profiles.name")}</span>
            <input value={editor.name} maxLength={64} disabled={busy} onChange={(event) => {
              setEditor({ ...editor, name: event.target.value }); setNotice(null);
            }} /></label>
        </ProfileSection>
        {sourceSchema ? <SchemaForm schema={sourceSchema} value={editor.parameters} disabled={busy} onChange={changeParameters} />
          : <p className="setting-description">{t("profiles.schemaRequired")}</p>}</>}
        {dimensionSchema && Object.keys(dimensionSchema.properties).length > 0 && <details className="setting-group" open={sourceMode === "new"}>
          <summary>{t(reportedSource ? "profiles.expectedParameters" : "profiles.dimensionDefaults")}</summary>
          <p className="setting-description">{t(reportedSource ? sourceMode === "new" ? "profiles.newSourceHint" : "profiles.expectedParametersHint" : "profiles.dimensionDefaultsHint")}</p>
          {reportedSource ? <dl className="expected-source-parameters">
            <div><dt>{t("metadata.sampleRate")}</dt><dd>{String(editor.parameters.sample_rate ?? t("metadata.notConfirmed"))}</dd></div>
            <div><dt>{t("metadata.outputRows")}</dt><dd>{String(editor.parameters.channel_number ?? t("metadata.notConfirmed"))}</dd></div>
          </dl> : <SchemaForm schema={dimensionSchema} value={editor.parameters} disabled={busy} onChange={changeParameters} />}
        </details>}
        {slot.kind === "acquisition" && definition && <><div className="setting-group">
          <h2>{t("profiles.connection")}</h2>
          <p className="setting-description">{t(sourceStatus?.streaming ? "profiles.stopBeforeConnect" : sourceMode === "new" ? "profiles.connectDraft" : "profiles.connectSaves")}</p>
          <div className="actions"><button className="primary" disabled={busy || runtime.blocked || !engineAvailable || !definition.available || errors.length > 0 || !!sourceStatus?.streaming}
            onClick={() => void connectSource()}>{t("run.connect")}</button>
            <button disabled={busy || runtime.inProgress || !sourceStatus?.connected} onClick={() => void disconnectSource()}>{t("run.disconnect")}</button></div>
          <p role="status">{t(sourceStatus?.connected && sourceMatches ? "profiles.sourceConnectedStatus" : "profiles.sourceDisconnectedStatus")}</p>
        </div><AcquisitionMetadata value={sourceMetadata} busy={busy} canRead={canReadSource}
          readHint={t(sourceStatus?.streaming ? "profiles.stopBeforeRead" : canReadSource ? "profiles.readReady" : "profiles.connectBeforeRead")}
          onRead={() => void readSourceMetadata()} canUseObserved={canUseObserved}
          showComparison={sourceMode !== "new" || !reportedSource}
          onUseObserved={reportedSource && sourceMode === "saved" ? useObservedValues : undefined}
          onSaveObservedProfile={reportedSource && sourceMode === "saved" ? saveObservedProfile : undefined} /></>}
        {slot.kind === "decoder" && definition && <ProcessingInput key={`${editor.profile?.profile_id ?? editor.plugin}:${editor.profile?.revision ?? "new"}`}
          value={editor.processingInput} busy={busy}
          decoderSelection={definition.selection} channelRequirements={definition.channel_requirements}
          configurationRevision={`${runtime.status?.accepted_revision}:${runtime.status?.applied_revision}`}
          onChange={(processingInput) => { setEditor({ ...editor, processingInput }); setNotice(null); }} />}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}
        {selectedBindings.length > 0 && <p className="setting-description">{t("profiles.revisions", { selected: selectedBindings.map((item) => item.profile_revision).join(", "), saved: editor.profile?.revision ?? "" })}</p>}
        {slot.kind === "filter" && <div className="setting-group"><h2>{t("profiles.filterChain")}</h2>
          <p className="setting-row">{t("profiles.filterOrder")}</p>
          <ol className="filter-chain">{chain.map((item, index) => <li key={item.slot}>
            <label className="setting-label" htmlFor={`${item.slot}-plugin`}>{t("profiles.filterPlugin", { index: index + 1 })}</label>
            <select id={`${item.slot}-plugin`} value={item.plugin} disabled={busy} onChange={(event) => {
              const plugin = event.target.value;
              setChain(chain.map((previous) => previous.slot === item.slot
                ? { ...previous, plugin, profile_id: profilesFor(plugin)[0]?.profile_id ?? "" } : previous));
            }}>{pluginOptions}</select>
            <label className="setting-label" htmlFor={item.slot}>{t("profiles.filterProfile", { index: index + 1 })}</label>
            <select id={item.slot} value={item.profile_id} disabled={busy} onChange={(event) => setChain(chain.map((previous) =>
              previous.slot === item.slot ? { ...previous, profile_id: event.target.value } : previous))}>
              {!profiles.some((profile) => profile.profile_id === item.profile_id) && <option value={item.profile_id}>{t("profiles.profileUnavailable")}</option>}
              {profilesFor(item.plugin).map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select><div className="actions">
              {!profilesFor(item.plugin).length && <button disabled={busy} onClick={() => newProfile(item.plugin)}>{t("profiles.create")}</button>}
              <button disabled={busy || index === 0} onClick={() => { const next = [...chain]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setChain(next); }}>{t("profiles.moveUp")}</button>
              <button disabled={busy || index === chain.length - 1} onClick={() => { const next = [...chain]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; setChain(next); }}>{t("profiles.moveDown")}</button>
              <button disabled={busy} onClick={() => setChain(chain.filter((previous) => previous.slot !== item.slot))}>{t("profiles.remove")}</button>
              <button disabled={busy || !engineAvailable || runtime.blocked || chainDirty} onClick={() => void reviewSelection(bindingSelections, [item.slot])}>{t("profiles.reviewValues")}</button>
            </div>
          </li>)}</ol>
          {!chain.length && <p className="setting-row">{t("profiles.noFilters")}</p>}
          <div className="setting-row"><button className="primary" disabled={busy || !engineAvailable || runtime.blocked || !chainDirty || chain.some((item) => !item.profile_id)}
            onClick={() => void reviewSelection(() => [
              ...bindingSelections().filter((item) => !item.slot.startsWith("processing.filter.")),
              ...chain.map(({ slot, profile_id }) => ({ slot, profile_id })),
            ], [])}>{t("profiles.reviewChain")}</button></div>
        </div>}
      </div>
      <footer className="editor-footer"><span className="save-state">{noTrigger ? t("profiles.noTrigger") : dirty || chainDirty ? t("profiles.unsaved")
        : !editor.profile && definition?.schema ? t("profiles.newNotSaved") : t("profiles.allSaved")}</span>
        <div className="actions">
          <button disabled={busy} onClick={() => changeEditor(() => { void run(async () => editor.profile?.profile_id ?? "new", "profiles.refreshed"); }, true)}>{t("profiles.reload")}</button>
          {!noTrigger && <>{editor.profile && <button className="text-button danger" disabled={busy || selectedBindings.length > 0 || chain.some((item) => item.profile_id === editor.profile?.profile_id)}
            onClick={() => setDialog({ title: t("profiles.deleteProfile"), message: t("profiles.deletePrompt", { name: editor.profile!.name }), confirmLabel: t("profiles.deleteProfile"),
              onConfirm: () => { setDialog(null); void run(async () => { await api.delete(editor.profile!); return "new"; }, "profiles.deleted", true); } })}>{t("profiles.delete")}</button>}
          <button disabled={busy || (!dirty && !chainDirty)} onClick={() => {
            resetEditor(structuredClone(baseline)); setChain(savedChain);
          }}>{t("profiles.discard")}</button>
          <button className="primary" disabled={busy || !definition?.schema || errors.length > 0 || !editor.name.trim() || (!dirty && !!editor.profile)
            || (slot.kind === "acquisition" && sourceMode === "new" && reportedSource && (!editor.parameters.sample_rate || !editor.parameters.channel_number))}
            onClick={() => save()}>{t("profiles.save")}</button></>}
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
