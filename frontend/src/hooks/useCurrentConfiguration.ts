/** Editable plugin values live independently of saved profiles and mounted pages. */
import { useCallback, useEffect, useRef, useState } from "react";
import { pluginConfigApi, selectionKey, configurationModules, type ModuleActivation, type RevisionMap, type PluginProfile, type PluginSelection } from "../api/plugin-config";
import type { ProcessingInput } from "../api/signal-config";
import type { ModuleConfigurationRequest } from "../api/runtime-config";
import { sameParameters, normalizedParameters, type ConfigurationSchema } from "../SchemaForm";

export type ConfigurationModule = "acquisition" | "processing" | "stimulation" | "actuation";
export type CurrentPluginConfiguration = {
  sourceOrigin?: "new" | "current" | "profile";
  profile: PluginProfile | null;
  selection: PluginSelection | null;
  configurationVersion?: number;
  schema?: ConfigurationSchema | null;
  requiresProcessingInputReview?: boolean;
  plugin: string;
  name: string;
  parameters: Record<string, unknown>;
  processingInput: ProcessingInput | null;
};
export const emptyCurrentConfiguration: CurrentPluginConfiguration = {
  profile: null, selection: null, plugin: "", name: "", parameters: {}, processingInput: null,
};

export function currentFromBinding(binding: ModuleActivation["bindings"][number]): CurrentPluginConfiguration {
  return { sourceOrigin: "current", profile: null, selection: structuredClone(binding.selection),
    configurationVersion: binding.configuration_version,
    requiresProcessingInputReview: binding.kind === "decoder" && !binding.processing_input, plugin: selectionKey(binding.selection), name: "",
    parameters: structuredClone(binding.parameters), processingInput: binding.processing_input
      ? { ...structuredClone(binding.processing_input), outputs: structuredClone(binding.processing_input.outputs ?? []) } : null };
}

export function currentBindings(module: ConfigurationModule, entries: Record<string, CurrentPluginConfiguration>,
  filterOrder: string[], activation: ModuleActivation | null): ModuleConfigurationRequest["bindings"] {
  const values: Record<string, CurrentPluginConfiguration> = Object.fromEntries(
    (activation?.bindings ?? [])
      .map((binding) => [binding.slot, currentFromBinding(binding)]));
  for (const [slot, value] of Object.entries(entries)) {
    if (slot.startsWith(`${module}.`) && slot !== "processing.filters") values[slot] = value;
  }
  const slots = Object.keys(values).filter((slot) => !slot.startsWith("processing.filter."));
  if (module === "processing") slots.push(...filterOrder);
  return slots.flatMap((slot) => {
    const value = values[slot];
    if (!value?.selection) return [];
    return [{ slot, selection: structuredClone(value.selection), parameters: normalizedParameters(value.parameters, value.schema),
      ...(value.configurationVersion === undefined ? {} : { configuration_version: value.configurationVersion }),
      ...(slot === "processing.decoder" ? { processing_input: structuredClone(value.processingInput) } : {}) }];
  });
}

export type ModuleCurrentState = { activation: ModuleActivation | null; ready: boolean; error: string | null; conflicted?: boolean };
export type ModuleRestoration = { module: ConfigurationModule; operationId: string; revision: string };
const emptyModule = (): ModuleCurrentState => ({ activation: null, ready: false, error: null });

export function useCurrentConfiguration(acceptedRevisions?: RevisionMap,
  restorations: Partial<Record<ConfigurationModule, ModuleRestoration>> = {}) {
  const [entries, setEntries] = useState<Record<string, CurrentPluginConfiguration>>({});
  const [filterOrder, updateFilterOrder] = useState<string[]>([]);
  const [settings, updateSettings] = useState<Partial<Record<ConfigurationModule, Record<string, unknown>>>>({});
  const settingValues = useRef(settings);
  const baselines = useRef<Partial<Record<ConfigurationModule, ModuleConfigurationRequest>>>({});
  const [modules, setModules] = useState<Record<ConfigurationModule, ModuleCurrentState>>(() =>
    Object.fromEntries(configurationModules.map((module) => [module, emptyModule()])) as Record<ConfigurationModule, ModuleCurrentState>);
  const states = useRef(modules);
  const loadGeneration = useRef<Partial<Record<ConfigurationModule, number>>>({});
  const initialized = useRef(new Set<ConfigurationModule>());
  const restoredOperation = useRef<Partial<Record<ConfigurationModule, string>>>({});
  const observed = useRef<Partial<Record<ConfigurationModule, string>>>({});
  const restorationRef = useRef(restorations);
  restorationRef.current = restorations;
  const values = useRef(entries);
  const order = useRef(filterOrder);
  const updateModule = useCallback((module: ConfigurationModule, state: ModuleCurrentState) => {
    states.current = { ...states.current, [module]: state };
    setModules(states.current);
  }, []);
  const restoreModule = useCallback((activation: ModuleActivation) => {
    const module = activation.module;
    const remaining = Object.fromEntries(Object.entries(values.current).filter(([slot]) => !slot.startsWith(`${module}.`)));
    values.current = { ...remaining, ...Object.fromEntries(activation.bindings.map((binding) => [binding.slot, currentFromBinding(binding)])) };
    setEntries(values.current);
    if (module === "processing") {
      order.current = activation.bindings.filter((binding) => binding.kind === "filter").map((binding) => binding.slot);
      updateFilterOrder(order.current);
    }
    settingValues.current = { ...settingValues.current, [module]: structuredClone(activation.settings) };
    updateSettings(settingValues.current);
    baselines.current[module] = { expected_revision: activation.revision,
      settings: structuredClone(activation.settings),
      bindings: currentBindings(module, {}, activation.bindings.filter((binding) => binding.kind === "filter").map((binding) => binding.slot), activation) };
    updateModule(module, { activation, ready: true, error: null });
  }, [updateModule]);
  const draftChanged = (module: ConfigurationModule) => {
    const baseline = baselines.current[module];
    return !!baseline && (!sameParameters(baseline.settings, settingValues.current[module])
      || !sameParameters(baseline.bindings, currentBindings(module, values.current, order.current, null)));
  };
  const reload = useCallback(async (module: ConfigurationModule, replaceCurrent = false) => {
    const generation = (loadGeneration.current[module] ?? 0) + 1;
    loadGeneration.current[module] = generation;
    try {
      const activation = await pluginConfigApi.module(module);
      if (generation !== loadGeneration.current[module]) return;
      const restoration = restorationRef.current[module];
      if (replaceCurrent) restoreModule(activation);
      else if (restoration && restoredOperation.current[module] !== restoration.operationId) {
        restoredOperation.current[module] = restoration.operationId;
        if (activation.revision !== restoration.revision) throw new Error("The restored module changed again. Refresh and inspect its current values before continuing.");
        restoreModule(activation);
      } else if (!initialized.current.has(module)
        || baselines.current[module]?.expected_revision !== activation.revision && !draftChanged(module)) restoreModule(activation);
      initialized.current.add(module);
      updateModule(module, { activation, ready: true, error: null,
        conflicted: baselines.current[module]?.expected_revision !== activation.revision });
      return activation;
    } catch (cause) {
      if (generation === loadGeneration.current[module]) updateModule(module, {
        ...states.current[module], ready: false, error: cause instanceof Error ? cause.message : String(cause),
      });
    }
  }, [restoreModule, updateModule]);
  useEffect(() => {
    for (const module of configurationModules) {
      const key = JSON.stringify([acceptedRevisions?.[module], restorations[module]?.operationId]);
      if (observed.current[module] !== key) { observed.current[module] = key; void reload(module); }
    }
  }, [reload, JSON.stringify(acceptedRevisions), JSON.stringify(restorations)]);
  useEffect(() => () => {
    for (const module of configurationModules) loadGeneration.current[module] = (loadGeneration.current[module] ?? 0) + 1;
  }, []);
  const get = useCallback((slot: string) => values.current[slot], []);
  const set = useCallback((slot: string, value: CurrentPluginConfiguration) => {
    values.current = { ...values.current, [slot]: structuredClone(value) };
    setEntries(values.current);
  }, []);
  const setFilterOrder = useCallback((slots: string[]) => {
    order.current = [...slots]; updateFilterOrder(order.current);
  }, []);
  const setSetting = useCallback((module: ConfigurationModule, key: string, value: unknown) => {
    settingValues.current = { ...settingValues.current, [module]: { ...settingValues.current[module], [key]: value } };
    updateSettings(settingValues.current);
  }, []);
  const acknowledge = useCallback((module: ConfigurationModule, submitted: ModuleConfigurationRequest, revision: string) => {
    baselines.current[module] = structuredClone({ ...submitted, expected_revision: revision });
    void reload(module);
  }, [reload]);
  const validationError = useCallback((module: ConfigurationModule): string | null => {
    const baseline = settingValues.current.processing?.["ALGORITHM.baseline_duration"];
    if (module === "processing" && baseline !== undefined && (typeof baseline !== "number" || !Number.isFinite(baseline) || baseline < 0)) {
      return "Pre-trigger duration must be a non-negative number.";
    }
    const decoder = values.current["processing.decoder"];
    return module === "processing" && decoder?.selection && (decoder.requiresProcessingInputReview || !decoder.processingInput)
      ? "Configure the decoder input before applying or starting processing." : null;
  }, []);
  const request = useCallback((module: ConfigurationModule): ModuleConfigurationRequest => {
    if (!states.current[module].ready) throw new Error("Current configuration is unavailable. Refresh before continuing.");
    const validation = validationError(module);
    if (validation) throw new Error(validation);
    return { settings: structuredClone(settingValues.current[module] ?? {}),
      bindings: currentBindings(module, values.current, order.current, null),
      expected_revision: baselines.current[module]?.expected_revision ?? null };
  }, [validationError]);
  const hasChanges = useCallback((module: ConfigurationModule, activation: ModuleActivation | null) => {
    if (!initialized.current.has(module)) return false;
    const previousOrder = (activation?.bindings ?? []).filter((binding) => binding.slot.startsWith("processing.filter.")).map((binding) => binding.slot);
    const before = currentBindings(module, {}, previousOrder, activation);
    const after = currentBindings(module, values.current, order.current, activation);
    const normalized = (bindings: ModuleConfigurationRequest["bindings"]) => [...bindings].sort((a, b) => {
      if (a.slot.startsWith("processing.filter.") && b.slot.startsWith("processing.filter.")) return 0;
      return a.slot.localeCompare(b.slot);
    });
    return !sameParameters(normalized(before), normalized(after))
      || !sameParameters(settingValues.current[module] ?? {}, activation?.settings ?? {});
  }, []);
  const dirty = configurationModules.some(draftChanged);
  return { entries, filterOrder, settings, modules, dirty, restoreModule, get, set, setFilterOrder, setSetting, acknowledge, request, hasChanges, validationError, reload,
    loadAccepted: (module: ConfigurationModule) => reload(module, true) };
}

export type CurrentConfigurationController = ReturnType<typeof useCurrentConfiguration>;
