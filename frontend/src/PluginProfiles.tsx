/** Plugin-owned settings, saved profiles, and explicit ordered activation. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { SchemaForm, initialSchemaValue, schemaErrors, sameParameters, normalizedParameters } from "./SchemaForm";
import { pluginConfigApi as api, selectionKey, type ModuleActivation,
  type PluginDefinition, type PluginKind, type PluginProfile } from "./api/plugin-config";
import { useI18n, translatePluginName, type TranslationKey } from "./i18n";
import "./PluginProfiles.css";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { ConfigurationHistory } from "./ConfigurationHistory";
import { AcquisitionMetadata } from "./AcquisitionMetadata";
import { ProcessingInput, emptyProcessingInput, processingInputErrors } from "./ProcessingInput";
import { api as controlApi, type DataSourceMetadata } from "./api/client";
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";
import type { AcquisitionController } from "./hooks/useAcquisition";
import { currentFromBinding, emptyCurrentConfiguration, type CurrentConfigurationController, type CurrentPluginConfiguration } from "./hooks/useCurrentConfiguration";

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
type Editor = CurrentPluginConfiguration;
type FilterInstance = Editor & { slot: string };
const emptyEditor = emptyCurrentConfiguration;
const observedFields = new Set(["sample_rate", "channel_count", "channel_labels"]);
const connectionParameters = (parameters: Record<string, unknown>) => Object.fromEntries(
  Object.entries(parameters).filter(([key]) => !observedFields.has(key)));
const filterInstances = (activation: ModuleActivation | null): FilterInstance[] => activation?.bindings
  .filter((item) => item.kind === "filter").map((item) => ({ slot: item.slot, ...currentFromBinding(item) })) ?? [];

function focusSection(id: string) {
  if (typeof document === "undefined") return;
  const section = document.getElementById(id);
  section?.focus({ preventScroll: true });
  section?.scrollIntoView({ block: "start" });
}

function observedParameters(metadata: DataSourceMetadata, pluginType: string | undefined): Record<string, unknown> {
  const output = metadata.descriptor!;
  const parameters: Record<string, unknown> = { sample_rate: output.sample_rate, channel_count: output.output_row_count };
  if (pluginType === "neuroscan_curry8_reader") {
    try {
      const labels: unknown = JSON.parse(metadata.metadata)?.channel_labels;
      if (Array.isArray(labels) && labels.length === output.output_row_count && labels.every((label) => typeof label === "string")) {
        parameters.channel_labels = labels;
      }
    } catch { /* Source text without valid JSON has no importable labels. */ }
  }
  return parameters;
}

function editorFor(profile: PluginProfile | undefined, definitions: PluginDefinition[], plugin?: string, profiles: PluginProfile[] = []): Editor {
  if (profile) return { sourceOrigin: "profile", profile, selection: structuredClone(profile.selection),
    configurationVersion: profile.configuration_version,
    schema: definitions.find((item) => selectionKey(item.selection) === selectionKey(profile.selection))?.schema,
    requiresProcessingInputReview: profile.kind === "decoder" && (!profile.processing_input), plugin: selectionKey(profile.selection), name: profile.name,
    parameters: structuredClone(profile.parameters),
    processingInput: profile.processing_input ? { ...structuredClone(profile.processing_input),
      outputs: structuredClone(profile.processing_input.outputs ?? []) } : null };
  const definition = plugin !== undefined ? definitions.find((item) => selectionKey(item.selection) === plugin)
    : definitions.find((item) => item.schema && item.available) ?? definitions.find((item) => item.schema);
  const defaults = definition?.schema ? initialSchemaValue(definition.schema) as Record<string, unknown> : {};
  const pluginKey = plugin ?? (definition ? selectionKey(definition.selection) : "");
  const names = new Set(profiles.filter((item) => item.kind === definition?.kind
    && selectionKey(item.selection) === pluginKey).map((item) => item.name));
  let index = 1;
  while (names.has(`new profile ${index}`)) index += 1;
  return { sourceOrigin: "new", profile: null, selection: definition?.selection ?? null,
    configurationVersion: definition?.configuration_version ?? undefined, schema: definition?.schema, plugin: pluginKey, name: `new profile ${index}`,
    parameters: ["file_reader", "neuroscan_curry8_reader"].includes(definition?.plugin_type ?? "")
      ? connectionParameters(defaults) : defaults,
    processingInput: definition?.kind === "decoder" ? emptyProcessingInput() : null };
}

export function PluginProfiles({ module, engineRunning, engineAvailable = true, navigation,
  runtime, acquisition, current, onBusyChange, onRun }: {
  module: Module;
  engineRunning: boolean;
  engineAvailable?: boolean;
  navigation?: ReactNode;
  runtime: RuntimeConfiguration;
  current: CurrentConfigurationController;
  acquisition: AcquisitionController;
  onBusyChange?: (busy: boolean) => void;
  onRun?: () => void;
}) {
  const { t } = useI18n();
  const [slotName, setSlotName] = useState(slots[module][0].slot);
  const slot = slots[module].find((item) => item.slot === slotName) ?? slots[module][0];
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [definitions, setDefinitions] = useState<PluginDefinition[]>([]);
  const [activation, setActivation] = useState<ModuleActivation | null>(null);
  const [filterSlot, setFilterSlot] = useState<string | null>(null);
  const editorSlot = slot.kind === "filter" && filterSlot && current.filterOrder.includes(filterSlot) ? filterSlot : slot.slot;
  const editor = current.entries[editorSlot] ?? emptyEditor;
  const setEditor = (next: Editor | ((previous: Editor) => Editor)) => current.set(editorSlot, typeof next === "function" ? next(editor) : next);
  const chain: FilterInstance[] = current.filterOrder.map((slot) => ({ ...current.entries[slot], slot }));
  const setChain = (next: FilterInstance[]) => {
    next.forEach(({ slot, ...value }) => current.set(slot, value));
    current.setFilterOrder(next.map((item) => item.slot));
  };
  const [localBusy, setBusy] = useState(true);
  const currentState = current.modules[module];
  const runtimeState = runtime.state(module);
  const busy = localBusy || module === "acquisition" && acquisition.pending || (!currentState.ready && !currentState.error);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const sourceStatus = !engineAvailable || acquisition.error ? null : acquisition.data;
  const [sourceMetadata, setSourceMetadata] = useState<DataSourceMetadata | null>(null);
  const [libraryExpanded, setLibraryExpanded] = useState(false);
  const [profileQuery, setProfileQuery] = useState("");
  const pendingFocus = useRef<string | null>(null);
  const saveFocus = useRef<HTMLElement | null>(null);
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const sourceMode = editor.sourceOrigin ?? (editor.profile ? "profile" : "new");
  const metadataGeneration = useRef(0);
  const sourceOperation = useRef(false);
  const loadGeneration = useRef(0);
  const definition = definitions.find((item) => selectionKey(item.selection) === editor.plugin);
  const noTrigger = slot.kind === "trigger" && editor.plugin === "";
  const profilesFor = (plugin: string) => profiles.filter((item) => item.kind === slot.kind
    && selectionKey(item.selection) === plugin);
  const pluginProfiles = profilesFor(editor.plugin);
  const displayedProfiles = pluginProfiles.filter((item) => `${item.name} ${item.profile_id}`
    .toLowerCase().includes(profileQuery.trim().toLowerCase()));
  const profileHidden = !!editor.profile && !displayedProfiles.some((item) => item.profile_id === editor.profile!.profile_id);
  const missingPlugins = [...new Map([
    ...profiles.filter((item) => item.kind === slot.kind).map((item) => item.selection),
    ...(editor.selection ? [editor.selection] : []),
  ].filter((selection) => !definitions.some((definition) => selectionKey(definition.selection) === selectionKey(selection)))
    .map((selection) => [selectionKey(selection), selection])).entries()];
  const selectedBinding = activation?.bindings.find((item) => item.slot === editorSlot);
  const selectedProfile = profiles.find((item) => item.profile_id === selectedBinding?.profile_id);
  const selectedPlugin = definitions.find((item) => selectedBinding
    && selectionKey(item.selection) === selectionKey(selectedBinding.selection));
  const selectionLabel = !engineRunning ? t("profiles.nextStart")
    : activation && runtime.status?.applied?.[module] === activation.revision ? t("profiles.applied") : t("profiles.selected");
  const baseline = editor.profile ? editorFor(editor.profile, definitions) : emptyEditor;
  const dirty = !!editor.profile && (!sameParameters(editor.parameters, baseline.parameters)
    || !sameParameters(editor.processingInput, baseline.processingInput));
  const errors = [...(definition?.schema ? schemaErrors(definition.schema, editor.parameters, undefined, t) : []),
    ...(slot.kind === "decoder" ? processingInputErrors(editor.processingInput, t) : [])];
  const missingDecoderInput = slot.kind === "decoder" && (editor.requiresProcessingInputReview || !editor.processingInput);
  const moduleValidation = current.validationError(module);
  const savedChain = filterInstances(activation);
  const chainDirty = !sameParameters(chain.map(({ slot, selection, parameters }) => ({ slot, selection, parameters })),
    savedChain.map(({ slot, selection, parameters }) => ({ slot, selection, parameters })));
  const reportedSource = ["neuroscan_curry8_reader", "file_reader"].includes(definition?.plugin_type ?? "");
  const sourceParameters = editor.parameters;
  const sourceParameterKey = JSON.stringify(sourceParameters);
  const sourceSelected = !!selectedBinding && runtime.status?.accepted[module] === activation?.revision
    && selectionKey(selectedBinding.selection) === editor.plugin
    && sameParameters(selectedBinding.parameters, normalizedParameters(sourceParameters, definition?.schema));
  const sourceMatches = sourceSelected && runtime.status?.running === true
    && runtime.status.applied?.[module] === activation?.revision
    && runtime.status.applied?.engine === runtime.status.accepted.engine;
  const canReadSource = !!sourceStatus?.connected && !sourceStatus.streaming && sourceMatches
    && !runtimeState.blocked;
  const metadataRevision = metadataGeneration.current;
  const canUseObserved = canReadSource && reportedSource && !!sourceMetadata?.descriptor
    && !!sourceMetadata.comparison && sourceMetadata.comparison.status !== "unverified"
    && sourceMetadata.connection_id === sourceStatus?.connection_id
    && sourceMetadata.descriptor.connection_id === sourceMetadata.connection_id
    && sourceMetadata.descriptor.source_id === sourceMetadata.source_id;

  useEffect(() => {
    metadataGeneration.current += 1;
    setSourceMetadata(null);
  }, [sourceMode, editor.plugin, editor.profile?.profile_id, sourceParameterKey, sourceStatus?.connected, sourceStatus?.connection_id, sourceStatus?.streaming, sourceMatches, runtime.status?.generation, runtimeState.blocked]);

  useEffect(() => {
    let active = true;
    void api.module(module).then((next) => {
      if (!active) return;
      setActivation(next);
    }).catch((cause) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [runtime.status?.accepted[module], runtimeState.operation?.operation_id, runtimeState.operation?.status,
    runtimeState.completedOperation?.operation_id, slot.kind]);

  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => { onBusyChange?.(false); }, [onBusyChange]);

  const resetEditor = (next: Editor) => {
    metadataGeneration.current += 1; setSourceMetadata(null);
    setEditor({ ...next, schema: definitions.find((item) => selectionKey(item.selection) === next.plugin)?.schema }); setNotice(null);
  };
  const reload = useCallback(async () => {
    const generation = ++loadGeneration.current;
    const [nextDefinitions, nextProfiles, nextActivation] = await Promise.all([
      api.catalog(slot.kind), api.profiles(slot.kind), api.module(module),
    ]);
    if (generation !== loadGeneration.current) return;
    setDefinitions(nextDefinitions); setProfiles(nextProfiles); setActivation(nextActivation);
    if (!currentState.ready) return;
    const existing = current.get(slot.slot);
    if (existing) {
      const definition = nextDefinitions.find((item) => selectionKey(item.selection) === existing.plugin);
      if (definition) current.set(slot.slot, { ...existing, schema: definition.schema });
      if (slot.kind === "filter") for (const filterSlot of current.filterOrder) {
        const filter = current.get(filterSlot);
        const filterDefinition = nextDefinitions.find((item) => selectionKey(item.selection) === filter?.plugin);
        if (filter && filterDefinition) current.set(filterSlot, { ...filter, schema: filterDefinition.schema });
      }
      return;
    }
    const binding = nextActivation?.bindings.find((item) => item.slot === slot.slot);
    current.set(slot.slot, binding ? { ...currentFromBinding(binding), schema: nextDefinitions.find((item) => selectionKey(item.selection) === selectionKey(binding.selection))?.schema }
      : slot.kind === "trigger" ? structuredClone(emptyEditor) : editorFor(undefined, nextDefinitions, undefined, nextProfiles));
  }, [slot.kind, slot.slot, currentState.ready]);

  useEffect(() => {
    let active = true;
    setBusy(true); setError(""); setNotice(null);
    void reload().catch((cause) => { if (active) setError(String(cause)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; loadGeneration.current += 1; };
  }, [reload]);

  useEffect(() => {
    if (busy || dialog || !pendingFocus.current || typeof document === "undefined") return;
    focusSection(pendingFocus.current);
    pendingFocus.current = null;
  }, [busy, slot.slot, dialog]);

  useEffect(() => {
    if (busy || !saveFocus.current) return;
    const field = saveFocus.current;
    saveFocus.current = null;
    if (field.isConnected && field.getClientRects().length) field.focus();
    else editorHeading.current?.focus();
  }, [busy]);

  const changeEditor = (action: () => void) => {
    const original = selectedBinding ? currentFromBinding(selectedBinding) : editorFor(undefined, definitions, editor.plugin, profiles);
    const changed = dirty || (!editor.profile && (editor.plugin !== original.plugin
      || !sameParameters(editor.parameters, original.parameters) || !sameParameters(editor.processingInput, original.processingInput)));
    if (!changed) { action(); return; }
    setDialog({ title: t("profiles.discardPrompt"), message: t("profiles.discardMessage"),
      cancelLabel: t("profiles.keepEditing"), confirmLabel: t("profiles.discardChanges"),
      onConfirm: () => { setDialog(null); action(); } });
  };
  const choosePlugin = (plugin: string) => changeEditor(() => resetEditor(editorFor(undefined, definitions, plugin, profiles)));
  const newProfile = (plugin = editor.plugin) => changeEditor(() => resetEditor(editorFor(undefined, definitions, plugin, profiles)));
  const openProcessingStep = (target: string, section: string) => {
    if (busy) return;
    if (target === slot.slot) {
      focusSection(section);
      return;
    }
    pendingFocus.current = section; setBusy(true); setSlotName(target);
  };
  const save = async (name: string) => {
    setBusy(true); setError(""); setNotice(null);
    try {
      if (!definition) throw new Error(t("profiles.schemaRequired"));
      const saved = await api.create(slot.kind, definition.selection, name, editor.parameters,
        slot.kind === "decoder" ? editor.processingInput ?? undefined : undefined);
      setProfiles((current) => [...current, saved]);
      if (current.get(editorSlot) === editor) resetEditor(editorFor(saved, definitions));
      setNotice("profiles.saved");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const connectSource = async () => {
    if (sourceOperation.current || connectReason) return;
    sourceOperation.current = true;
    metadataGeneration.current += 1; setSourceMetadata(null);
    setBusy(true); setError(""); setNotice(null);
    try { await acquisition.connect.run(current.request("acquisition")); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { sourceOperation.current = false; setBusy(false); }
  };
  const disconnectSource = async () => {
    if (sourceOperation.current || disconnectReason) return;
    sourceOperation.current = true;
    setBusy(true); setError(""); setNotice(null);
    try { await acquisition.disconnect.run(); }
    finally { sourceOperation.current = false; setBusy(false); }
  };
  const readSourceMetadata = async () => {
    if (!canReadSource || busy || sourceOperation.current) return;
    sourceOperation.current = true;
    const generation = ++metadataGeneration.current;
    setBusy(true); setError(""); setNotice(null); setSourceMetadata(null);
    try {
      const metadata = await controlApi.acqMetadata();
      if (generation !== metadataGeneration.current) return;
      setSourceMetadata(metadata);
    } catch (cause) {
      if (generation === metadataGeneration.current) setError(t("profiles.inspectFailed", {
        error: cause instanceof Error ? cause.message : String(cause) }));
    }
    finally {
      await acquisition.refresh();
      sourceOperation.current = false; setBusy(false);
    }
  };
  const changeParameters = (parameters: Record<string, unknown>) => {
    setEditor({ ...editor, parameters, schema: definition?.schema }); setNotice(null);
  };
  const useObservedValues = () => {
    if (busy || !canUseObserved || metadataRevision !== metadataGeneration.current) return;
    setEditor((current) => ({ ...current, parameters: { ...connectionParameters(current.parameters),
      ...observedParameters(sourceMetadata!, definition?.plugin_type) } }));
    setNotice("profiles.observedValuesCopied");
  };
  const sourceSchema = slot.kind === "acquisition" && definition?.schema
    ? { ...definition.schema, properties: Object.fromEntries(Object.entries(definition.schema.properties ?? {})
      .filter(([key]) => !observedFields.has(key))) } : definition?.schema;
  const dimensionSchema = slot.kind === "acquisition" && definition?.schema
    ? { ...definition.schema, properties: Object.fromEntries(Object.entries(definition.schema.properties ?? {})
      .filter(([key]) => observedFields.has(key))) } : null;
  const pluginOptions = <>
    {slot.kind === "trigger" ? <option value="">{t("profiles.noTrigger")}</option>
      : !definitions.length && !missingPlugins.length && <option value="">{t("profiles.noPlugins")}</option>}
    {definitions.map((item) => <option key={selectionKey(item.selection)} value={selectionKey(item.selection)}>
      {translatePluginName(item.plugin_type, item.label, t)} · {item.selection.version} · {item.selection.package_id}/{item.selection.contribution_id}
      {item.available ? "" : t("profiles.platformUnavailable")}{item.schema ? "" : t("profiles.noSchemaSuffix")}
    </option>)}
    {missingPlugins.map(([key, selection]) => <option key={key} value={key}>
      {selection.package_id}/{selection.contribution_id} · {selection.version} {t("profiles.unavailable")}
    </option>)}
  </>;

  const blockedReason = currentState.error ? "feedback.runtimeUnavailable" : runtimeBlockReason(runtime, module);
  const addFilterReason = busy ? "feedback.busy" : !definition?.available ? "profiles.pluginUnavailable"
    : errors.length > 0 ? "profiles.fixErrors" : null;
  const saveReason = busy ? "feedback.busy" : !definition?.schema ? "profiles.schemaRequired"
    : missingDecoderInput ? "profiles.configureDecoder" : errors.length > 0 ? "profiles.fixErrors" : null;
  const connectReason = busy ? "feedback.busy" : blockedReason
    ?? (!engineAvailable || !sourceStatus ? "profiles.sourceUnavailable" : sourceStatus.streaming ? "profiles.stopBeforeConnect"
      : sourceStatus.connected ? sourceMatches ? "profiles.sourceAlreadyConnected" : "profiles.disconnectChangedSource"
      : !definition?.available ? "profiles.pluginUnavailable" : errors.length > 0 ? "profiles.fixErrors" : null);
  const disconnectReason = busy ? "feedback.busy" : runtime.status?.recovery.acquisition
    ? (!sourceStatus ? "profiles.sourceUnavailable" : null)
    : blockedReason ?? (!sourceStatus ? "profiles.sourceUnavailable" : !sourceStatus.connected ? "profiles.sourceDisconnectedStatus"
      : sourceStatus.streaming ? "profiles.stopBeforeConnect" : null);
  const saveAs = () => setDialog({ title: t("profiles.save"), message: t("profiles.saveHelp"),
    confirmLabel: t("profiles.create"), initialValue: editor.profile ? t("profiles.copyName", { name: editor.profile.name }) : editor.name || "new profile 1",
    onConfirm: (name) => { setDialog(null); void save(name); } });
  const moduleSettings = current.settings[module] ?? {};
  const modulePrefix = module.toUpperCase();

  return <section className={`settings-workspace plugin-profiles module-${module}`} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "s" && !event.defaultPrevented) {
      event.preventDefault();
      if (!event.repeat && !event.nativeEvent?.isComposing && !dialog && !runtime.plan && !noTrigger) {
        if (!saveReason) {
          saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
          saveAs();
        }
      }
    }
  }}>
    <aside className="explorer profile-explorer">
      {navigation}
      <button className="profile-library-toggle" aria-expanded={libraryExpanded} aria-controls="profile-library"
        onClick={() => setLibraryExpanded(!libraryExpanded)}>{t("profiles.libraryTools")} <span aria-hidden="true">{libraryExpanded ? "−" : "+"}</span></button>
      <div id="profile-library" className={`profile-library${libraryExpanded ? " expanded" : ""}`}>
      <div className="explorer-heading">{t("profiles.pluginConfiguration")}</div>
      {slots[module].map((item) => <button key={item.slot} disabled={busy}
        className={`explorer-item ${slot.slot === item.slot ? "selected" : ""}`}
        aria-current={slot.slot === item.slot ? "page" : undefined}
        onClick={() => setSlotName(item.slot)}>{t(item.label)}</button>)}
      <div className="explorer-heading">{t("profiles.pluginProfiles")}</div>
      <div className="profile-search">
        <input type="search" aria-label={t("profiles.searchLabel")} aria-describedby="profile-search-scope"
          placeholder={t("profiles.searchPlaceholder")} value={profileQuery} onChange={(event) => setProfileQuery(event.target.value)} />
        <p id="profile-search-scope">{t("profiles.searchScope")}</p>
        {profileQuery && <button className="text-button" onClick={() => setProfileQuery("")}>{t("profiles.clearSearch")}</button>}
        {!busy && !error && <p role="status">{t("profiles.searchCount", { shown: displayedProfiles.length, total: pluginProfiles.length })}
          {profileHidden && <> {t("profiles.searchHidden")}</>}</p>}
      </div>
      <div className="profile-list" aria-busy={busy}>
        {!busy && !error && displayedProfiles.length === 0 && <p className="profile-list-empty">{t(pluginProfiles.length
          ? "profiles.noSearchMatches" : "profiles.noSavedProfiles")}</p>}
        {displayedProfiles.map((profile) => <button key={profile.profile_id}
          disabled={busy} className={`explorer-item profile-item ${editor.profile?.profile_id === profile.profile_id ? "selected" : ""}`}
          aria-current={editor.profile?.profile_id === profile.profile_id ? "true" : undefined}
          onClick={() => changeEditor(() => resetEditor(editorFor(profile, definitions)))}>
          <span>{profile.name}</span>{activation?.bindings.some((item) => item.profile_id === profile.profile_id)
            && <span className="active-indicator">{t("profiles.selected")}</span>}
        </button>)}
      </div>
      <div className="explorer-footer"><p>{t("profiles.explorerHint")}</p></div>
      </div>
    </aside>
    <div className="settings-editor">
      <div className="settings-scroll">
      <header className="editor-heading"><div><p className="eyebrow">{t(`profiles.module.${module}`)} / {t(slot.label)}</p>
        <h1 ref={editorHeading} tabIndex={-1}>{slot.kind === "acquisition" ? t("profiles.acquisitionSource") : t(slot.label)}</h1><p>{t(slot.kind === "acquisition" ? "profiles.connectHint" : "profiles.chooseHint")}</p></div>
      </header>
      {module === "processing" && <nav className="processing-navigation" aria-label={t("profiles.processingNavigation")}>
        {([
          ["processing.decoder", "processing-channel-mapping", "processing.flowMapping"],
          ["processing.epoch_detector", "processing-parameters", "processing.flowEpoching"],
          ["processing.filters", "processing-filter-chain", "profiles.slotFilters"],
          ["processing.decoder", "processing-decoder-parameters", "profiles.slotDecoder"],
        ] as const).map(([target, section, label]) => <button key={section} disabled={busy}
          onClick={() => openProcessingStep(target, section)}>{t(label)}</button>)}
      </nav>}
      {(error || currentState.error) && <p className="inline-message error" role="alert">{error || currentState.error}</p>}
      {notice && <p className="inline-message success" role="status">{t(notice)}</p>}
      {engineRunning && slot.kind !== "acquisition" && <p className="inline-message">{t("profiles.engineRunning")}</p>}
      {selectedBinding && <div className="binding-summary" aria-label={t("profiles.selectedConfiguration")}>
        <span>{selectionLabel}: <strong>{selectedPlugin ? translatePluginName(selectedPlugin.plugin_type, selectedPlugin.label, t) : selectedBinding.selection.package_id}</strong></span>
        {selectedProfile && selectedProfile.revision > selectedBinding.profile_revision && <span>{t("profiles.profileChanged")}</span>}
        <button disabled={busy} onClick={() => changeEditor(() => resetEditor(currentFromBinding(selectedBinding)))}>{t("profiles.restoreApplied")}</button>
      </div>}
      {currentState.conflicted && <div className="inline-message" role="status">
        <p id="accepted-configuration-help">{t("profiles.acceptedChanged")}</p>
        <button disabled={busy || runtimeState.inProgress} aria-describedby="accepted-configuration-help" onClick={async () => {
          setBusy(true);
          try {
            const accepted = await current.loadAccepted(module);
            if (accepted) setActivation(accepted);
          } finally { setBusy(false); }
        }}>{t("profiles.loadAccepted")}</button>
      </div>}
      {moduleValidation && <p className="inline-message error" role="alert">{moduleValidation}</p>}
      {module === "processing" && <section className="setting-group">
        <label className="setting-row" htmlFor="processing-pre-trigger-duration"><span className="setting-label">{t("settings.baselineDuration")}</span>
          <input id="processing-pre-trigger-duration" type="number" min="0" step="any" disabled={busy || !currentState.ready}
            value={String(current.settings.processing?.["ALGORITHM.baseline_duration"] ?? 0)}
            onChange={(event) => current.setSetting("processing", "ALGORITHM.baseline_duration", event.target.value === "" ? "" : Number(event.target.value))} />
        </label>
        <p className="setting-description">{t("settings.preTriggerDescription")}</p>
      </section>}
      {(module === "stimulation" || module === "actuation") && <section className="setting-group" aria-label={t("profiles.moduleSettings")}>
        <h2>{t("profiles.moduleSettings")}</h2>
        <p className="setting-description">{t("profiles.moduleSettingsHint")}</p>
        <label className="setting-row"><span className="setting-label">{t("profiles.runMode")}</span>
          <select disabled={busy || !currentState.ready} value={moduleSettings[`${modulePrefix}.auto_spawn`] === false ? "external" : "managed"}
            onChange={(event) => current.setSetting(module, `${modulePrefix}.auto_spawn`, event.target.value === "managed")}>
            <option value="managed">{t("profiles.managedProcess")}</option><option value="external">{t("profiles.externalNode")}</option>
          </select>
        </label>
        {module === "stimulation" && <label className="setting-row"><span className="setting-label">{t("profiles.renderer")}</span>
          <select disabled={busy || !currentState.ready} value={String(moduleSettings["STIMULATION.renderer"] ?? "moderngl")}
            onChange={(event) => current.setSetting(module, "STIMULATION.renderer", event.target.value)}>
            <option value="moderngl">ModernGL</option><option value="pyopengl">PyOpenGL</option>
          </select>
        </label>}
        <label className="setting-row"><span className="setting-label">{t("profiles.listenAddress")}</span>
          <input type="text" disabled={busy || !currentState.ready} value={String(moduleSettings[`${modulePrefix}.server_ip`] ?? "127.0.0.1")}
            onChange={(event) => current.setSetting(module, `${modulePrefix}.server_ip`, event.target.value)} />
        </label>
        <label className="setting-row"><span className="setting-label">{t("profiles.listenPort")}</span>
          <input type="number" min="1" max="65535" step="1" disabled={busy || !currentState.ready}
            value={String(moduleSettings[`${modulePrefix}.server_port`] ?? (module === "stimulation" ? 8765 : 8001))}
            onChange={(event) => current.setSetting(module, `${modulePrefix}.server_port`, event.target.value === "" ? "" : Number(event.target.value))} />
        </label>
        <p className="setting-description">{t("profiles.listenerHint")}</p>
        {module === "actuation" && <>
          <label className="setting-row"><span className="setting-label">{t("profiles.watchdogTimeout")}</span>
            <input type="number" min="0" step="any" disabled={busy || !currentState.ready}
              value={String(moduleSettings["ACTUATION.watchdog_timeout_s"] ?? 1)}
              onChange={(event) => current.setSetting(module, "ACTUATION.watchdog_timeout_s", event.target.value === "" ? "" : Number(event.target.value))} />
          </label>
          <label className="setting-row"><span className="setting-label">{t("profiles.maxCommandAge")}</span>
            <input type="number" min="0" step="any" disabled={busy || !currentState.ready}
              value={String(moduleSettings["ACTUATION.max_command_age_s"] ?? 0)}
              onChange={(event) => current.setSetting(module, "ACTUATION.max_command_age_s", event.target.value === "" ? "" : Number(event.target.value))} />
          </label>
          <p className="setting-description">{t("profiles.commandAgeHint")}</p>
        </>}
      </section>}
      <ConfigurationHistory module={module} profile={profiles.find((profile) => profile.profile_id === editor.profile?.profile_id) ?? null}
        runtime={runtime} onProfileRestored={(profile) => setProfiles((previous) => previous.map((item) => item.profile_id === profile.profile_id ? profile : item))} />
      <p className="setting-description">{t(current.hasChanges(module, currentState.activation) ? "profiles.pendingCurrent" : "profiles.currentApplied")}</p>
        <div className="setting-group profile-selectors"><h2>{t("profiles.pluginAndProfile")}</h2>
          <label className="setting-row"><span className="setting-label">{t("profiles.pluginAndVersion")}</span>
            <select value={editor.plugin} disabled={busy} onChange={(event) => choosePlugin(event.target.value)}>{pluginOptions}</select></label>
          {!noTrigger && <><label className="setting-row"><span className="setting-label">{t("profiles.profile")}</span>
            <select value={editor.profile?.profile_id ?? ""} disabled={busy || !editor.plugin}
              onChange={(event) => { const profile = pluginProfiles.find((item) => item.profile_id === event.target.value);
                changeEditor(() => resetEditor(editorFor(profile, definitions, editor.plugin, profiles))); }}>
              {!editor.profile && <option value="">{t("profiles.newUnsaved")}</option>}
              {pluginProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select></label>
          <div className="profile-tools actions">
            <button disabled={busy || !definition?.schema} onClick={() => newProfile()}>{t("profiles.newProfile")}</button>
          </div></>}
          {noTrigger && <p className="setting-description">{t("profiles.noTriggerHint")}</p>}
          {slot.kind === "acquisition" && <p className="setting-description">{t(sourceMode === "new"
            ? reportedSource ? "profiles.newSourceHint" : "profiles.newDeclaredSourceHint" : "profiles.sourceConfigurationHint")}</p>}
          {missingDecoderInput && <p className="setting-description">{t("profiles.configureDecoder")}</p>}
          {!noTrigger && !pluginProfiles.length && slot.kind !== "acquisition" && <p className="setting-description">{t("profiles.noSavedProfiles")}</p>}
        </div>
        {slot.kind === "filter" && <div id="processing-filter-chain" tabIndex={-1} className="setting-group" role="region" aria-label={t("profiles.filterChain")}><h2>{t("profiles.filterChain")}</h2>
          <p className="setting-description">{t("profiles.filterOrder")}</p>
          <ol className="filter-chain">{chain.map((item, index) => <li key={item.slot}>
            <button aria-pressed={editorSlot === item.slot} disabled={busy} onClick={() => setFilterSlot(item.slot)}>
              {t("profiles.editFilter", { index: index + 1 })} · {definitions.find((definition) => selectionKey(definition.selection) === item.plugin)?.label ?? item.selection?.package_id}
            </button>
            <div className="actions">
              <button disabled={busy || index === 0} onClick={() => { const next = [...chain]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setChain(next); }}>{t("profiles.moveUp")}</button>
              <button disabled={busy || index === chain.length - 1} onClick={() => { const next = [...chain]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; setChain(next); }}>{t("profiles.moveDown")}</button>
              <button disabled={busy} onClick={() => setChain(chain.filter((previous) => previous.slot !== item.slot))}>{t("profiles.remove")}</button>
            </div>
          </li>)}</ol>
          {!chain.length && <p className="setting-description">{t("profiles.noFilters")}</p>}
          <div className="actions"><button disabled={busy} onClick={() => setFilterSlot(null)}>{t("profiles.newFilter")}</button>
            {editorSlot === "processing.filters" && <button className="primary" disabled={!!addFilterReason}
              aria-describedby="filter-add-help" onClick={() => {
                const slot = `processing.filter.${crypto.randomUUID()}`;
                current.set(slot, editor); current.setFilterOrder([...current.filterOrder, slot]); setFilterSlot(slot);
                setNotice("profiles.filterAdded");
              }}>{t("profiles.addFilter")}</button>}</div>
          {editorSlot === "processing.filters" && <p className="action-help" id="filter-add-help">{t(addFilterReason ?? "profiles.addFilterHelp")}</p>}
        </div>}
        <div className={slot.kind === "acquisition" ? "acquisition-layout" : "profile-layout"}>
        <div className="profile-fields">
        {!noTrigger && <><div className="setting-group profile-identity"><h2>{t("profiles.currentConfiguration")}</h2></div>
        <div id={slot.kind === "decoder" ? "processing-decoder-parameters" : "processing-parameters"}
          className="profile-parameters" tabIndex={-1} role="region" aria-label={t(slot.label)}>
          {sourceSchema ? <SchemaForm schema={sourceSchema} value={editor.parameters} disabled={busy} onChange={changeParameters} />
            : <p className="setting-description">{t("profiles.schemaRequired")}</p>}
        </div></>}
        {dimensionSchema && Object.keys(dimensionSchema.properties).length > 0 && <details className="setting-group" open={sourceMode === "new"}>
          <summary>{t(reportedSource ? "profiles.expectedParameters" : "profiles.dimensionDefaults")}</summary>
          <p className="setting-description">{t(reportedSource ? sourceMode === "new" ? "profiles.newSourceHint" : "profiles.expectedParametersHint" : "profiles.dimensionDefaultsHint")}</p>
          {reportedSource ? <dl className="expected-source-parameters">
            <div><dt>{t("metadata.sampleRate")}</dt><dd>{String(editor.parameters.sample_rate ?? t("metadata.notConfirmed"))}</dd></div>
            <div><dt>{t("metadata.outputRows")}</dt><dd>{String(editor.parameters.channel_count ?? t("metadata.notConfirmed"))}</dd></div>
            {definition?.plugin_type === "neuroscan_curry8_reader" && <div><dt>{t("metadata.channelLabels")}</dt>
              <dd>{Array.isArray(editor.parameters.channel_labels) ? editor.parameters.channel_labels.join(", ") : t("metadata.notConfirmed")}</dd></div>}
          </dl> : <SchemaForm schema={dimensionSchema} value={editor.parameters} disabled={busy} onChange={changeParameters} />}
        </details>}
        </div>
        {slot.kind === "acquisition" && <div className="acquisition-observations"><div className="setting-group source-connection">
          <h2>{t("profiles.connection")}</h2>
          <dl className="source-preparation-summary">
            <div><dt>{t("profiles.startupSource")}</dt><dd>{selectedBinding
              ? `${selectedProfile?.name ?? (selectedBinding.profile_id || t("profiles.currentSourceConfiguration"))} · ${selectedBinding.selection.package_id}/${selectedBinding.selection.contribution_id} · ${selectedBinding.selection.version}${selectedBinding.profile_id ? ` · ${t("profiles.revision", { revision: selectedBinding.profile_revision })}` : ""}`
              : t("run.none")}</dd></div>
            <div><dt>{t("profiles.currentSource")}</dt><dd>{!runtime.status?.running ? t("run.engineStopped")
              : runtime.status.applied?.[module] !== activation?.revision ? t("profiles.currentSourceUnknown")
              : selectedBinding ? `${selectedProfile?.name ?? (selectedBinding.profile_id || t("profiles.currentSourceConfiguration"))} · ${selectedBinding.selection.package_id}/${selectedBinding.selection.contribution_id}${selectedBinding.profile_id ? ` · ${t("profiles.revision", { revision: selectedBinding.profile_revision })}` : ""}`
              : t("run.none")}</dd></div>
          </dl>
          <p className="setting-description" id="source-connect-help">{t("profiles.connectHelp")}</p>
          {connectReason && <p className="action-help" id="source-connect-blocked">{t(connectReason)}</p>}
          <div className="actions"><button className="primary" disabled={!!connectReason}
            aria-describedby={connectReason ? "source-connect-blocked source-connect-help" : "source-connect-help"}
            onClick={() => void connectSource()}>{t("profiles.connectSource")}</button>
            <button disabled={!!disconnectReason} aria-describedby={disconnectReason
              ? disconnectReason === "profiles.sourceDisconnectedStatus" ? "source-connection-status" : "source-disconnect-help" : undefined}
              onClick={() => void disconnectSource()}>{t("profiles.disconnectSource")}</button></div>
          {disconnectReason && disconnectReason !== "profiles.sourceDisconnectedStatus"
            && <p className="action-help" id="source-disconnect-help">{t(disconnectReason)}</p>}
          {acquisition.connect.error && <p className="inline-message error" role="alert"><strong>{t("profiles.connectSource")}:</strong> {acquisition.connect.error}</p>}
          {acquisition.disconnect.error && <p className="inline-message error" role="alert"><strong>{t("profiles.disconnectSource")}:</strong> {acquisition.disconnect.error}</p>}
          <p role="status" id="source-connection-status">{t(!sourceStatus ? "profiles.sourceUnavailable" : sourceStatus.streaming ? "profiles.sourceReading"
            : sourceStatus.connected ? "profiles.currentSourceConnected" : "profiles.sourceDisconnectedStatus")}</p>
          {reportedSource && (editor.parameters.sample_rate == null || editor.parameters.channel_count == null)
            && <p className="setting-description">{t("profiles.expectationsUncaptured")}</p>}
          {onRun && <button className="text-button" disabled={busy} onClick={onRun}>{t("profiles.openRun")}</button>}
        </div><AcquisitionMetadata value={sourceMetadata} busy={busy} canRead={canReadSource}
          readHint={t(sourceStatus?.streaming ? "profiles.stopBeforeRead" : canReadSource ? "profiles.readReady" : "profiles.connectBeforeRead")}
          onRead={() => void readSourceMetadata()} canUseObserved={canUseObserved}
          showComparison={sourceSelected || sourceMode !== "new" || !reportedSource}
          onUseObserved={reportedSource && sourceSelected ? useObservedValues : undefined} /></div>}
        </div>
        {slot.kind === "decoder" && definition && <div id="processing-channel-mapping" tabIndex={-1} role="region" aria-label={t("processing.flowMapping")}>
          <ProcessingInput key={`${editor.profile?.profile_id ?? editor.plugin}:${editor.profile?.revision ?? "new"}`}
          value={editor.processingInput} busy={busy}
          decoderSelection={definition.selection} channelRequirements={definition.channel_requirements}
          configurationRevision={`${runtime.status?.accepted.acquisition}:${runtime.status?.applied?.acquisition}:${runtime.status?.accepted.processing}:${runtime.status?.applied?.processing}`}
          onChange={(processingInput) => { setEditor({ ...editor, processingInput, requiresProcessingInputReview: false }); setNotice(null); }} /></div>}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}

      </div>
      <footer className="editor-footer"><div className="editor-feedback">
        <div className="save-state" role="status">{noTrigger ? t("profiles.noTrigger")
          : dirty ? t("profiles.unsaved") : editor.profile ? t("profiles.profileSaved") : t("profiles.newNotSaved")}
          {chainDirty && <span className="pending-state">{t("profiles.chainPending")}</span>}</div>
        {!noTrigger && <p className="action-help" id="profile-save-help">{t(saveReason ?? "profiles.saveHelp")}</p>}
        <RuntimeConfigurationProgress runtime={runtime} module={module} />
      </div>
        <div className="actions">
          <button disabled={localBusy} onClick={() => { void current.reload(module); void reload().catch((cause) => setError(String(cause))); }}>{t("profiles.reload")}</button>
          {!noTrigger && <>
            {editor.profile && <button className="text-button danger" disabled={busy || !!activation?.bindings.some((binding) => binding.profile_id === editor.profile?.profile_id)}
              onClick={() => setDialog({ title: t("profiles.deleteProfile"), message: t("profiles.deletePrompt", { name: editor.profile!.name }),
                confirmLabel: t("profiles.deleteProfile"), onConfirm: () => {
                  setDialog(null); setBusy(true); setError("");
                  void api.delete(editor.profile!).then(() => {
                    setProfiles((profiles) => profiles.filter((profile) => profile.profile_id !== editor.profile!.profile_id));
                    setEditor({ ...editor, profile: null }); setNotice("profiles.deleted");
                  }).catch((cause) => setError(String(cause))).finally(() => setBusy(false));
                } })}>{t("profiles.deleteProfile")}</button>}
            <button className="primary" disabled={!!saveReason} aria-describedby="profile-save-help" aria-keyshortcuts="Control+s Meta+s"
              onClick={saveAs}>{t("profiles.save")}</button>
          </>}
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
