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
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";
import { prepareSource, type SourceStage } from "./source-lifecycle";

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
const observedFields = new Set(["sample_rate", "channel_count", "channel_labels"]);
const connectionParameters = (parameters: Record<string, unknown>) => Object.fromEntries(
  Object.entries(parameters).filter(([key]) => !observedFields.has(key)));
const filterInstances = (activation: Activation | null): FilterInstance[] => activation?.bindings
  .filter((item) => item.kind === "filter").map((item) => ({ slot: item.slot,
    profile_id: item.profile_id ?? "", plugin: selectionKey(item.selection) })) ?? [];

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
  runtime, acquisitionStatus = null, onAcquisitionChange, onSelected, onDirtyChange, onBusyChange, onRun }: {
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
  onRun?: () => void;
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
  const [libraryExpanded, setLibraryExpanded] = useState(false);
  const [profileQuery, setProfileQuery] = useState("");
  const [sourceStage, setSourceStage] = useState<SourceStage | null>(null);
  const [inspection, setInspection] = useState<{ revision: string; generation: string; connectionId: string } | null>(null);
  const pendingFocus = useRef<string | null>(null);
  const saveFocus = useRef<HTMLElement | null>(null);
  const editorHeading = useRef<HTMLHeadingElement>(null);
  const sourceMode = editor.profile ? "saved" : "new";
  const metadataGeneration = useRef(0);
  const sourceOperation = useRef(false);
  const loadGeneration = useRef(0);
  const definition = definitions.find((item) => selectionKey(item.selection) === editor.plugin);
  const noTrigger = slot.kind === "trigger" && editor.plugin === "";
  const sourceParametersChanged = !!editor.profile && !sameParameters(editor.parameters, editor.profile.parameters);
  const profilesFor = (plugin: string) => profiles.filter((item) => item.kind === slot.kind
    && selectionKey(item.selection) === plugin);
  const pluginProfiles = profilesFor(editor.plugin);
  const displayedProfiles = pluginProfiles.filter((item) => `${item.name} ${item.profile_id}`
    .toLowerCase().includes(profileQuery.trim().toLowerCase()));
  const profileHidden = !!editor.profile && !displayedProfiles.some((item) => item.profile_id === editor.profile!.profile_id);
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
  const sourceSelected = !!selectedBinding && !!editor.profile && selectedBinding.profile_id === editor.profile.profile_id
    && selectedBinding.profile_revision === editor.profile.revision
    && selectionKey(selectedBinding.selection) === editor.plugin
    && sameParameters(selectedBinding.parameters, sourceParameters);
  const sourceMatches = sourceSelected && runtime.status?.running === true
    && runtime.status.applied_revision === activation?.revision;
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
      setEditor((current) => current.profile || !Object.keys(current.parameters).some((key) => observedFields.has(key))
        ? current : { ...current, parameters: connectionParameters(current.parameters) });
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
  }, [runtime.status?.accepted_revision, runtime.operation?.operation_id, runtime.operation?.status, onSelected]);

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

  useEffect(() => {
    if (busy || !pendingFocus.current || typeof document === "undefined") return;
    focusSection(pendingFocus.current);
    pendingFocus.current = null;
  }, [busy, slot.slot]);

  useEffect(() => {
    if (busy || !saveFocus.current) return;
    const field = saveFocus.current;
    saveFocus.current = null;
    if (field.isConnected && field.getClientRects().length) field.focus();
    else editorHeading.current?.focus();
  }, [busy]);

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
      cancelLabel: t("profiles.keepEditing"),
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
  const openProcessingStep = (target: string, section: string) => {
    if (busy) return;
    if (target === slot.slot) {
      focusSection(section);
      return;
    }
    changeEditor(() => { pendingFocus.current = section; setBusy(true); setSlotName(target); }, true);
  };
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
    if (sourceOperation.current || connectReason || !activation) return;
    sourceOperation.current = true;
    metadataGeneration.current += 1; setSourceMetadata(null);
    setBusy(true); setError(""); setNotice(null);
    let stage: SourceStage = "checking";
    try {
      const prepared = await prepareSource(activation.revision, (next) => { stage = next; setSourceStage(next); });
      setSourceStatus(prepared.acquisition);
      if (prepared.acquisition.streaming) throw new Error(t("profiles.stopBeforeRead"));
      await runtime.refresh();
      setInspection({ revision: activation.revision, generation: prepared.runtime.generation!,
        connectionId: prepared.acquisition.connection_id! });
    } catch (cause) {
      setError(t("profiles.prepareFailed", { stage: t(`profiles.stage.${stage}`),
        error: cause instanceof Error ? cause.message : String(cause) }));
      sourceOperation.current = false;
      setSourceStage(null); setBusy(false);
      await runtime.refresh(); await onAcquisitionChange?.();
    }
  };
  const disconnectSource = async () => {
    if (sourceOperation.current || disconnectReason) return;
    sourceOperation.current = true;
    setBusy(true); setError(""); setNotice(null);
    try {
      const [current, status] = await Promise.all([controlApi.acquisitionStatus(), runtimeConfigApi.status()]);
      if (!current?.connected || current.streaming || current.connection_id !== sourceStatus?.connection_id
          || status.generation !== runtime.status?.generation || status.accepted_revision !== activation?.revision
          || status.applied_revision !== activation?.revision) throw new Error(t("profiles.sourceChanged"));
      await controlApi.acqDisconnect();
      setSourceStatus(await controlApi.acquisitionStatus());
      await onAcquisitionChange?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { sourceOperation.current = false; setBusy(false); }
  };
  const readSourceMetadata = async (prepared = false) => {
    if (!canReadSource || (!prepared && (busy || sourceOperation.current))) return;
    sourceOperation.current = true;
    const generation = ++metadataGeneration.current;
    setBusy(true); setError(""); setNotice(null); setSourceMetadata(null);
    setSourceStage("inspecting");
    try {
      const metadata = await controlApi.acqMetadata();
      if (generation !== metadataGeneration.current) return;
      setSourceMetadata(metadata);
      if (prepared) setNotice("profiles.sourceInspected");
      await onAcquisitionChange?.();
    } catch (cause) {
      if (generation === metadataGeneration.current) setError(t("profiles.inspectFailed", {
        error: cause instanceof Error ? cause.message : String(cause) }));
    }
    finally { sourceOperation.current = false; setSourceStage(null); setBusy(false); }
  };
  useEffect(() => {
    if (!inspection) return;
    const abort = () => {
      sourceOperation.current = false;
      setInspection(null); setSourceStage(null); setBusy(false); setError(t("profiles.sourceChanged"));
    };
    if (runtime.blocked || runtime.error || runtime.status?.accepted_revision !== inspection.revision
        || (runtime.status?.running && runtime.status.generation !== inspection.generation)
        || sourceStatus?.streaming
        || (sourceStatus?.connection_id && sourceStatus.connection_id !== inspection.connectionId)) {
      abort();
      return;
    }
    if (sourceMatches && sourceStatus?.connected && sourceStatus.connection_id === inspection.connectionId) {
      setInspection(null);
      void readSourceMetadata(true);
      return;
    }
    // A parent status poll may still contain the stopped engine from before preparation.
    const timeout = setTimeout(abort, 5000);
    return () => clearTimeout(timeout);
  }, [inspection, sourceMatches, sourceStatus?.connected, sourceStatus?.connection_id, sourceStatus?.streaming,
    runtime.status?.accepted_revision, runtime.status?.generation, runtime.status?.running, runtime.blocked, runtime.error]);
  const changeParameters = (parameters: Record<string, unknown>) => {
    setEditor({ ...editor, parameters: sourceMode === "new" && reportedSource ? connectionParameters(parameters) : parameters }); setNotice(null);
  };
  const useObservedValues = () => {
    if (busy || !canUseObserved || metadataRevision !== metadataGeneration.current) return;
    setEditor((current) => ({ ...current, parameters: { ...connectionParameters(current.parameters),
      ...observedParameters(sourceMetadata!, definition?.plugin_type) } }));
    setNotice("profiles.observedValuesCopied");
  };
  const saveObservedProfile = () => {
    if (busy || !canUseObserved || !definition || metadataRevision !== metadataGeneration.current) return;
    const parameters = { ...connectionParameters(structuredClone(editor.parameters)),
      ...observedParameters(sourceMetadata!, definition.plugin_type) };
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
      {translatePluginName(item.plugin_type, item.label, t)} · {item.selection.version ?? t("profiles.unversioned")} · {item.selection.package_id}/{item.selection.contribution_id}
      {item.available ? "" : t("profiles.platformUnavailable")}{item.schema ? "" : t("profiles.noSchemaSuffix")}
    </option>)}
    {missingPlugins.map(([key, selection]) => <option key={key} value={key}>
      {selection.package_id}/{selection.contribution_id} · {selection.version ?? t("profiles.unversioned")} {t("profiles.unavailable")}
    </option>)}
  </>;

  const ProfileSection = slot.kind === "acquisition" ? "details" : "div";
  const ProfileHeading = slot.kind === "acquisition" ? "summary" : "h2";
  const blockedReason = runtimeBlockReason(runtime);
  const reviewReason = busy ? "feedback.busy" : dirty ? "profiles.saveBeforeReview"
    : !noTrigger && !editor.profile ? "profiles.saveBeforeReview"
    : !noTrigger && !definition?.available ? "profiles.pluginUnavailable"
    : slot.kind !== "filter" ? !engineAvailable ? "feedback.runtimeUnavailable" : blockedReason : null;
  const saveReason = busy ? "feedback.busy" : !definition?.schema ? "profiles.schemaRequired"
    : errors.length > 0 ? "profiles.fixErrors" : !editor.name.trim() ? "profiles.nameRequired"
    : !dirty && editor.profile ? "profiles.nothingToSave" : null;
  const connectReason = busy ? "feedback.busy" : blockedReason
    ?? (!engineAvailable ? "feedback.runtimeUnavailable" : !definition?.available ? "profiles.pluginUnavailable"
      : errors.length > 0 ? "profiles.fixErrors" : sourceStatus?.streaming ? "profiles.stopBeforeConnect"
      : dirty || !editor.profile ? "profiles.saveBeforePrepare" : !sourceSelected ? "profiles.applyBeforePrepare" : null);
  const disconnectReason = busy ? "feedback.busy" : blockedReason
    ?? (!sourceStatus?.connected ? "profiles.sourceDisconnectedStatus" : sourceStatus.streaming ? "profiles.stopBeforeConnect"
      : !sourceMatches ? "profiles.viewSourceBeforeDisconnect" : null);
  const chainReason = busy ? "feedback.busy" : !engineAvailable ? "feedback.runtimeUnavailable" : blockedReason
    ?? (!chainDirty ? "profiles.chainUnchanged" : chain.some((item) => !item.profile_id) ? "profiles.saveEveryProfile" : null);
  const profileAction = <div className="editor-action"><button className="primary" disabled={!!reviewReason}
          aria-describedby="profile-review-help"
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
          }}>{slot.kind === "filter" ? t("profiles.addFilter") : t("profiles.reviewProfile")}</button>
    <p className="action-help" id="profile-review-help">{t(reviewReason ?? (slot.kind === "filter" ? "profiles.addFilterHelp" : "profiles.reviewHelp"))}</p>
  </div>;

  return <section className={`settings-workspace plugin-profiles module-${module}`} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "s" && !event.defaultPrevented) {
      event.preventDefault();
      if (!event.repeat && !event.nativeEvent?.isComposing && !dialog && !runtime.plan && !noTrigger && !saveReason) {
        saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
        save();
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
        onClick={() => changeEditor(() => setSlotName(item.slot), true)}>{t(item.label)}</button>)}
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
        {profileAction}
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
        {slot.kind === "filter" && <div id="processing-filter-chain" tabIndex={-1} className="setting-group" role="region" aria-label={t("profiles.filterChain")}><h2>{t("profiles.filterChain")}</h2>
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
          <p className="action-help" id="filter-chain-help">{t(chainReason ?? "profiles.chainReviewHelp")}</p>
          <div className="setting-row"><button className="primary" disabled={!!chainReason} aria-describedby="filter-chain-help"
            onClick={() => void reviewSelection(() => [
              ...bindingSelections().filter((item) => !item.slot.startsWith("processing.filter.")),
              ...chain.map(({ slot, profile_id }) => ({ slot, profile_id })),
            ], [])}>{t("profiles.reviewChain")}</button></div>
        </div>}
        <div className={slot.kind === "acquisition" ? "acquisition-layout" : "profile-layout"}>
        <div className="profile-fields">
        {!noTrigger && <><ProfileSection className="setting-group profile-identity" {...(slot.kind === "acquisition" && sourceMode === "new" ? { open: true } : {})}>
          <ProfileHeading>{editor.profile ? t("profiles.edit") : t("profiles.newProfile")}</ProfileHeading>
          <label className="setting-row"><span className="setting-label">{t("profiles.name")}</span>
            <input value={editor.name} maxLength={64} disabled={busy} onChange={(event) => {
              setEditor({ ...editor, name: event.target.value }); setNotice(null);
            }} /></label>
        </ProfileSection>
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
        {slot.kind === "acquisition" && definition && <div className="acquisition-observations"><div className="setting-group source-connection">
          <h2>{t("profiles.connection")}</h2>
          <dl className="source-preparation-summary">
            <div><dt>{t("profiles.startupSource")}</dt><dd>{selectedBinding
              ? `${selectedProfile?.name ?? selectedBinding.selection.package_id} · ${selectedBinding.selection.version} · ${t("profiles.revision", { revision: selectedBinding.profile_revision ?? 0 })}`
              : t("run.none")}</dd></div>
            <div><dt>{t("profiles.currentSource")}</dt><dd>{!runtime.status?.running ? t("run.engineStopped")
              : runtime.status.applied_revision !== activation?.revision ? t("profiles.currentSourceUnknown")
              : selectedBinding ? `${selectedProfile?.name ?? selectedBinding.selection.package_id} · ${t("profiles.revision", { revision: selectedBinding.profile_revision ?? 0 })}`
              : t("run.none")}</dd></div>
          </dl>
          <p className="setting-description" id="source-connect-help">{t("profiles.prepareHelp")}</p>
          {connectReason && <p className="action-help" id="source-connect-blocked">{t(connectReason)}</p>}
          {sourceStage && <p className="action-help" role="status">{t(`profiles.stage.${sourceStage}`)}</p>}
          <div className="actions"><button className="primary" disabled={!!connectReason}
            aria-describedby={connectReason ? "source-connect-blocked source-connect-help" : "source-connect-help"}
            onClick={() => void connectSource()}>{t("profiles.prepareInspect")}</button>
            <button disabled={!!disconnectReason} aria-describedby={disconnectReason
              ? disconnectReason === "profiles.sourceDisconnectedStatus" ? "source-connection-status" : "source-disconnect-help" : undefined}
              onClick={() => void disconnectSource()}>{t("profiles.disconnectSource")}</button></div>
          {disconnectReason && disconnectReason !== "profiles.sourceDisconnectedStatus"
            && <p className="action-help" id="source-disconnect-help">{t(disconnectReason)}</p>}
          <p role="status" id="source-connection-status">{t(!sourceStatus ? "profiles.sourceUnavailable" : sourceStatus.streaming ? "profiles.sourceReading"
            : sourceStatus.connected ? "profiles.currentSourceConnected" : "profiles.sourceDisconnectedStatus")}</p>
          {reportedSource && (editor.parameters.sample_rate == null || editor.parameters.channel_count == null)
            && <p className="setting-description">{t("profiles.expectationsUncaptured")}</p>}
          {onRun && <button className="text-button" disabled={busy} onClick={() => changeEditor(onRun)}>{t("profiles.openRun")}</button>}
        </div><AcquisitionMetadata value={sourceMetadata} busy={busy} canRead={canReadSource}
          readHint={t(sourceStatus?.streaming ? "profiles.stopBeforeRead" : canReadSource ? "profiles.readReady" : "profiles.connectBeforeRead")}
          onRead={() => void readSourceMetadata()} canUseObserved={canUseObserved}
          showComparison={sourceMode !== "new" || !reportedSource}
          onUseObserved={reportedSource && sourceMode === "saved" ? useObservedValues : undefined}
          onSaveObservedProfile={reportedSource && sourceMode === "saved" ? saveObservedProfile : undefined} /></div>}
        </div>
        {slot.kind === "decoder" && definition && <div id="processing-channel-mapping" tabIndex={-1} role="region" aria-label={t("processing.flowMapping")}>
          <ProcessingInput key={`${editor.profile?.profile_id ?? editor.plugin}:${editor.profile?.revision ?? "new"}`}
          value={editor.processingInput} busy={busy}
          decoderSelection={definition.selection} channelRequirements={definition.channel_requirements}
          configurationRevision={`${runtime.status?.accepted_revision}:${runtime.status?.applied_revision}`}
          onChange={(processingInput) => { setEditor({ ...editor, processingInput }); setNotice(null); }} /></div>}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}
        {selectedBindings.length > 0 && <p className="setting-description">{t("profiles.revisions", { selected: selectedBindings.map((item) => item.profile_revision).join(", "), saved: editor.profile?.revision ?? "" })}</p>}

      </div>
      <footer className="editor-footer"><div className="editor-feedback">
        <div className="save-state" role="status">{noTrigger ? t("profiles.noTrigger") : dirty ? <><span className="unsaved-dot" />{t("profiles.unsaved")}</>
          : editor.profile ? t("profiles.profileSaved") : t("profiles.newNotSaved")}
          {chainDirty && <span className="pending-state">{t("profiles.chainPending")}</span>}</div>
        {!noTrigger && <p className="action-help" id="profile-save-help">{t(saveReason ?? "profiles.saveHelp")}</p>}
        <RuntimeConfigurationProgress runtime={runtime} />
      </div>
        <div className="actions">
          <button disabled={busy} onClick={() => changeEditor(() => { void run(async () => editor.profile?.profile_id ?? "new", "profiles.refreshed"); }, true)}>{t("profiles.reload")}</button>
          {!noTrigger && <>{editor.profile && <button className="text-button danger" disabled={busy || selectedBindings.length > 0 || chain.some((item) => item.profile_id === editor.profile?.profile_id)}
            onClick={() => setDialog({ title: t("profiles.deleteProfile"), message: t("profiles.deletePrompt", { name: editor.profile!.name }), confirmLabel: t("profiles.deleteProfile"),
              onConfirm: () => { setDialog(null); void run(async () => { await api.delete(editor.profile!); return "new"; }, "profiles.deleted", true); } })}>{t("profiles.delete")}</button>}
          <button disabled={busy || (!dirty && !chainDirty)} onClick={() => changeEditor(() => {
            resetEditor(structuredClone(baseline)); setChain(savedChain);
          }, true)}>{t("profiles.discard")}</button>
          <button className="primary" disabled={!!saveReason} aria-describedby="profile-save-help" aria-keyshortcuts="Control+s Meta+s"
            onClick={() => save()}>{t("profiles.save")}</button></>}
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
