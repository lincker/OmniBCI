/** Editable plugin values live independently of saved profiles and mounted pages. */
import { useCallback, useEffect, useRef, useState } from "react";
import { pluginConfigApi, selectionKey, moduleBindings, type ModulesResponse, type ModuleActivation, type RevisionMap, type PluginProfile, type PluginSelection } from "../api/plugin-config";
import type { ProcessingInput } from "../api/signal-config";
import type { RuntimePlanRequest } from "../api/runtime-config";
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
  filterOrder: string[], activation: ModulesResponse | null): RuntimePlanRequest["bindings"] {
  const values: Record<string, CurrentPluginConfiguration> = Object.fromEntries(
    (activation?.modules[module]?.bindings ?? [])
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

export function useCurrentConfiguration(acceptedRevisions?: RevisionMap, restoration?: { module: ConfigurationModule; operationId: string; revision: string } | null) {
  const [entries, setEntries] = useState<Record<string, CurrentPluginConfiguration>>({});
  const [filterOrder, updateFilterOrder] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [baseline, setBaseline] = useState<ModulesResponse | null>(null);
  const loadGeneration = useRef(0);
  const initialized = useRef(false);
  const restoredOperation = useRef<string | null>(null);
  const available = useRef(false);
  const values = useRef(entries);
  const order = useRef(filterOrder);
  const initialize = useCallback((activation: ModulesResponse | null) => {
    available.current = true;
    setBaseline(activation); setReady(true); setError(null);
    if (initialized.current) return;
    initialized.current = true;
    values.current = Object.fromEntries(moduleBindings(activation).map((binding) => [binding.slot, currentFromBinding(binding)]));
    order.current = moduleBindings(activation).filter((binding) => binding.slot.startsWith("processing.filter.")).map((binding) => binding.slot);
    setEntries(values.current); updateFilterOrder(order.current);
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
    setBaseline((previous) => previous ? { ...previous, modules: { ...previous.modules, [module]: activation } } : previous);
  }, []);
  const reload = useCallback(async () => {
    const generation = ++loadGeneration.current;
    try {
      const activation = await pluginConfigApi.modules();
      if (generation !== loadGeneration.current) return;
      initialize(activation); setError(null);
      if (restoration && restoredOperation.current !== restoration.operationId) {
        const restored = activation.modules[restoration.module];
        restoredOperation.current = restoration.operationId;
        if (restored.revision !== restoration.revision) throw new Error("The restored module changed again. Refresh and inspect its current values before continuing.");
        restoreModule(restored);
      }
    } catch (cause) {
      if (generation === loadGeneration.current) {
        available.current = false; setReady(false); setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
  }, [initialize, restoreModule, restoration?.operationId]);
  useEffect(() => { void reload(); return () => { loadGeneration.current += 1; }; }, [reload, JSON.stringify(acceptedRevisions)]);
  const get = useCallback((slot: string) => values.current[slot], []);
  const set = useCallback((slot: string, value: CurrentPluginConfiguration) => {
    values.current = { ...values.current, [slot]: structuredClone(value) };
    setEntries(values.current);
  }, []);
  const setFilterOrder = useCallback((slots: string[]) => {
    order.current = [...slots]; updateFilterOrder(order.current);
  }, []);
  const validationError = useCallback((module: ConfigurationModule): string | null => {
    const decoder = values.current["processing.decoder"];
    return module === "processing" && decoder?.selection && (decoder.requiresProcessingInputReview || !decoder.processingInput)
      ? "Configure the decoder input before applying or starting processing." : null;
  }, []);
  const request = useCallback((module: ConfigurationModule, activation: ModulesResponse | null): RuntimePlanRequest => {
    if (!available.current) throw new Error("Current configuration is unavailable. Refresh before continuing.");
    const validation = validationError(module);
    if (validation) throw new Error(validation);
    return { module, bindings: currentBindings(module, values.current, order.current, activation),
      expected_revision: activation?.modules[module]?.revision ?? null };
  }, [validationError]);
  const hasChanges = useCallback((module: ConfigurationModule, activation: ModulesResponse | null) => {
    if (!initialized.current) return false;
    const previousOrder = moduleBindings(activation).filter((binding) => binding.slot.startsWith("processing.filter.")).map((binding) => binding.slot);
    const before = currentBindings(module, {}, previousOrder, activation);
    const after = currentBindings(module, values.current, order.current, activation);
    const normalized = (bindings: RuntimePlanRequest["bindings"]) => [...bindings].sort((a, b) => {
      if (a.slot.startsWith("processing.filter.") && b.slot.startsWith("processing.filter.")) return 0;
      return a.slot.localeCompare(b.slot);
    });
    return !sameParameters(normalized(before), normalized(after));
  }, []);
  const dirty = (["acquisition", "processing", "stimulation", "actuation"] as const).some((module) => hasChanges(module, baseline));
  return { entries, filterOrder, ready, error, dirty, baseline, restoreModule, get, set, setFilterOrder, request, hasChanges, validationError, reload };
}

export type CurrentConfigurationController = ReturnType<typeof useCurrentConfiguration>;
