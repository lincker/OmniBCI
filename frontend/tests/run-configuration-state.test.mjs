/** Real load effects distinguish a first-use configuration from failed requests. */
import assert from "node:assert/strict";
import { moduleConfiguration, revisionMap, runtimeController, currentController } from "./module-fixtures.mjs";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { useCurrentConfiguration } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { useRuntimeControls } = await server.ssrLoadModule("/src/hooks/useRuntimeControls.ts");
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { signalConfigApi } = await server.ssrLoadModule("/src/api/signal-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const { currentFromBinding, currentBindings } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const noop = () => {};
const selection = (id) => ({ package_id: `example.${id}`, contribution_id: "main", version: "1.0.0" });
const source = { slot: "acquisition.source", kind: "acquisition", selection: selection("source"), parameters: {} };
const decoder = { slot: "processing.decoder", kind: "decoder", selection: selection("decoder"), parameters: {} };
const detector = { slot: "processing.epoch_detector", kind: "epoch_detector", selection: selection("detector"), parameters: {} };
const paradigm = { kind: "paradigm", selection: selection("visual"), plugin_type: "visual", label: "Visual trials", available: true, controls: {} };
const sourceOnly = { revision: "source-only", bindings: [source] };
const complete = { revision: "complete", bindings: [source, decoder, detector,
  { slot: "processing.filter.first", kind: "filter", selection: selection("filter"), parameters: {} },
  { slot: "stimulation.paradigm", kind: "paradigm", selection: paradigm.selection, parameters: {} },
  { slot: "stimulation.trigger", kind: "trigger", selection: selection("trigger"), parameters: {} },
  { slot: "actuation.effector", kind: "effector", selection: selection("effector"), parameters: {} }] };

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function region(tree, label) {
  const result = elements(tree).find((item) => item.props["aria-label"] === label);
  assert.ok(result, `Missing ${label}`);
  return result;
}
function button(tree, label) {
  const result = elements(tree).find((item) => item.type === "button" && text(item).trim() === label);
  assert.ok(result, `Missing ${label}`);
  return result;
}
function alerts(tree) {
  return elements(tree).filter((item) => item.props.role === "alert").map(text).join(" ");
}
function preparation(tree) {
  const result = elements(region(tree, "Active decoder input")).find((item) => item.type === "div"
    && item.props.children?.[0]?.props.children === "Signal preparation");
  assert.ok(result);
  return text(result);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const sameDeps = (left, right) => !!left && !!right && left.length === right.length
  && left.every((value, index) => Object.is(value, right[index]));

function componentHarness(props) {
  const hooks = [];
  let cursor = 0, dirty = true, tree;
  let effects = [];
  const memo = (factory, deps) => {
    const index = cursor++;
    if (!hooks[index] || !sameDeps(hooks[index].deps, deps)) hooks[index] = { value: factory(), deps };
    return hooks[index].value;
  };
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (next) => {
        const value = typeof next === "function" ? next(hooks[index].value) : next;
        if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; dirty = true; }
      }];
    },
    useRef(initial) { const index = cursor++; return hooks[index] ??= { current: initial }; },
    useCallback: (callback, deps) => memo(() => callback, deps),
    useMemo: memo,
    useEffect(effect, deps) {
      const index = cursor++;
      if (!hooks[index] || !sameDeps(hooks[index].deps, deps)) {
        const cleanup = hooks[index]?.cleanup;
        hooks[index] = { deps, cleanup };
        effects.push(() => { cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  function render() {
    const internal = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
    const previous = internal.current;
    internal.current = dispatcher; cursor = 0; dirty = false;
    try {
      const loaded = useCurrentConfiguration(props.runtime.status.accepted);
      const overrides = props.current;
      const current = { ...loaded, ...overrides, modules: Object.fromEntries(Object.entries(loaded.modules).map(([module, state]) => [module, {
        ...state, ...(overrides?.ready === undefined ? {} : { ready: overrides.ready }), ...(overrides?.error === undefined ? {} : { error: overrides.error }),
      }])) };
      const runtime = runtimeController(props.runtime);
      const runtimeControls = useRuntimeControls(runtime, current, props.acquisition, props.engine.refresh);
      tree = RunConsole({ ...props, current, runtime, runtimeControls });
    } finally { internal.current = previous; }
    const pending = effects; effects = [];
    pending.forEach((effect) => effect());
  }
  return {
    get tree() { return tree; }, render,
    updateRuntimeState(value) { Object.assign(props.runtime, value); dirty = true; },
    updateRuntime(status) { props.runtime = { ...props.runtime, status: { ...props.runtime.status, ...status } }; dirty = true; },
    async settle() {
      for (let round = 0; round < 80; round++) {
        if (dirty) render();
        await Promise.resolve();
      }
      assert.equal(dirty, false, "Run did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

async function withScenario({ activation = async () => null, acceptedRevision = null,
  profiles = async () => [], catalog = async () => [paradigm], running = false, appliedRevisions,
  current, applyCurrentSelection, processingRunning = running, actuationRunning = false, actuationConnected = false,
  sourceStatus = { connected: false, streaming: false },
  stimulationStatus = { active: false, process_alive: false, engine_connected: false, paradigm: null } } = {}, action) {
  const original = { api: { ...api }, plugin: { ...pluginConfigApi }, signal: { ...signalConfigApi },
    runtime: { ...runtimeConfigApi }, document: globalThis.document };
  const calls = [];
  const backendStatus = { accepted: revisionMap(acceptedRevision), applied: appliedRevisions ?? (running ? revisionMap(acceptedRevision) : null),
    running, recovery: {}, generation: running ? "generation-1" : null, operations: {} };
  globalThis.document = { hidden: false };
  api.stimulationStatus = async () => stimulationStatus;
  signalConfigApi.get = async () => ({ readiness: { ready: true }, active_input: null });
  pluginConfigApi.modules = async () => moduleConfiguration(await activation() ?? {});
  pluginConfigApi.module = async (module) => (await pluginConfigApi.modules()).modules[module];
  pluginConfigApi.profiles = profiles;
  pluginConfigApi.catalog = catalog;
  runtimeConfigApi.status = async () => structuredClone(backendStatus);
  for (const name of ["engineStart", "engineStop", "acqConnect", "acqDisconnect", "acqStreamStart", "acqStreamStop", "stimStart", "stimStop", "trialStart", "moduleStart", "moduleStop", "modulePrepare"]) {
    api[name] = async (...args) => { calls.push([name, ...args]); };
  }
  const props = {
    engine: { data: { running, acquisition_alive: running, processor_alive: processingRunning, actuation_alive: actuationRunning, actuation_connected: actuationConnected }, error: null, refresh: async () => {} },
    acquisition: { data: sourceStatus, error: null, pending: false, refresh: async () => { props.runtime.status = structuredClone(backendStatus); }, cancelPending: noop,
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name,
        { pending: false, error: null, run: async (...args) => { calls.push([name, ...args]); } }])) },
    runtime: { status: structuredClone(backendStatus), blocked: false, inProgress: false, refresh: async () => { props.runtime.status = structuredClone(backendStatus); },
      applyCurrentSelection: async (request) => {
        calls.push(["applyCurrentSelection", structuredClone(request)]);
        assert.ok(applyCurrentSelection, "Unexpected current configuration application");
        return applyCurrentSelection(request, backendStatus);
      } },
    current,
    feed: { generation: null, trials: [], acks: [] }, onBusyChange: noop,
    onEditSetup: () => calls.push(["editSetup"]), onResults: noop,
  };
  api.engineStatus = async () => ({ ...props.engine.data });
  const harness = componentHarness(props);
  try { harness.render(); await action({ harness, calls, backendStatus, acquisitionController: props.acquisition }); }
  finally {
    harness.dispose();
    Object.assign(api, original.api); Object.assign(pluginConfigApi, original.plugin); Object.assign(signalConfigApi, original.signal);
    Object.assign(runtimeConfigApi, original.runtime);
    if (original.document === undefined) delete globalThis.document; else globalThis.document = original.document;
  }
}

test("a successful empty activation is first-use guidance, not a failed request", async () => {
  const pending = deferred();
  await withScenario({ activation: () => pending.promise }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Module configurations")), /Loading/);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    pending.resolve(null); await harness.settle();
    assert.match(text(harness.tree), /No data source configured/);
    assert.doesNotMatch(text(region(harness.tree, "Module configurations")), /Loading|Unavailable/);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
    assert.equal(alerts(harness.tree), "");
    assert.equal(button(harness.tree, "Start engine").props.disabled, false);
    for (const label of ["Connect", "Start reading"]) assert.equal(button(harness.tree, label).props.disabled, true, label);
    const configure = button(harness.tree, "Configure data source");
    assert.equal(!!configure.props.disabled, false);
    configure.props.onClick();
    assert.deepEqual(calls, [["editSetup"]], "Guidance only navigates; it must not send runtime commands");
  });
});

test("a source-only configuration renders without processing or trial setup requirements", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Module configurations")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Module configurations")), /Loading|Unavailable/);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
    assert.equal(button(harness.tree, "Start engine").props.disabled, false);
    assert.equal(button(harness.tree, "Connect").props.disabled, false, "Step one preserves Connect's existing engine startup behavior");
    assert.equal(button(harness.tree, "Start reading").props.disabled, true);
    assert.deepEqual(calls, []);
  });
});

test("an inline source summary identifies the current configuration without a profile revision", async () => {
  const activation = { ...sourceOnly, bindings: [{ ...source, profile_revision: 0 }] };
  await withScenario({ activation: async () => activation, acceptedRevision: activation.revision }, async ({ harness }) => {
    await harness.settle();
    const summary = text(region(harness.tree, "Module configurations"));
    assert.match(summary, /Current configuration/);
    assert.match(summary, /example\.source\/main/);
    assert.match(summary, /Plugin 1\.0\.0/);
    assert.doesNotMatch(summary, /Profile revision|revision 0/);
  });
});

test("a saved source summary retains its profile name and selected revision", async () => {
  const activation = { ...sourceOnly, bindings: [{ ...source, profile_id: "saved-source", profile_revision: 3 }] };
  await withScenario({ activation: async () => activation, acceptedRevision: activation.revision,
    profiles: async () => [{ profile_id: "saved-source", name: "Lab source", revision: 4 }] }, async ({ harness }) => {
    await harness.settle();
    const summary = text(region(harness.tree, "Module configurations"));
    assert.match(summary, /Lab source/);
    assert.match(summary, /example\.source\/main/);
    assert.match(summary, /Plugin 1\.0\.0/);
    assert.match(summary, /Profile revision 3/);
    assert.doesNotMatch(summary, /not saved as a profile|Profile revision 4/);
  });
});

test("a complete configuration preserves selected modules and processing readiness", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true }, async ({ harness, calls }) => {
    await harness.settle();
    const summary = text(region(harness.tree, "Module configurations"));
    for (const plugin of ["source", "decoder", "detector", "filter", "visual", "trigger", "effector"]) assert.match(summary, new RegExp(`example.${plugin}`));
    assert.doesNotMatch(summary, /Loading|Unavailable/);
    assert.equal(preparation(harness.tree), "Signal preparationReady");
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    assert.deepEqual(calls, []);
  });
});

test("an activation request failure is unavailable, never an empty configuration or indefinite loading", async () => {
  const pending = deferred();
  await withScenario({ activation: () => pending.promise }, async ({ harness, calls }) => {
    await harness.settle();
    pending.reject(new Error("Activation request failed")); await harness.settle();
    assert.match(alerts(harness.tree), /Activation request failed/);
    assert.match(text(region(harness.tree, "Module configurations")), /Unavailable/);
    assert.equal(preparation(harness.tree), "Signal preparationUnavailable");
    assert.doesNotMatch(text(harness.tree), /No data source configured|Loading/);
    assert.equal(button(harness.tree, "Start engine").props.disabled, false);
    for (const label of ["Connect", "Start reading"]) assert.equal(button(harness.tree, label).props.disabled, true, label);
    assert.deepEqual(calls, []);
  });
});

test("a failed stimulation catalog does not hide a valid source or imply acquisition is unavailable", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision,
    catalog: async () => { throw new Error("Paradigm catalog offline"); } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Module configurations")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Module configurations")), /Unavailable|Loading/);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.equal(button(harness.tree, "Launch stimulation").props.disabled, true);
    assert.deepEqual(calls, []);
  });
});

test("an unavailable profile library does not invalidate an accepted source configuration", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision,
    profiles: async () => { throw new Error("Profile library offline"); } }, async ({ harness }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Module configurations")), /example.source/);
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
  });
});

test("changed configuration waits for the current response and ignores an older empty response", async () => {
  const old = deferred(), current = deferred();
  let request = 0;
  await withScenario({ activation: () => (++request === 1 ? old : current).promise }, async ({ harness, calls }) => {
    await harness.settle();
    harness.updateRuntime({ accepted: revisionMap(sourceOnly.revision) }); await harness.settle();
    old.resolve(null); await harness.settle();
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    assert.match(text(region(harness.tree, "Module configurations")), /Loading/);
    assert.equal(button(harness.tree, "Connect").props.disabled, true);
    current.resolve(sourceOnly); await harness.settle();
    assert.match(text(region(harness.tree, "Module configurations")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Module configurations")), /Loading|Unavailable/);
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.deepEqual(calls, []);
  });
});


test("retrying a failed configuration load can recover to the normal first-use state", async () => {
  let requests = 0;
  await withScenario({ activation: async () => {
    if (++requests === 1) throw new Error("Temporary configuration failure");
    return null;
  } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(alerts(harness.tree), /Temporary configuration failure/);
    const retry = button(harness.tree, "Retry loading configuration");
    assert.equal(!!retry.props.disabled, false);
    retry.props.onClick(); await harness.settle();
    assert.equal(requests, 5);
    assert.equal(alerts(harness.tree), "");
    assert.match(text(harness.tree), /No data source configured/);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
    assert.deepEqual(calls, [], "Retry only reloads configuration; it cannot start or connect a runtime");
  });
});


test("reloading configuration cannot temporarily bypass a missing epoch detector", async () => {
  const pending = deferred();
  let requests = 0;
  const missingDetector = { ...complete, bindings: complete.bindings.filter((item) => item.slot !== "processing.epoch_detector") };
  await withScenario({ activation: () => ++requests <= 4 ? Promise.resolve(missingDetector) : pending.promise,
    acceptedRevision: missingDetector.revision, running: true, processingRunning: false, sourceStatus: { connected: true, streaming: false },
    catalog: async () => { throw new Error("Trial catalog temporarily unavailable"); } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(preparation(harness.tree), /Epoch detector missing/);
    assert.equal(button(harness.tree, "Start processing").props.disabled, true);
    button(harness.tree, "Retry loading configuration").props.onClick(); await harness.settle();
    assert.match(preparation(harness.tree), /Epoch detector missing/);
    const read = button(harness.tree, "Start processing");
    assert.equal(read.props.disabled, true, "Loading cannot bypass decoder prerequisites from the accepted configuration");
    await read.props.onClick();
    assert.deepEqual(calls, []);
    assert.equal(button(harness.tree, "Stop engine").props.disabled, false);
    assert.equal(button(harness.tree, "Disconnect").props.disabled, false);
    pending.resolve(missingDetector); await harness.settle();
    assert.equal(button(harness.tree, "Start processing").props.disabled, true);
  });
});

test("retrying invalid trial controls keeps trial submission blocked and stop controls available", async () => {
  const pending = deferred();
  let requests = 0;
  const invalidParadigm = { ...paradigm, controls: { invalid: { field: "unsupported", type: "int" } } };
  await withScenario({ activation: () => ++requests <= 4 ? Promise.resolve(complete) : pending.promise,
    acceptedRevision: complete.revision, running: true, sourceStatus: { connected: true, streaming: true },
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type },
    catalog: async () => [invalidParadigm] }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(alerts(harness.tree), /Unsupported trial control/);
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, true);
    button(harness.tree, "Retry loading configuration").props.onClick(); await harness.settle();
    assert.doesNotMatch(text(region(harness.tree, "Module configurations")), /Loading/);
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, true,
      "Clearing the old error during retry cannot enable controls that have not reloaded");
    const trialForm = elements(harness.tree).find((item) => item.type === "form" && item.props.className?.includes("trial-controls"));
    assert.ok(trialForm);
    trialForm.props.onSubmit({ preventDefault: noop });
    assert.deepEqual(calls, []);
    for (const label of ["Stop engine", "Stop reading", "Stop stimulation"]) {
      assert.equal(button(harness.tree, label).props.disabled, false, label);
    }
    pending.resolve(complete); await harness.settle();
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, true);
  });
});

function currentConfiguration(changes = {}, pending = Object.keys(changes), errors = {}) {
  return {
    ready: true, error: null,
    entries: Object.fromEntries(Object.values(changes).flat().map((binding) => [binding.slot, currentFromBinding(binding)])),
    filterOrder: Object.values(changes).flat().filter((binding) => binding.slot.startsWith("processing.filter.")).map((binding) => binding.slot),
    hasChanges: (module, accepted) => pending.includes(module) && !accepted?.revision?.startsWith("applied-"),
    validationError: (module) => errors[module] ?? null,
    request: (module, accepted) => {
      if (errors[module]) throw new Error(errors[module]);
      return { module, expected_revision: accepted?.revision ?? null,
        bindings: structuredClone(changes[module] ?? accepted?.bindings ?? [])
          .map(({ slot, selection, parameters, processing_input }) => ({ slot, selection, parameters,
            ...(processing_input === undefined ? {} : { processing_input }) })),
      };
    },
  };
}

function appliedConfiguration(accepted, request, status) {
  const revision = `applied-${request.module}`;
  status.accepted = { ...status.accepted, [request.module]: revision, system: "system-revision" };
  if (status.running) status.applied = { ...status.applied, [request.module]: revision, system: "system-revision" };
  return { ...accepted, revisions: { ...status.accepted }, revision, bindings: [...(accepted?.bindings ?? []).filter((item) => !item.slot.startsWith(`${request.module}.`)),
    ...request.bindings] };
}

test("a first unsaved current source can connect without an accepted activation or a saved profile", async () => {
  const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "current-device" } }] });
  await withScenario({ current }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    await button(harness.tree, "Connect").props.onClick();
    assert.deepEqual(calls, [["connect", current.request("acquisition", null)]]);
  });
});

test("a source edited after starting an empty engine can connect using its current values", async () => {
  const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "current-device" } }] });
  await withScenario({ current, running: true }, async ({ harness, calls }) => {
    harness.updateRuntime({ applied: { ...revisionMap(null), acquisition: "empty-acquisition-hash" } });
    await harness.settle();
    const connect = button(harness.tree, "Connect");
    assert.equal(connect.props.disabled, false);
    await connect.props.onClick();
    assert.deepEqual(calls, [["connect", current.request("acquisition", null)]]);
  });
});

test("Start engine starts the foundation without applying any current module values", async () => {
  let accepted = null;
  const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "current-device" } }],
    processing: [decoder, detector] });
  await withScenario({ activation: async () => accepted, current,
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { [request.module]: accepted.revisions[request.module], system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Start engine").props.disabled, false);
    button(harness.tree, "Start engine").props.onClick();
    await harness.settle();
    assert.deepEqual(calls, [["engineStart"]]);
  });
});

for (const actuationRunning of [false, true]) test(`Start reading leaves processing and ${actuationRunning ? "running" : "stopped"} actuation unchanged`, async () => {
  let accepted = complete;
  const current = currentConfiguration({
    processing: [{ ...decoder, parameters: { window_duration: 2 } }, detector],
    actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: { destination: "new" } }],
    stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }],
  });
  await withScenario({ activation: async () => accepted, acceptedRevision: accepted.revision, current, running: true, actuationRunning,
    sourceStatus: { connected: true, streaming: false },
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { [request.module]: accepted.revisions[request.module], system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    const read = button(harness.tree, "Start reading");
    assert.equal(read.props.disabled, false);
    await read.props.onClick();
    await harness.settle();
    assert.deepEqual(calls, [["read", { acquisition: complete.revision, system: "system-revision" }]]);
  });
});

test("a rejected current processing configuration never starts processing with the old values", async () => {
  const current = currentConfiguration({ processing: [{ ...decoder, parameters: { window_duration: -1 } }, detector] });
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true, processingRunning: false,
    sourceStatus: { connected: true, streaming: false }, applyCurrentSelection: async () => null }, async ({ harness, calls }) => {
    await harness.settle();
    await button(harness.tree, "Start processing").props.onClick();
    assert.deepEqual(calls, [["applyCurrentSelection", current.request("processing", moduleConfiguration(complete).modules.processing)]]);
  });
});

test("changing the current source while connected requires reconnecting before reading", async () => {
  const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "another-device" } }] });
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision, current, running: true,
    sourceStatus: { connected: true, streaming: false } }, async ({ harness, calls }) => {
    await harness.settle();
    const read = button(harness.tree, "Start reading");
    assert.equal(read.props.disabled, true);
    assert.match(text(harness.tree), /reconnect/i);
    await read.props.onClick();
    assert.deepEqual(calls, []);
    assert.equal(button(harness.tree, "Disconnect").props.disabled, false);
  });
});

test("Launch stimulation applies only the current stimulation configuration", async () => {
  let accepted = complete;
  const current = currentConfiguration({
    stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }],
    processing: [{ ...decoder, parameters: { window_duration: 2 } }, detector],
  });
  await withScenario({ activation: async () => accepted, acceptedRevision: accepted.revision, current, running: true,
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { [request.module]: accepted.revisions[request.module], system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    const launch = button(harness.tree, "Launch stimulation");
    assert.equal(launch.props.disabled, false);
    launch.props.onClick();
    await harness.settle();
    assert.deepEqual(calls, [["applyCurrentSelection", current.request("stimulation", moduleConfiguration(complete).modules.stimulation)], ["stimStart"]]);
  });
});

test("a changed acquisition revision does not prevent foundation startup", async () => {
  const current = currentConfiguration();
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision, current },
    async ({ harness, calls, backendStatus }) => {
      await harness.settle();
      const start = button(harness.tree, "Start engine");
      assert.equal(start.props.disabled, false);
      backendStatus.accepted.acquisition = "another-client";
      start.props.onClick();
      await harness.settle();
      assert.deepEqual(calls, [["engineStart"]]);
      assert.equal(alerts(harness.tree), "");
    });
});

for (const [label, actionName, streaming] of [["Disconnect", "disconnect", false], ["Stop reading", "stopRead", true],
  ["Stop engine", "engineStop", true], ["Stop stimulation", "stimStop", true]]) {
  test(`${label} does not submit edited current values`, async () => {
    const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "another-device" } }],
      processing: [decoder, detector], stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: {} }] });
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true,
      sourceStatus: { connected: true, streaming },
      stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type } },
    async ({ harness, calls }) => {
      await harness.settle();
      const action = button(harness.tree, label);
      assert.equal(action.props.disabled, false);
      action.props.onClick();
      await harness.settle();
      assert.deepEqual(calls, [[actionName]]);
    });
  });
}

test("Trigger trial uses the active paradigm without applying pending plugin edits", async () => {
  const current = currentConfiguration({ stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection,
    parameters: { frequency: 12 } }] });
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true,
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type } },
  async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, false);
    const form = elements(harness.tree).find((item) => item.type === "form");
    form.props.onSubmit({ preventDefault: noop });
    await harness.settle();
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "trialStart");
  });
});

for (const connected of [false, true]) test(`a pending legacy decoder mapping does not block ${connected ? "reading" : "source connection"}`, async () => {
  const message = "Configure the decoder channel mapping before using signal processing.";
  const current = currentConfiguration({ processing: [{ ...decoder, processing_input: null }, detector] }, ["processing"],
    { processing: message });
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true, processingRunning: false,
    sourceStatus: { connected, streaming: false } }, async ({ harness, calls }) => {
    await harness.settle();
    const read = button(harness.tree, "Start reading");
    assert.equal(read.props.disabled, !connected);
    assert.equal(button(harness.tree, "Start processing").props.disabled, true);
    if (connected) {
      assert.match(text(harness.tree), /Configure the decoder channel mapping/);
      await read.props.onClick();
      assert.deepEqual(calls, [["read", { acquisition: complete.revision, system: "system-revision" }]]);
    } else {
      const connect = button(harness.tree, "Connect");
      assert.equal(connect.props.disabled, false);
      await connect.props.onClick();
      assert.deepEqual(calls, [["connect", current.request("acquisition", moduleConfiguration(complete).modules.acquisition)]]);
    }
  });
});


test("a fresh external stimulation configuration is prepared before its node connects", async () => {
  let accepted = { ...sourceOnly, settings: {} };
  const current = currentConfiguration({ stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }] });
  const peer = { active: false, process_alive: false, engine_connected: false, paradigm: null };
  await withScenario({ activation: async () => accepted, acceptedRevision: accepted.revision, current, running: true, stimulationStatus: peer,
    applyCurrentSelection: async (request, status) => {
      accepted = { ...appliedConfiguration(accepted, request, status), settings: { "STIMULATION.auto_spawn": false } };
      return { stimulation: accepted.revisions.stimulation, system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    await button(harness.tree, "Launch stimulation").props.onClick(); await harness.settle();
    assert.equal(calls.filter(([name]) => name === "applyCurrentSelection").length, 1);
    assert.equal(calls.some(([name]) => name === "stimStart" || name === "engineStart"), false);
    assert.match(text(harness.tree), /configuration is prepared.*external stimulation node/);
    assert.ok(calls[0][1].bindings.every((binding) => binding.slot.startsWith("stimulation.")));
  });
});

test("connected external stimulation cannot launch after its current edits are rejected", async () => {
  const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": false } };
  const current = currentConfiguration({ stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }] });
  await withScenario({ activation: async () => accepted, acceptedRevision: accepted.revision, current, running: true,
    stimulationStatus: { active: false, process_alive: false, engine_connected: true, paradigm: null },
    applyCurrentSelection: async () => null }, async ({ harness, calls }) => {
    await harness.settle();
    await button(harness.tree, "Enable stimulation").props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name]) => name), ["applyCurrentSelection"]);
    assert.doesNotMatch(text(harness.tree), /configuration is prepared/);
  });
});


test("Start reading rechecks current source values against the fresh accepted module", async () => {
  let accepted = complete;
  const current = currentConfiguration({ acquisition: [source], processing: [decoder, detector] }, ["processing"]);
  current.hasChanges = (module, value) => module === "acquisition"
    ? !!value && value.revision !== complete.revision : module === "processing";
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true,
    sourceStatus: { connected: true, streaming: false } }, async ({ harness, calls, backendStatus }) => {
    await harness.settle();
    const read = button(harness.tree, "Start reading");
    assert.equal(read.props.disabled, false);
    accepted = { ...complete, revisions: { ...backendStatus.accepted, acquisition: "changed-source" },
      bindings: complete.bindings.map((binding) => binding.slot === "acquisition.source" ? { ...binding, parameters: { host: "changed" } } : binding) };
    backendStatus.accepted.acquisition = "changed-source"; backendStatus.applied.acquisition = "changed-source";
    await read.props.onClick(); await harness.settle();
    assert.deepEqual(calls, []);
    assert.match(alerts(harness.tree), /reconnect/i);
  });
});

test("a processing plan cannot adopt a changed shared system revision", async () => {
  let accepted = complete;
  const current = currentConfiguration({ processing: [{ ...decoder, parameters: { window_duration: 2 } }, detector] });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true, processingRunning: false,
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      status.accepted.system = "concurrent-change"; status.applied.system = "concurrent-change";
      return { processing: "applied-processing", system: "concurrent-change" };
    } }, async ({ harness, calls }) => {
    await harness.settle(); await button(harness.tree, "Start processing").props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name]) => name), ["applyCurrentSelection"]);
    assert.match(alerts(harness.tree), /configuration changed/i);
  });
});

test("reading never starts actuation when its host setting changes", async () => {
  let accepted = { ...complete, settings: { "ACTUATION.auto_spawn": false } };
  const current = currentConfiguration({ processing: [decoder, detector], actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: { destination: "current" } }] });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true,
    sourceStatus: { connected: true, streaming: false }, applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { [request.module]: accepted.revisions[request.module], system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle(); accepted = { ...accepted, settings: { "ACTUATION.auto_spawn": true } };
    await button(harness.tree, "Start reading").props.onClick(); await harness.settle();
    assert.deepEqual(calls.filter(([name]) => name === "applyCurrentSelection"), []);
    assert.equal(calls.at(-1)[0], "read");
    assert.equal(calls.at(-1)[1].actuation, undefined);
  });
});

for (const running of [false, true]) test(`Start processing uses only its current values with engine running=${running}`, async () => {
  let accepted = { ...complete, bindings: complete.bindings.filter((item) => !item.slot.startsWith("acquisition.")) };
  const current = currentConfiguration({
    processing: [{ ...decoder, parameters: { window_duration: 2 } }, detector],
    actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: { destination: "new" } }],
  });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running, processingRunning: false,
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { processing: "applied-processing", system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    const start = button(harness.tree, "Start processing");
    assert.equal(start.props.disabled, false, "Processing can wait for acquisition without requiring a source");
    await start.props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name, module]) => name === "applyCurrentSelection" ? [name, module.module] : [name, module]),
      [["applyCurrentSelection", "processing"], ...(running ? [] : [["engineStart", undefined]]), ["moduleStart", "processing"]]);
    assert.equal(alerts(harness.tree), "");
  });
});

test("Start actuation applies only its own current values while acquisition is reading", async () => {
  let accepted = { ...complete, settings: { "ACTUATION.auto_spawn": true } };
  const current = currentConfiguration({
    actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: { destination: "new" } }],
    processing: [{ ...decoder, parameters: { window_duration: 2 } }, detector],
  });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true,
    sourceStatus: { connected: true, streaming: true },
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { actuation: "applied-actuation", system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Start actuation").props.disabled, false);
    await button(harness.tree, "Start actuation").props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name, module]) => [name, name === "applyCurrentSelection" ? module.module : module]),
      [["applyCurrentSelection", "actuation"], ["moduleStart", "actuation"]]);
    assert.equal(alerts(harness.tree), "");
  });
});

for (const connected of [false, true]) test(`external actuation ${connected ? "starts after connection" : "prepares before connection"} without acquisition`, async () => {
  let accepted = { revision: complete.revision, settings: { "ACTUATION.auto_spawn": false },
    bindings: [{ slot: "actuation.effector", selection: selection("effector"), parameters: {} }] };
  const current = currentConfiguration({ actuation: [{ ...accepted.bindings[0], parameters: { destination: "new" } }] });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true,
    actuationConnected: connected, sourceStatus: { connected: false, streaming: false },
    applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { actuation: "applied-actuation", system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    const start = button(harness.tree, connected ? "Start actuation" : "Prepare external actuation");
    assert.equal(start.props.disabled, false);
    await start.props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name]) => name), connected ? ["applyCurrentSelection", "moduleStart"] : ["applyCurrentSelection", "modulePrepare"]);
    if (!connected) assert.match(text(harness.tree), /Actuation configuration is prepared/);
  });
});

for (const module of ["processing", "actuation"]) test(`Stop ${module} leaves the other modules running and ignores edited values`, async () => {
  const current = currentConfiguration({ processing: [decoder, detector],
    actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: {} }] });
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true,
    processingRunning: true, actuationRunning: true, sourceStatus: { connected: true, streaming: true },
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type } }, async ({ harness, calls }) => {
    await harness.settle();
    await button(harness.tree, `Stop ${module}`).props.onClick(); await harness.settle();
    assert.deepEqual(calls, [["moduleStop", module]]);
  });
});

test("stimulation can apply and start while acquisition is reading", async () => {
  let accepted = complete;
  const current = currentConfiguration({ stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }] });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true,
    sourceStatus: { connected: true, streaming: true }, applyCurrentSelection: async (request, status) => {
      accepted = appliedConfiguration(accepted, request, status);
      return { stimulation: "applied-stimulation", system: "system-revision" };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Launch stimulation").props.disabled, false);
    await button(harness.tree, "Launch stimulation").props.onClick(); await harness.settle();
    assert.deepEqual(calls.map(([name]) => name), ["applyCurrentSelection", "stimStart"]);
  });
});

test("Stop processing cancels the pending start continuation without blocking stimulation", async () => {
  let accepted = complete;
  const finish = deferred();
  const current = currentConfiguration({ processing: [decoder, detector], stimulation: [{ slot: "stimulation.paradigm", selection: paradigm.selection, parameters: { frequency: 12 } }] });
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true, processingRunning: false,
    applyCurrentSelection: async (request, status) => {
      if (request.module === "processing") await finish.promise;
      accepted = appliedConfiguration(accepted, request, status);
      return { [request.module]: status.accepted[request.module], system: status.accepted.system };
    } }, async ({ harness, calls }) => {
    await harness.settle();
    const starting = button(harness.tree, "Start processing").props.onClick();
    await harness.settle();
    assert.equal(button(harness.tree, "Stop processing").props.disabled, false);
    assert.equal(button(harness.tree, "Launch stimulation").props.disabled, false);
    await button(harness.tree, "Launch stimulation").props.onClick();
    await harness.settle();
    await button(harness.tree, "Stop processing").props.onClick();
    finish.resolve(); await starting; await harness.settle();
    assert.ok(calls.some(([name]) => name === "stimStart"));
    assert.ok(calls.some(([name, module]) => name === "moduleStop" && module === "processing"));
    assert.equal(calls.some(([name, module]) => name === "moduleStart" && module === "processing"), false);
  });
});

test("unrelated module revisions preserve edited trial controls", async () => {
  let accepted = complete;
  const controls = { trials: { field: "n_trials", type: "int", default: 1 }, cue: { field: "trial_params", type: "int", default_sequence: "1-3", min: 1, max: 10 } };
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type },
    catalog: async () => [{ ...paradigm, controls }] }, async ({ harness, backendStatus }) => {
    await harness.settle();
    const input = () => elements(harness.tree).find((item) => item.type === "input" && item.props["aria-describedby"] === "trial-sequence-help");
    input().props.onChange({ target: { value: "4, 5, 6" } }); await harness.settle();
    for (const module of ["acquisition", "processing", "actuation"]) {
      backendStatus.accepted[module] = `${module}-changed`;
      backendStatus.applied[module] = `${module}-changed`;
      accepted = { ...accepted, revisions: { ...backendStatus.accepted } };
      harness.updateRuntime({ accepted: { ...backendStatus.accepted }, applied: { ...backendStatus.applied } });
      await harness.settle();
      assert.equal(input().props.value, "4, 5, 6", module);
    }
  });
});

test("Stop reading prevents a pending read validation from restarting the stream", async () => {
  const pending = deferred();
  let delay = false;
  await withScenario({ activation: async () => { if (delay) await pending.promise; return complete; },
    acceptedRevision: complete.revision, running: true, sourceStatus: { connected: true, streaming: false } },
  async ({ harness, calls }) => {
    await harness.settle(); delay = true;
    const reading = button(harness.tree, "Start reading").props.onClick();
    await harness.settle();
    const stop = button(harness.tree, "Stop reading");
    assert.equal(stop.props.disabled, false);
    await stop.props.onClick();
    pending.resolve(); await reading; await harness.settle();
    assert.deepEqual(calls, [["stopRead"]]);
  });
});

for (const [label, command, module] of [["Start processing", "moduleStart", "processing"],
  ["Launch stimulation", "stimStart", "stimulation"], ["Start actuation", "moduleStart", "actuation"]]) {
  test(`${label} prepares its saved configuration on a foundation with no prepared business modules`, async () => {
    const saved = { ...complete, bindings: complete.bindings.map((binding) => binding.slot === "processing.decoder"
      ? { ...binding, processing_input: { schema_version: 2, outputs: [] } } : binding) };
    await withScenario({ activation: async () => saved, acceptedRevision: saved.revision, running: true,
      processingRunning: false, appliedRevisions: { system: "system-revision" } }, async ({ harness, calls }) => {
      await harness.settle();
      const start = button(harness.tree, label);
      assert.equal(start.props.disabled, false);
      await start.props.onClick(); await harness.settle();
      assert.deepEqual(calls, command === "stimStart" ? [[command]] : [[command, module]]);
      assert.equal(button(harness.tree, "Trigger trial").props.disabled, true, "Trial requires the applied stimulation acknowledgement");
    });
  });
}

test("stimulation command completion does not wait for acquisition or processing status", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true },
  async ({ harness, calls, acquisitionController }) => {
    await harness.settle();
    acquisitionController.refresh = async () => assert.fail("Stimulation must not refresh acquisition");
    signalConfigApi.get = async () => assert.fail("Stimulation must not refresh processing");
    await button(harness.tree, "Launch stimulation").props.onClick(); await harness.settle();
    assert.deepEqual(calls, [["stimStart"]]);
    assert.equal(alerts(harness.tree), "");
  });
});

for (const origin of ["engine", "processing"]) test(`Stop engine remains available while ${origin} is starting the foundation`, async () => {
  const finish = deferred();
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision,
    current: currentConfiguration({}, []), running: false }, async ({ harness, calls }) => {
    api.engineStart = async () => { calls.push(["engineStart"]); await finish.promise; };
    await harness.settle();
    const starting = button(harness.tree, origin === "engine" ? "Start engine" : "Start processing").props.onClick();
    await harness.settle();
    assert.deepEqual(calls, [["engineStart"]]);
    const stop = button(harness.tree, "Stop engine");
    assert.equal(stop.props.disabled, false);
    stop.props.onClick(); await harness.settle();
    finish.resolve(); await starting; await harness.settle();
    assert.deepEqual(calls, [["engineStart"], ["engineStop"]]);
    assert.equal(alerts(harness.tree), "");
  });
});

for (const [module, label] of [["processing", "Start processing"], ["stimulation", "Launch stimulation"], ["actuation", "Start actuation"]]) {
  test(`Stop engine cancels ${module} application continuation before any start command`, async () => {
    const finish = deferred();
    let accepted = complete;
    const current = currentConfiguration({ [module]: complete.bindings.filter((item) => item.slot.startsWith(`${module}.`)) });
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current,
      running: module !== "processing", processingRunning: false,
      applyCurrentSelection: async (request, status) => {
        await finish.promise;
        accepted = appliedConfiguration(accepted, request, status);
        return { [module]: status.accepted[module], system: status.accepted.system };
      } }, async ({ harness, calls }) => {
      await harness.settle();
      const starting = button(harness.tree, label).props.onClick(); await harness.settle();
      assert.equal(calls[0][0], "applyCurrentSelection");
      const stop = button(harness.tree, "Stop engine");
      assert.equal(stop.props.disabled, false);
      stop.props.onClick(); await harness.settle();
      finish.resolve(); await starting; await harness.settle();
      assert.deepEqual(calls.map(([name]) => name), ["applyCurrentSelection", "engineStop"]);
    });
  });
}

for (const [module, label, command] of [["acquisition", "Disconnect", "disconnect"], ["processing", "Stop processing", "moduleStop"],
  ["stimulation", "Stop stimulation", "stimStop"], ["actuation", "Stop actuation", "moduleStop"]]) {
  test(`${label} recovers only its module even when the foundation and worker are stopped`, async () => {
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision }, async ({ harness, calls, backendStatus }) => {
      backendStatus.recovery = { [module]: "The module needs recovery" };
      harness.updateRuntimeState({ scopes: { [module]: { blocked: true, recoveryRequired: true } } });
      harness.updateRuntime({ recovery: { ...backendStatus.recovery } }); await harness.settle();
      const stop = button(harness.tree, label);
      assert.equal(stop.props.disabled, false);
      stop.props.onClick(); await harness.settle();
      assert.deepEqual(calls, command === "moduleStop" ? [[command, module]] : [[command]]);
      for (const [other, otherLabel] of [["acquisition", "Disconnect"], ["processing", "Stop processing"],
        ["stimulation", "Stop stimulation"], ["actuation", "Stop actuation"]]) {
        if (other !== module) assert.equal(button(harness.tree, otherLabel).props.disabled, true, other);
      }
    });
  });
}

test("Stop engine is available while acquisition is preparing an automatic startup", async () => {
  await withScenario({}, async ({ harness, calls, acquisitionController }) => {
    let cancelled = 0;
    acquisitionController.pending = true;
    acquisitionController.connect.pending = true;
    acquisitionController.cancelPending = () => { cancelled++; };
    harness.render(); await harness.settle();
    const stop = button(harness.tree, "Stop engine");
    assert.equal(stop.props.disabled, false);
    stop.props.onClick(); await harness.settle();
    assert.deepEqual(calls, [["engineStop"]]);
    assert.equal(cancelled, 1);
  });
});

for (const module of ["acquisition", "processing", "stimulation", "actuation", "system"]) {
  test(`Engine Start ${module === "system" ? "waits for" : "preserves"} ${module} recovery`, async () => {
    await withScenario({}, async ({ harness, calls, backendStatus }) => {
      backendStatus.recovery = { [module]: "Recovery required" };
      harness.updateRuntime({ recovery: backendStatus.recovery });
      harness.updateRuntimeState({ scopes: { system: { blocked: true, recoveryRequired: true } } });
      await harness.settle();
      const start = button(harness.tree, "Start engine");
      assert.equal(start.props.disabled, module === "system");
      await start.props.onClick(); await harness.settle();
      assert.deepEqual(calls, module === "system" ? [] : [["engineStart"]]);
    });
  });
}

for (const [module, setting, prepareLabel, startLabel] of [["stimulation", "STIMULATION.auto_spawn", "Prepare external stimulation", "Enable stimulation"],
  ["actuation", "ACTUATION.auto_spawn", "Prepare external actuation", "Start actuation"]]) {
  test(`external ${module} recovery prepares its retained listener without applying edited values`, async () => {
    const accepted = { ...complete, settings: { [setting]: false } };
    const current = currentConfiguration({ [module]: complete.bindings.filter((item) => item.slot.startsWith(`${module}.`)) });
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true },
      async ({ harness, calls, backendStatus }) => {
        backendStatus.recovery = { [module]: "External state unknown" };
        harness.updateRuntime({ recovery: backendStatus.recovery });
        harness.updateRuntimeState({ scopes: { [module]: { blocked: true, recoveryRequired: true } } });
        await harness.settle();
        const prepare = button(harness.tree, prepareLabel);
        assert.equal(prepare.props.disabled, false);
        prepare.props.onClick(); await harness.settle();
        assert.deepEqual(calls, [["modulePrepare", module]]);
      });
  });
  test(`connected external ${module} cannot start until Stop confirms recovery`, async () => {
    const accepted = { ...complete, settings: { [setting]: false } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
      actuationConnected: true, stimulationStatus: { active: false, process_alive: false, engine_connected: true, paradigm: null } },
      async ({ harness, backendStatus }) => {
        backendStatus.recovery = { [module]: "External state unknown" };
        harness.updateRuntime({ recovery: backendStatus.recovery });
        harness.updateRuntimeState({ scopes: { [module]: { blocked: true, recoveryRequired: true } } });
        await harness.settle();
        assert.equal(button(harness.tree, startLabel).props.disabled, true);
        assert.equal(button(harness.tree, module === "stimulation" ? "Stop stimulation" : "Stop actuation").props.disabled, false);
      });
  });
}
