/** Plugin-owned settings, saved profiles, and explicit ordered activation. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import { SchemaForm, initialSchemaValue, schemaErrors, sameParameters, schemaText } from "./SchemaForm";
import { pluginConfigApi as api, selectionKey, type Activation, type BindingSelection,
  type PluginDefinition, type PluginKind, type PluginProfile } from "./api/plugin-config";
import { useI18n, translatePluginName, type TranslationKey } from "./i18n";
import "./PluginProfiles.css";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { AcquisitionMetadata } from "./AcquisitionMetadata";
import type { ProcessingInput as DecoderInput } from "./api/signal-config";
import { ProcessingInput, emptyProcessingInput, processingInputErrors } from "./ProcessingInput";
import { api as controlApi, type DataSourceMetadata } from "./api/client";
import type { RuntimePlanRequest } from "./api/runtime-config";
import { RuntimeConfigurationProgress, runtimeBlockReason } from "./RuntimeConfiguration";
import type { AcquisitionController } from "./hooks/useAcquisition";
import { formatProfileValue, profileParameterChanges } from "./profileReviewDiff";

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
type Editor = { sourceOrigin?: "new" | "current" | "profile"; profile: PluginProfile | null; plugin: string; name: string; parameters: Record<string, unknown>; processingInput: DecoderInput | null };
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
  if (profile) return { sourceOrigin: "profile", profile, plugin: selectionKey(profile.selection), name: profile.name,
    parameters: structuredClone(profile.parameters),
    processingInput: profile.processing_input ? { ...structuredClone(profile.processing_input),
      outputs: structuredClone(profile.processing_input.outputs ?? []) } : null };
  const definition = plugin !== undefined ? definitions.find((item) => selectionKey(item.selection) === plugin)
    : definitions.find((item) => item.schema && item.available) ?? definitions.find((item) => item.schema);
  const defaults = definition?.schema ? initialSchemaValue(definition.schema) as Record<string, unknown> : {};
  return { sourceOrigin: "new", profile: null, plugin: plugin ?? (definition ? selectionKey(definition.selection) : ""), name: "",
    parameters: ["file_reader", "neuroscan_curry8_reader"].includes(definition?.plugin_type ?? "")
      ? connectionParameters(defaults) : defaults,
    processingInput: definition?.kind === "decoder" ? emptyProcessingInput() : null };
}

function editorForSource(binding: Activation["bindings"][number], profiles: PluginProfile[]): Editor {
  const profile = profiles.find((item) => item.profile_id === binding.profile_id) ?? null;
  return { sourceOrigin: "current", profile, plugin: selectionKey(binding.selection), name: profile?.name ?? "",
    parameters: structuredClone(binding.parameters), processingInput: null };
}

export function PluginProfiles({ module, engineRunning, engineAvailable = true, navigation,
  runtime, acquisition, onSelected, onDirtyChange, onBusyChange, onRun }: {
  module: Module;
  engineRunning: boolean;
  engineAvailable?: boolean;
  navigation?: ReactNode;
  runtime: RuntimeConfiguration;
  acquisition: AcquisitionController;
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
  const [localBusy, setBusy] = useState(true);
  const busy = localBusy || acquisition.pending;
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
  const needsSave = !noTrigger && (!editor.profile || dirty);
  const errors = [...(definition?.schema ? schemaErrors(definition.schema, editor.parameters, undefined, t) : []),
    ...(slot.kind === "decoder" ? processingInputErrors(editor.processingInput, t) : [])];
  const selectedBindings = editor.profile ? activation?.bindings.filter((item) => item.profile_id === editor.profile!.profile_id) ?? [] : [];
  const selectedChanged = selectedBindings.some((item) => !sameParameters(item.parameters, editor.profile?.parameters)
    || selectionKey(item.selection) !== selectionKey(editor.profile!.selection)
    || !sameParameters(item.processing_input ?? null, editor.profile?.processing_input ?? null));
  const legacyDecoder = slot.kind === "decoder" && !!editor.profile && !editor.processingInput;
  const savedChain = filterInstances(activation);
  const chainDirty = JSON.stringify(chain) !== JSON.stringify(savedChain);
  const chainDirtyRef = useRef(chainDirty);
  chainDirtyRef.current = chainDirty;
  const reportedSource = ["neuroscan_curry8_reader", "file_reader"].includes(definition?.plugin_type ?? "");
  const sourceParameters = editor.parameters;
  const sourceParameterKey = JSON.stringify(sourceParameters);
  const sourceSelected = !!selectedBinding && runtime.status?.accepted_revision === activation?.revision
    && selectionKey(selectedBinding.selection) === editor.plugin
    && sameParameters(selectedBinding.parameters, sourceParameters);
  const sourceMatches = sourceSelected && runtime.status?.running === true
    && runtime.status.applied_revision === activation?.revision;
  const canReadSource = !!sourceStatus?.connected && !sourceStatus.streaming && sourceMatches
    && !runtime.blocked;
  const metadataRevision = metadataGeneration.current;
  const canUseObserved = canReadSource && reportedSource && !!sourceMetadata?.descriptor
    && !!sourceMetadata.comparison && sourceMetadata.comparison.status !== "unverified"
    && sourceMetadata.connection_id === sourceStatus?.connection_id
    && sourceMetadata.descriptor.connection_id === sourceMetadata.connection_id
    && sourceMetadata.descriptor.source_id === sourceMetadata.source_id;

  useEffect(() => {
    metadataGeneration.current += 1;
    setSourceMetadata(null);
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
  }, [runtime.status?.accepted_revision, runtime.operation?.operation_id, runtime.operation?.status,
    runtime.completedOperation?.operation_id, slot.kind, onSelected]);

  useEffect(() => { onDirtyChange?.(dirty || chainDirty); }, [dirty, chainDirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);

  const resetEditor = (next: Editor) => {
    metadataGeneration.current += 1; setSourceMetadata(null);
    setEditor(next); setBaseline(next.profile ? editorFor(next.profile, definitions) : structuredClone(next)); setNotice(null);
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
    const next = slot.kind === "acquisition" && nextBinding && (profileId === undefined || profileId === "current")
      ? editorForSource(nextBinding, nextProfiles)
      : slot.kind === "trigger" && (plugin === "" || (plugin === undefined && !nextBinding))
        ? structuredClone(emptyEditor) : editorFor(selected, nextDefinitions, plugin);
    setEditor(next); setBaseline(next.profile ? editorFor(next.profile, nextDefinitions) : structuredClone(next));
  }, [slot.kind, slot.slot]);

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
  const requireSavedProfile = () => {
    if (!needsSave) return false;
    const before = editor.profile ? { name: editor.profile.name, parameters: editor.profile.parameters,
      ...(slot.kind === "decoder" ? { processing_input: editor.profile.processing_input ?? null } : {}) } : {};
    const after = { name: editor.name, parameters: editor.parameters,
      ...(slot.kind === "decoder" ? { processing_input: editor.processingInput } : {}) };
    const changes = profileParameterChanges(before, after, { type: "object", properties: {
      name: { type: "string", title: t("profiles.name") },
      parameters: { type: "object", ...definition?.schema, title: t("schema.parameters") },
      processing_input: { type: "object", title: t("processing.flowMapping") },
    } }, (label) => schemaText(label, t));
    const displayValue = (value: unknown, present: boolean, secret: boolean) => !present ? t("runtime.profileNotSet")
      : secret ? t("runtime.profileSecretValue") : formatProfileValue(value);
    setDialog({ title: t(editor.profile ? "profiles.unsaved" : "profiles.newNotSaved"),
      message: t("profiles.saveBeforeReview"), cancelLabel: t("profiles.keepEditing"), confirmLabel: t("profiles.edit"),
      details: <div className="profile-review-diff-scroll"><table className="profile-review-diff">
        <caption>{t("profiles.unsaved")}</caption>
        <thead><tr><th scope="col">{t("runtime.profileDiffField")}</th>
          <th scope="col">{t("runtime.profileDiffBefore")}</th><th scope="col">{t("runtime.profileDiffAfter")}</th></tr></thead>
        <tbody>{changes.map((change) => <tr key={change.path}>
          <th scope="row">{change.label}</th>
          <td><pre>{displayValue(change.before, change.beforePresent, change.secret)}</pre></td>
          <td><pre>{displayValue(change.after, change.afterPresent, change.secret)}</pre></td>
        </tr>)}</tbody>
      </table></div>,
      onConfirm: () => { pendingFocus.current = "profile-name"; setDialog(null); } });
    return true;
  };
  const reviewSelection = async (makeBindings: () => RuntimePlanRequest["bindings"], refreshSlots: string[]) => {
    if (requireSavedProfile()) return;
    setBusy(true); setError(""); setNotice(null);
    try {
      const bindings = makeBindings();
      const expectedProfiles = profiles.filter((profile) => bindings.some((binding) => "profile_id" in binding
        && binding.profile_id === profile.profile_id && (refreshSlots.includes(binding.slot)
          || activation?.bindings.find((current) => current.slot === binding.slot)?.profile_id !== binding.profile_id)));
      return await runtime.review({ module, bindings, expected_revision: activation?.revision ?? null,
        refresh_slots: refreshSlots, include_host_settings: false }, undefined, expectedProfiles);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const persistProfile = async (copy = false, name = editor.name) => {
    if (!definition) throw new Error(t("profiles.schemaRequired"));
    const processingInput = slot.kind === "decoder" ? editor.processingInput ?? undefined : undefined;
    return editor.profile && !copy
      ? api.update(editor.profile, name, editor.parameters, processingInput)
      : api.create(slot.kind, definition.selection, name, editor.parameters, processingInput);
  };
  const save = async (copy = false, name = editor.name) => {
    setBusy(true); setError(""); setNotice(null);
    try {
      const saved = await persistProfile(copy, name);
      setProfiles((current) => [...current.filter((profile) => profile.profile_id !== saved.profile_id), saved]);
      resetEditor(editorFor(saved, definitions));
      setNotice("profiles.saved");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const connectSource = async () => {
    if (sourceOperation.current || connectReason || !activation) return;
    sourceOperation.current = true;
    metadataGeneration.current += 1; setSourceMetadata(null);
    setBusy(true); setError(""); setNotice(null);
    try { await acquisition.connect.run(activation.revision); }
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
    setEditor({ ...editor, parameters }); setNotice(null);
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
      {translatePluginName(item.plugin_type, item.label, t)} · {item.selection.version ?? t("profiles.unversioned")} · {item.selection.package_id}/{item.selection.contribution_id}
      {item.available ? "" : t("profiles.platformUnavailable")}{item.schema ? "" : t("profiles.noSchemaSuffix")}
    </option>)}
    {missingPlugins.map(([key, selection]) => <option key={key} value={key}>
      {selection.package_id}/{selection.contribution_id} · {selection.version ?? t("profiles.unversioned")} {t("profiles.unavailable")}
    </option>)}
  </>;

  const blockedReason = runtimeBlockReason(runtime);
  const reviewReason = busy ? "feedback.busy" : needsSave ? null
    : !noTrigger && !definition?.available ? "profiles.pluginUnavailable"
      : slot.kind !== "filter" ? !engineAvailable ? "feedback.runtimeUnavailable" : blockedReason : null;
  const saveReason = busy ? "feedback.busy" : !definition?.schema ? "profiles.schemaRequired"
    : errors.length > 0 ? "profiles.fixErrors" : !editor.name.trim() ? "profiles.nameRequired"
    : !dirty && editor.profile ? "profiles.nothingToSave" : null;
  const connectReason = busy ? "feedback.busy" : blockedReason
    ?? (!engineAvailable || !sourceStatus ? "profiles.sourceUnavailable" : sourceStatus.connected ? "profiles.sourceAlreadyConnected"
      : !definition?.available ? "profiles.pluginUnavailable" : errors.length > 0 ? "profiles.fixErrors"
      : !sourceSelected ? "profiles.applySourceBeforeConnect" : null);
  const disconnectReason = busy ? "feedback.busy" : blockedReason
    ?? (!sourceStatus ? "profiles.sourceUnavailable" : !sourceStatus.connected ? "profiles.sourceDisconnectedStatus"
      : sourceStatus.streaming ? "profiles.stopBeforeConnect" : null);
  const chainReason = busy ? "feedback.busy" : !engineAvailable ? "feedback.runtimeUnavailable" : blockedReason
    ?? (!chainDirty ? "profiles.chainUnchanged" : chain.some((item) => !item.profile_id) ? "profiles.saveEveryProfile" : null);
  const profileAction = <div className="editor-action"><button className="primary" disabled={!!reviewReason}
          aria-describedby="profile-review-help"
          onClick={() => {
            if (requireSavedProfile()) return;
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
          }}>{t(slot.kind === "filter" ? "profiles.addFilter" : "profiles.reviewProfile")}</button>
    <p className="action-help" id="profile-review-help">{t(reviewReason ?? (needsSave ? "profiles.saveBeforeReview" : slot.kind === "filter" ? "profiles.addFilterHelp" : "profiles.reviewHelp"))}</p>
  </div>;

  return <section className={`settings-workspace plugin-profiles module-${module}`} onKeyDown={(event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "s" && !event.defaultPrevented) {
      event.preventDefault();
      if (!event.repeat && !event.nativeEvent?.isComposing && !dialog && !runtime.plan && !noTrigger) {
        if (!saveReason) {
          saveFocus.current = typeof document === "undefined" ? null : document.activeElement as HTMLElement | null;
          save();
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
          {" / "}{selectedProfile?.name ?? selectedBinding.profile_id ?? t("profiles.currentSourceConfiguration")}
          {selectedBinding.profile_id && <> · {t("profiles.revision", { revision: selectedBinding.profile_revision })}</>}</span>
        {(slot.kind === "acquisition" ? !sourceSelected : selectedProfile && selectedProfile.profile_id !== editor.profile?.profile_id) && <button disabled={busy}
          onClick={() => changeEditor(() => resetEditor(slot.kind === "acquisition"
            ? editorForSource(selectedBinding, profiles) : editorFor(selectedProfile, definitions)))}>{t(slot.kind === "acquisition" ? "profiles.viewCurrentConfiguration" : "profiles.viewSelected")}</button>}
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
            <button disabled={busy || !definition?.schema || errors.length > 0 || legacyDecoder} onClick={() => setDialog({ title: t("profiles.duplicate"),
              message: t("profiles.duplicateMessage"),
              confirmLabel: t("profiles.create"), initialValue: t("profiles.copyName", { name: editor.name || (definition ? translatePluginName(definition.plugin_type, definition.label, t) : t(slot.label)) }),
              onConfirm: (name) => { setDialog(null); void save(true, name); } })}>{t("profiles.duplicate")}</button>
          </div></>}
          {noTrigger && <p className="setting-description">{t("profiles.noTriggerHint")}</p>}
          {slot.kind === "acquisition" && <p className="setting-description">{t(sourceMode === "new"
            ? reportedSource ? "profiles.newSourceHint" : "profiles.newDeclaredSourceHint" : "profiles.sourceConfigurationHint")}</p>}
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
        {!noTrigger && <><div className="setting-group profile-identity">
          <h2>{t(editor.profile ? "profiles.edit" : "profiles.newProfile")}</h2>
          <label className="setting-row"><span className="setting-label">{t("profiles.name")}</span>
            <input id="profile-name" value={editor.name} maxLength={64} disabled={busy} onChange={(event) => {
              setEditor({ ...editor, name: event.target.value }); setNotice(null);
            }} /></label>
        </div>
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
              : runtime.status.applied_revision !== activation?.revision ? t("profiles.currentSourceUnknown")
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
          {onRun && <button className="text-button" disabled={busy} onClick={() => changeEditor(onRun)}>{t("profiles.openRun")}</button>}
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
          configurationRevision={`${runtime.status?.accepted_revision}:${runtime.status?.applied_revision}`}
          onChange={(processingInput) => { setEditor({ ...editor, processingInput }); setNotice(null); }} /></div>}
        {errors.length > 0 && <ul className="schema-errors" role="alert">{errors.map((message) => <li key={message}>{message}</li>)}</ul>}
        {selectedBindings.length > 0 && <p className="setting-description">{t("profiles.revisions", { selected: selectedBindings.map((item) => item.profile_revision).join(", "), saved: editor.profile?.revision ?? "" })}</p>}

      </div>
      <footer className="editor-footer"><div className="editor-feedback">
        <div className="save-state" role="status">{noTrigger ? t("profiles.noTrigger")
          : dirty ? <><span className="unsaved-dot" />{t("profiles.unsaved")}</>
          : editor.profile ? t("profiles.profileSaved") : t("profiles.newNotSaved")}
          {slot.kind === "acquisition" && !sourceSelected && <span className="pending-state">{t("profiles.sourceUnappliedChanges")}</span>}
          {chainDirty && <span className="pending-state">{t("profiles.chainPending")}</span>}</div>
        {!noTrigger && <p className="action-help" id="profile-save-help">{t(saveReason ?? "profiles.saveHelp")}</p>}
        <RuntimeConfigurationProgress runtime={runtime} />
      </div>
        <div className="actions">
          <button disabled={busy} onClick={() => changeEditor(() => { void run(async () => slot.kind === "acquisition" && sourceMode === "current" ? "current" : editor.profile?.profile_id ?? "new", "profiles.refreshed"); }, true)}>{t("profiles.reload")}</button>
          {!noTrigger && <>{editor.profile && <button className="text-button danger" disabled={busy || selectedBindings.length > 0 || chain.some((item) => item.profile_id === editor.profile?.profile_id)}
            onClick={() => setDialog({ title: t("profiles.deleteProfile"), message: t("profiles.deletePrompt", { name: editor.profile!.name }), confirmLabel: t("profiles.deleteProfile"),
              onConfirm: () => { setDialog(null); void run(async () => { await api.delete(editor.profile!); return "new"; }, "profiles.deleted", true); } })}>{t("profiles.delete")}</button>}
          <button disabled={busy || (!dirty && !chainDirty)} onClick={() => changeEditor(() => {
            resetEditor(structuredClone(baseline)); setChain(savedChain);
          }, true)}>{t("profiles.discard")}</button>
          <button className="primary" disabled={!!saveReason} aria-describedby="profile-save-help" aria-keyshortcuts="Control+s Meta+s"
            onClick={() => void save()}>{t("profiles.save")}</button></>}
        </div>
      </footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
