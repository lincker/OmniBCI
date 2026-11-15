/** Module configurations used by console tests; runtime APIs always receive separate sections. */
export const moduleNames = ["acquisition", "processing", "stimulation", "actuation"];
export const revisionMap = (revision) => ({
  ...Object.fromEntries(moduleNames.map((module) => [module, revision ?? null])),
  engine: revision ? "engine-revision" : null,
});
export function moduleConfiguration({ revision, bindings = [], settings = {}, revisions = revisionMap(revision) } = {}) {
  return {
    modules: Object.fromEntries(moduleNames.map((module) => [module, {
      module, revision: revisions[module],
      bindings: bindings.filter((binding) => binding.slot.startsWith(`${module}.`)),
      settings: Object.fromEntries(Object.entries(settings).filter(([key]) => key.startsWith(`${module.toUpperCase()}.`)
        || module === "processing" && key === "ALGORITHM.baseline_duration")),
    }])),
    engine: { revision: revisions.engine, settings: {} },
  };
}

/** Scoped controller stubs for presentation tests. Real command tests use the hooks. */
export function runtimeController(value = {}) {
  return { status: { operations: {}, recovery: {}, accepted: {}, applied: null },
    async execute(module, configuration, send, onApplied) {
      const operation = await send({ operation_id: crypto.randomUUID(), configuration });
      if (operation?.result?.configuration_applied) onApplied?.(operation);
      return operation;
    },
    ...value,
    state(module) {
      const operation = this.status?.operations?.[module] ?? this.operation ?? null;
      const scoped = this.scopes?.[module] ?? {};
      return { operation, blocked: false, inProgress: false, uncertain: false, reviewing: false, submitting: false,
        recoveryRequired: false, requestError: null, ...this, ...scoped,
        ownUncertain: scoped.uncertain ?? this.uncertain ?? false,
        recoveryError: this.status?.recovery?.[module] ?? (this.recoveryRequired ? "Recovery required" : undefined) };
    },
  };
}

export function currentController(activation, value = {}) {
  const selected = activation?.modules ? activation : moduleConfiguration(activation ?? {});
  return { entries: {}, filterOrder: [], settings: Object.fromEntries(moduleNames.map((module) => [module, selected.modules[module].settings])), acknowledge: () => {}, setSetting: () => {},
    hasChanges: () => false, validationError: () => null,
    request: (module) => ({ expected_revision: selected.modules[module].revision,
      settings: selected.modules[module].settings, bindings: selected.modules[module].bindings }),
    ...value, modules: Object.fromEntries(moduleNames.map((module) => [module, {
      activation: selected.modules[module], ready: value.ready ?? true, error: value.error ?? null,
      ...value.modules?.[module],
    }])) };
}

export function controlsController(value = {}) {
  const noop = async () => {};
  return { notices: {}, pending: () => false, enginePending: false, busy: false,
    stimulation: { data: null, error: null, refresh: noop }, signal: { data: null, error: null, refresh: noop },
    ...Object.fromEntries(["start", "stop", "launch", "stopStim", "trigger", "readCurrent", "stopRead", "startProcessing", "stopProcessing", "startActuation", "stopActuation"].map((name) => [name, { pending: false, error: null, run: noop }])),
    ...value };
}
