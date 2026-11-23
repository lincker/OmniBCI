/** Real load effects distinguish a first-use configuration from failed requests. */
import assert from "node:assert/strict";
import { moduleConfiguration, revisionMap, runtimeController } from "./module-fixtures.mjs";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { useCurrentConfiguration } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { useRuntimeControls } = await server.ssrLoadModule("/src/hooks/useRuntimeControls.ts");
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { ModuleRuntime } = await server.ssrLoadModule("/src/ModuleRuntime.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { signalConfigApi } = await server.ssrLoadModule("/src/api/signal-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const { currentFromBinding } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const noop = () => {};
const selection = (id) => ({ package_id: `example.${id}`, contribution_id: "main", version: "1.0.0" });
const source = { slot: "acquisition.source", kind: "acquisition", selection: selection("source"), parameters: {} };
const decoder = { slot: "processing.decoder", kind: "decoder", selection: selection("decoder"), parameters: {} };
const detector = { slot: "processing.epoch_detector", kind: "epoch_detector", selection: selection("detector"), parameters: {} };
const paradigm = { kind: "paradigm", selection: selection("visual"), label: "Visual trials", available: true, controls: {} };
const paradigmId = `${paradigm.selection.package_id}/${paradigm.selection.contribution_id}@${paradigm.selection.version}`;
const sourceOnly = { revision: "source-only", bindings: [source] };
const complete = { revision: "complete", bindings: [source, decoder, detector,
  { slot: "processing.filter.first", kind: "filter", selection: selection("filter"), parameters: {} },
  { slot: "stimulation.paradigm", kind: "paradigm", selection: paradigm.selection, parameters: {} },
  { slot: "stimulation.trigger", kind: "trigger", selection: selection("trigger"), parameters: {} },
  { slot: "actuation.effector", kind: "effector", selection: selection("effector"), parameters: {} }] };

function moduleRuntime(tree, action) {
  return elements(tree).find((element) => element.props.className === "module-runtime"
    && elements(element).some((child) => child.type === "button" && text(child) === action));
}

function expandModules(tree) {
  if (Array.isArray(tree)) return tree.map(expandModules);
  if (!React.isValidElement(tree)) return tree;
  if (tree.type === ModuleRuntime) return expandModules(ModuleRuntime(tree.props));
  return React.cloneElement(tree, {}, expandModules(tree.props.children));
}
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
  let cursor = 0, dirty = true, tree, currentController;
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
      currentController = current;
      const runtime = runtimeController(props.runtime);
      const runtimeControls = useRuntimeControls(runtime, current, props.acquisition, props.engine);
      tree = expandModules(RunConsole({ ...props, current, runtime, runtimeControls }));
    } finally { internal.current = previous; }
    const pending = effects; effects = [];
    pending.forEach((effect) => effect());
  }
  return {
    get tree() { return tree; }, get current() { return currentController; }, render,
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
  current, processingRunning = running, actuationRunning = false, actuationConnected = false,
  sourceStatus = { connected: false, streaming: false },
  stimulationStatus = { active: false, process_alive: false, engine_connected: false, paradigm: null } } = {}, action) {
  const original = { api: { ...api }, plugin: { ...pluginConfigApi }, signal: { ...signalConfigApi },
    runtime: { ...runtimeConfigApi }, document: globalThis.document };
  const calls = [];
  const serverStatus = { accepted: revisionMap(acceptedRevision), applied: appliedRevisions ?? (running ? revisionMap(acceptedRevision) : null),
    running, recovery: {}, generation: running ? "generation-1" : null, operations: {} };
  globalThis.document = { hidden: false };
  api.stimulationStatus = async () => stimulationStatus;
  signalConfigApi.get = async () => ({ readiness: { ready: true }, active_input: null });
  pluginConfigApi.modules = async () => moduleConfiguration(await activation() ?? {});
  pluginConfigApi.module = async (module) => (await pluginConfigApi.modules()).modules[module];
  pluginConfigApi.profiles = profiles;
  pluginConfigApi.catalog = catalog;
  runtimeConfigApi.status = async () => structuredClone(serverStatus);
  for (const name of ["engineStart", "engineStop", "acqConnect", "acqDisconnect", "acqStreamStart", "acqStreamStop", "stimStart", "stimStop", "trialStart", "moduleStart", "moduleStop", "modulePrepare"]) {
    api[name] = async (...args) => {
      calls.push([name, ...args]);
      if (name !== "moduleStart" && name !== "stimStart") return;
      const module = name === "stimStart" ? "stimulation" : args[0];
      const body = args.at(-1);
      const external = body.configuration.settings[`${module.toUpperCase()}.auto_spawn`] === false;
      const connected = module === "stimulation" ? stimulationStatus.engine_connected : actuationConnected;
      return { operation_id: body.operation_id, module, status: "succeeded", target_revisions: { [module]: acceptedRevision },
        result: { configuration_applied: true, action: external && !connected ? "prepared" : "started" } };
    };
  }
  const props = {
    engine: { data: { running, acquisition_alive: running, processor_alive: processingRunning, actuation_alive: actuationRunning, actuation_connected: actuationConnected }, error: null, refresh: async () => {} },
    acquisition: { data: { connection_id: sourceStatus.connected ? "source-connection" : null, ...sourceStatus }, error: null, pending: false, refresh: async () => { props.runtime.status = structuredClone(serverStatus); },
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name,
        { pending: false, error: null, run: async (...args) => { calls.push([name, ...args]); } }])) },
    runtime: { status: structuredClone(serverStatus), blocked: false, inProgress: false, refresh: async () => { props.runtime.status = structuredClone(serverStatus); },
      execute: async (module, configuration, send, onApplied) => {
        const result = await send({ operation_id: crypto.randomUUID(), configuration });
        if (result?.result?.configuration_applied) onApplied?.(result);
        return result;
      } },
    current,
    feed: { generation: null, trials: [], acks: [] }, onBusyChange: noop,
    onEditSetup: () => calls.push(["editSetup"]), onResults: noop,
  };
  api.engineStatus = async () => ({ ...props.engine.data });
  const harness = componentHarness(props);
  try { harness.render(); await action({ harness, calls, serverStatus, acquisitionController: props.acquisition }); }
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
    assert.match(text(region(harness.tree, "Current parameters")), /Loading/);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    pending.resolve(null); await harness.settle();
    assert.match(text(harness.tree), /No data source configured/);
    assert.doesNotMatch(text(region(harness.tree, "Current parameters")), /Loading|Unavailable/);
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
    assert.match(text(region(harness.tree, "Current parameters")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Current parameters")), /Loading|Unavailable/);
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
    const summary = text(region(harness.tree, "Current parameters"));
    assert.match(summary, /Current parameters/);
    assert.match(summary, /example\.source\/main/);
    assert.match(summary, /Plugin 1\.0\.0/);
    assert.doesNotMatch(summary, /Profile revision|revision 0/);
  });
});

test("a source loaded from a profile identifies the current plugin without implying a running profile", async () => {
  const activation = { ...sourceOnly, bindings: [{ ...source, profile_id: "saved-source", profile_revision: 3 }] };
  await withScenario({ activation: async () => activation, acceptedRevision: activation.revision,
    profiles: async () => [{ profile_id: "saved-source", name: "Lab source", revision: 4 }] }, async ({ harness }) => {
    await harness.settle();
    const summary = text(region(harness.tree, "Current parameters"));
    assert.doesNotMatch(summary, /Lab source/);
    assert.match(summary, /example\.source\/main/);
    assert.match(summary, /Plugin 1\.0\.0/);
    assert.doesNotMatch(summary, /Profile revision/);
    assert.doesNotMatch(summary, /not saved as a profile|Profile revision 4/);
  });
});

test("a complete configuration preserves selected modules and processing readiness", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true }, async ({ harness, calls }) => {
    await harness.settle();
    const summary = text(region(harness.tree, "Current parameters"));
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
    assert.match(text(region(harness.tree, "Current parameters")), /Unavailable/);
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
    assert.match(text(region(harness.tree, "Current parameters")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Current parameters")), /Unavailable|Loading/);
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
    assert.match(text(region(harness.tree, "Current parameters")), /example.source/);
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
    assert.match(text(region(harness.tree, "Current parameters")), /Loading/);
    assert.equal(button(harness.tree, "Connect").props.disabled, true);
    current.resolve(sourceOnly); await harness.settle();
    assert.match(text(region(harness.tree, "Current parameters")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Current parameters")), /Loading|Unavailable/);
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
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigmId },
    catalog: async () => [invalidParadigm] }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(alerts(harness.tree), /Unsupported trial control/);
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, true);
    button(harness.tree, "Retry loading configuration").props.onClick(); await harness.settle();
    assert.doesNotMatch(text(region(harness.tree, "Current parameters")), /Loading/);
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
    request: (module) => {
      const accepted = moduleConfiguration(complete).modules[module];
      if (errors[module]) throw new Error(errors[module]);
      return { settings: accepted?.settings ?? {}, expected_revision: accepted?.revision ?? null,
        bindings: structuredClone(changes[module] ?? accepted?.bindings ?? [])
          .map(({ slot, selection, parameters, processing_input }) => ({ slot, selection, parameters,
            ...(processing_input === undefined ? {} : { processing_input }) })),
      };
    },
  };
}


test("a first unsaved current source can connect without an accepted activation or a saved profile", async () => {
  const current = currentConfiguration({ acquisition: [{ ...source, parameters: { host: "current-device" } }] });
  await withScenario({ current }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    await button(harness.tree, "Connect").props.onClick();
    assert.deepEqual(calls, [["connect", current.request("acquisition")]]);
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
    assert.deepEqual(calls, [["connect", current.request("acquisition")]]);
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

test("a changed acquisition revision does not prevent foundation startup", async () => {
  const current = currentConfiguration();
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision, current },
    async ({ harness, calls, serverStatus }) => {
      await harness.settle();
      const start = button(harness.tree, "Start engine");
      assert.equal(start.props.disabled, false);
      serverStatus.accepted.acquisition = "another-client";
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
      stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigmId } },
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
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigmId } },
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
      assert.deepEqual(calls, [["read", { expected_revision: complete.revision, generation: "generation-1", connection_id: "source-connection" }]]);
    } else {
      const connect = button(harness.tree, "Connect");
      assert.equal(connect.props.disabled, false);
      await connect.props.onClick();
      assert.deepEqual(calls, [["connect", current.request("acquisition")]]);
    }
  });
});


test("Start reading sends the observed connection identity for atomic server validation", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
    sourceStatus: { connected: true, streaming: false } }, async ({ harness, calls, serverStatus }) => {
    await harness.settle();
    serverStatus.accepted.acquisition = "changed-source";
    pluginConfigApi.module = async () => assert.fail("Reading must not perform a check-then-act configuration fetch");
    await button(harness.tree, "Start reading").props.onClick();
    assert.deepEqual(calls, [["read", { expected_revision: complete.revision, generation: "generation-1", connection_id: "source-connection" }]]);
  });
});

for (const module of ["processing", "actuation"]) test(`Stop ${module} leaves the other modules running and ignores edited values`, async () => {
  const current = currentConfiguration({ processing: [decoder, detector],
    actuation: [{ slot: "actuation.effector", selection: selection("effector"), parameters: {} }] });
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, current, running: true,
    processingRunning: true, actuationRunning: true, sourceStatus: { connected: true, streaming: true },
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigmId } }, async ({ harness, calls }) => {
    await harness.settle();
    await button(harness.tree, `Stop ${module}`).props.onClick(); await harness.settle();
    assert.deepEqual(calls, [["moduleStop", module]]);
  });
});

test("unrelated module revisions preserve edited trial controls", async () => {
  let accepted = complete;
  const controls = { trials: { field: "n_trials", type: "int", default: 1 }, cue: { field: "trial_params", payload_key: "event_id", type: "int", default_sequence: "1-3", min: 1, max: 10 } };
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigmId },
    catalog: async () => [{ ...paradigm, controls }] }, async ({ harness, serverStatus }) => {
    await harness.settle();
    const input = () => elements(harness.tree).find((item) => item.type === "input" && item.props["aria-describedby"] === "trial-sequence-help");
    input().props.onChange({ target: { value: "4, 5, 6" } }); await harness.settle();
    for (const module of ["acquisition", "processing", "actuation"]) {
      serverStatus.accepted[module] = `${module}-changed`;
      serverStatus.applied[module] = `${module}-changed`;
      accepted = { ...accepted, revisions: { ...serverStatus.accepted } };
      harness.updateRuntime({ accepted: { ...serverStatus.accepted }, applied: { ...serverStatus.applied } });
      await harness.settle();
      assert.equal(input().props.value, "4, 5, 6", module);
    }
  });
});

test("Stop reading remains available while the read request is pending", async () => {
  const pending = deferred();
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
    sourceStatus: { connected: true, streaming: false } }, async ({ harness, calls, acquisitionController }) => {
    await harness.settle();
    acquisitionController.read.run = async (body) => { calls.push(["read", body]); await pending.promise; };
    const reading = button(harness.tree, "Start reading").props.onClick(); await harness.settle();
    const stop = button(harness.tree, "Stop reading");
    assert.equal(stop.props.disabled, false);
    await stop.props.onClick();
    pending.resolve(); await reading; await harness.settle();
    assert.deepEqual(calls.map(([name]) => name), ["read", "stopRead"]);
  });
});

for (const [label, command, module] of [["Start processing", "moduleStart", "processing"],
  ["Launch stimulation", "stimStart", "stimulation"], ["Start actuation", "moduleStart", "actuation"]]) {
  test(`${label} submits its current configuration in one request on an unprepared foundation`, async () => {
    const saved = { ...complete, bindings: complete.bindings.map((binding) => binding.slot === "processing.decoder"
      ? { ...binding, processing_input: { schema_version: 2, outputs: [] } } : binding) };
    await withScenario({ activation: async () => saved, acceptedRevision: saved.revision, running: true,
      processingRunning: false, appliedRevisions: { engine: "engine-revision" } }, async ({ harness, calls }) => {
      await harness.settle();
      const start = button(harness.tree, label);
      assert.equal(start.props.disabled, false);
      await start.props.onClick(); await harness.settle();
      assert.equal(calls.length, 1);
      assert.equal(calls[0][0], command);
      if (command === "moduleStart") assert.equal(calls[0][1], module);
      const body = calls[0].at(-1);
      assert.match(body.operation_id, /^[0-9a-f-]{36}$/);
      assert.equal(body.configuration.expected_revision, saved.revision);
      assert.deepEqual(body.configuration.settings, moduleConfiguration(saved).modules[module].settings);
      assert.ok(body.configuration.bindings.every((binding) => binding.slot.startsWith(`${module}.`)));
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
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "stimStart");
    assert.equal(alerts(harness.tree), "");
  });
});

test("Stop engine remains available while the foundation is starting", async () => {
  const finish = deferred();
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision,
    current: currentConfiguration({}, []), running: false }, async ({ harness, calls }) => {
    api.engineStart = async () => { calls.push(["engineStart"]); await finish.promise; };
    await harness.settle();
    const starting = button(harness.tree, "Start engine").props.onClick();
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

for (const [module, label, command] of [["acquisition", "Disconnect", "disconnect"], ["processing", "Stop processing", "moduleStop"],
  ["stimulation", "Stop stimulation", "stimStop"], ["actuation", "Stop actuation", "moduleStop"]]) {
  test(`${label} recovers only its module even when the foundation and worker are stopped`, async () => {
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision }, async ({ harness, calls, serverStatus }) => {
      serverStatus.recovery = { [module]: "The module needs recovery" };
      harness.updateRuntimeState({ scopes: { [module]: { blocked: true, recoveryRequired: true } } });
      harness.updateRuntime({ recovery: { ...serverStatus.recovery } }); await harness.settle();
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

test("Stop engine is available while the acquisition request is pending", async () => {
  await withScenario({}, async ({ harness, calls, acquisitionController }) => {
    acquisitionController.pending = true;
    acquisitionController.connect.pending = true;
    harness.render(); await harness.settle();
    const stop = button(harness.tree, "Stop engine");
    assert.equal(stop.props.disabled, false);
    stop.props.onClick(); await harness.settle();
    assert.deepEqual(calls, [["engineStop"]]);
  });
});

for (const module of ["acquisition", "processing", "stimulation", "actuation", "engine"]) {
  test(`Engine Start ${module === "engine" ? "waits for" : "preserves"} ${module} recovery`, async () => {
    await withScenario({}, async ({ harness, calls, serverStatus }) => {
      serverStatus.recovery = { [module]: "Recovery required" };
      harness.updateRuntime({ recovery: serverStatus.recovery });
      harness.updateRuntimeState({ scopes: { engine: { blocked: true, recoveryRequired: true } } });
      await harness.settle();
      const start = button(harness.tree, "Start engine");
      assert.equal(start.props.disabled, module === "engine");
      await start.props.onClick(); await harness.settle();
      assert.deepEqual(calls, module === "engine" ? [] : [["engineStart"]]);
    });
  });
}

for (const connected of [false, true]) {
  test(`external stimulation ${connected ? "enables" : "prepares"} a paradigm unavailable on the server platform`, async () => {
    const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": false } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
      catalog: async () => [{ ...paradigm, available: false }],
      stimulationStatus: { active: false, process_alive: false, engine_connected: connected, paradigm: null } },
      async ({ harness, calls }) => {
        await harness.settle();
        const start = button(harness.tree, connected ? "Enable stimulation" : "Prepare external stimulation");
        assert.equal(start.props.disabled, false);
        await start.props.onClick(); await harness.settle();
        assert.equal(calls.length, 1);
        assert.equal(calls[0][0], "stimStart");
        assert.equal(calls[0][1].configuration.settings["STIMULATION.auto_spawn"], false);
      });
  });
}

for (const available of [true, false]) {
  test(`enabled external stimulation can trigger trials with a null local paradigm and server availability=${available}`, async () => {
    const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": false } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
      catalog: async () => [{ ...paradigm, available }],
      stimulationStatus: { active: true, process_alive: false, engine_connected: true, paradigm: null } },
      async ({ harness, calls }) => {
        await harness.settle();
        const trigger = button(harness.tree, "Trigger trial");
        assert.equal(trigger.props.disabled, false);
        elements(harness.tree).find((item) => item.type === "form" && item.props.className.includes("trial-controls")).props.onSubmit({ preventDefault: noop });
        await harness.settle();
        assert.deepEqual(calls, [["trialStart", 1, [{}]]]);
      });
  });
}

for (const missing of ["enabled", "connected", "applied configuration"]) {
  test(`external trials still require ${missing}`, async () => {
    const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": false } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
      catalog: async () => [{ ...paradigm, available: false }],
      appliedRevisions: { ...revisionMap(complete.revision), stimulation: missing === "applied configuration" ? "older" : complete.revision },
      stimulationStatus: { active: missing !== "enabled", process_alive: false, engine_connected: missing !== "connected", paradigm: null } },
      async ({ harness, calls }) => {
        await harness.settle();
        const trigger = button(harness.tree, "Trigger trial");
        assert.equal(trigger.props.disabled, true);
        elements(harness.tree).find((item) => item.type === "form" && item.props.className.includes("trial-controls")).props.onSubmit({ preventDefault: noop });
        await harness.settle();
        assert.deepEqual(calls, []);
      });
  });
}

test("managed stimulation still requires a paradigm available on the server platform", async () => {
  const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": true } };
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
    catalog: async () => [{ ...paradigm, available: false }] }, async ({ harness, calls }) => {
    await harness.settle();
    assert.equal(button(harness.tree, "Launch stimulation").props.disabled, true);
    assert.deepEqual(calls, []);
  });
});

test("switching an external paradigm to managed mode still checks server platform availability", async () => {
  const accepted = { ...complete, settings: { "STIMULATION.auto_spawn": false } };
  await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
    catalog: async () => [{ ...paradigm, available: false }] }, async ({ harness, calls }) => {
    await harness.settle();
    harness.current.setSetting("stimulation", "STIMULATION.auto_spawn", true); await harness.settle();
    assert.equal(button(harness.tree, "Launch stimulation").props.disabled, true);
    assert.deepEqual(calls, []);
  });
});

for (const [module, setting, prepareLabel, startLabel] of [["stimulation", "STIMULATION.auto_spawn", "Prepare external stimulation", "Enable stimulation"],
  ["actuation", "ACTUATION.auto_spawn", "Prepare external actuation", "Start actuation"]]) {
  test(`${module} Start uses current mode and all edited settings in one request`, async () => {
    const prefix = module.toUpperCase();
    const accepted = { ...complete, settings: { [setting]: true, [`${prefix}.server_ip`]: "127.0.0.1",
      [`${prefix}.server_port`]: module === "stimulation" ? 8765 : 8001 } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true },
      async ({ harness, calls }) => {
        await harness.settle();
        harness.current.setSetting(module, setting, false);
        harness.current.setSetting(module, `${prefix}.server_port`, 9023);
        await harness.settle();
        const prepare = button(harness.tree, prepareLabel);
        assert.equal(prepare.props.disabled, false);
        await prepare.props.onClick(); await harness.settle();
        assert.equal(calls.length, 1);
        assert.equal(calls[0][0], module === "stimulation" ? "stimStart" : "moduleStart");
        const body = calls[0].at(-1).configuration;
        assert.deepEqual(body.settings, { [setting]: false, [`${prefix}.server_ip`]: "127.0.0.1", [`${prefix}.server_port`]: 9023 });
        assert.ok(body.bindings.length > 0);
        assert.equal(body.expected_revision, complete.revision);
      });
  });
  test(`${module} refuses current edits while an external node is connected, including switching to managed mode`, async () => {
    const accepted = { ...complete, settings: { [setting]: false } };
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, running: true,
      actuationConnected: true, stimulationStatus: { active: false, process_alive: false, engine_connected: true, paradigm: null } },
      async ({ harness, calls }) => {
        await harness.settle();
        harness.current.setSetting(module, setting, true); await harness.settle();
        const start = button(harness.tree, module === "stimulation" ? "Launch stimulation" : "Start actuation");
        assert.equal(start.props.disabled, true);
        assert.match(text(harness.tree), /Disconnect the external node before changing its configuration/);
        assert.equal(button(harness.tree, module === "stimulation" ? "Stop stimulation" : "Stop actuation").props.disabled, false);
        assert.deepEqual(calls, []);
      });
  });
  test(`${module} invalid settings block its Start while Stop and the other module remain available`, async () => {
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
      actuationConnected: true, stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: "visual" } },
      async ({ harness }) => {
        await harness.settle();
        harness.current.setSetting(module, `${module.toUpperCase()}.server_port`, 0); await harness.settle();
        assert.equal(button(harness.tree, module === "stimulation" ? "Launch stimulation" : "Start actuation").props.disabled, true);
        assert.match(text(harness.tree), /port.*1.*65535/i);
        assert.equal(button(harness.tree, module === "stimulation" ? "Stop stimulation" : "Stop actuation").props.disabled, false);
        assert.equal(button(harness.tree, "Connect").props.disabled, false);
      });
  });
  test(`external ${module} recovery prepares its retained listener without applying edited values`, async () => {
    const accepted = { ...complete, settings: { [setting]: false } };
    const current = currentConfiguration({ [module]: complete.bindings.filter((item) => item.slot.startsWith(`${module}.`)) });
    await withScenario({ activation: async () => accepted, acceptedRevision: complete.revision, current, running: true },
      async ({ harness, calls, serverStatus }) => {
        serverStatus.recovery = { [module]: "External state unknown" };
        harness.updateRuntime({ recovery: serverStatus.recovery });
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
      async ({ harness, serverStatus }) => {
        serverStatus.recovery = { [module]: "External state unknown" };
        harness.updateRuntime({ recovery: serverStatus.recovery });
        harness.updateRuntimeState({ scopes: { [module]: { blocked: true, recoveryRequired: true } } });
        await harness.settle();
        assert.equal(button(harness.tree, startLabel).props.disabled, true);
        assert.equal(button(harness.tree, module === "stimulation" ? "Stop stimulation" : "Stop actuation").props.disabled, false);
      });
  });
}

for (const [name, startLabel] of [["acquisition", "Connect"], ["processing", "Start processing"], ["stimulation", "Launch stimulation"], ["actuation", "Start actuation"]]) {
  test(`${name} with a retained applied revision is not reported running after it stops`, async () => {
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
      processingRunning: false }, async ({ harness }) => {
      await harness.settle();
      const state = moduleRuntime(harness.tree, startLabel);
      assert.match(text(state), name === "acquisition" ? /Not connected/ : /Not running/);
      assert.match(text(state), /next connection or start uses the current parameters/);
      assert.doesNotMatch(text(state), /module is using the current parameters/);
    });
  });
}

test("editing a running decoder changes the current summary and reports pending parameters without applying them", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(moduleRuntime(harness.tree, "Start processing")), /module is using the current parameters/);
    harness.current.set("processing.decoder", { ...harness.current.get("processing.decoder"),
      selection: selection("replacement-decoder"), parameters: { duration: 2 } });
    await harness.settle();
    const state = moduleRuntime(harness.tree, "Start processing");
    assert.match(text(region(harness.tree, "Current parameters")), /example\.replacement-decoder/);
    assert.match(text(state), /Current parameters differ from those in use/);
    assert.equal(button(state, "Start processing").props.disabled, true);
    assert.equal(button(state, "Stop processing").props.disabled, false);
    assert.deepEqual(calls, []);
  });
});

test("a loaded decoder appears in the current summary before the server has accepted Processing", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision }, async ({ harness, calls }) => {
    await harness.settle();
    harness.current.set("processing.decoder", { selection: selection("loaded-decoder"), plugin: "loaded-decoder", profile: null,
      name: "", parameters: { duration: 2 }, processingInput: { outputs: [] } });
    await harness.settle();
    assert.match(text(region(harness.tree, "Current parameters")), /example\.loaded-decoder/);
    assert.match(text(moduleRuntime(harness.tree, "Start processing")), /requires an epoch detector/);
    assert.deepEqual(calls, []);
  });
});

for (const uncertainty of [{ uncertain: true }, { recoveryRequired: true }, { inProgress: true }]) {
  test(`Processing does not claim current parameters are in use during ${Object.keys(uncertainty)[0]}`, async () => {
    await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true }, async ({ harness }) => {
      await harness.settle();
      harness.updateRuntimeState({ scopes: { processing: uncertainty } });
      await harness.settle();
      const state = moduleRuntime(harness.tree, "Start processing");
      assert.match(text(state), /running parameters cannot be confirmed/);
      assert.doesNotMatch(text(state), /module is using the current parameters/);
    });
  });
}

test("a different applied revision never presents accepted parameters as running values", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
    appliedRevisions: { ...revisionMap(complete.revision), processing: "other-running-configuration" } }, async ({ harness }) => {
    await harness.settle();
    assert.match(text(moduleRuntime(harness.tree, "Start processing")), /running parameters cannot be confirmed/);
    assert.doesNotMatch(text(moduleRuntime(harness.tree, "Start processing")), /module is using the current parameters/);
  });
});

for (const [name, prepareLabel, stopLabel] of [["stimulation", "Prepare external stimulation", "Stop stimulation"],
  ["actuation", "Prepare external actuation", "Stop actuation"]]) {
  test(`external ${name} preparation reports no running module and Stop clears the preparation notice`, async () => {
    const external = { ...complete, settings: { [`${name.toUpperCase()}.auto_spawn`]: false } };
    await withScenario({ activation: async () => external, acceptedRevision: external.revision, running: true }, async ({ harness, calls }) => {
      await harness.settle();
      await button(harness.tree, prepareLabel).props.onClick();
      await harness.settle();
      const state = moduleRuntime(harness.tree, prepareLabel);
      assert.match(text(state), /Not running/);
      assert.match(text(state), /configuration was prepared/);
      assert.doesNotMatch(text(state), /module is using the current parameters/);
      assert.equal(button(state, stopLabel).props.disabled, false);
      await button(state, stopLabel).props.onClick();
      await harness.settle();
      assert.doesNotMatch(text(moduleRuntime(harness.tree, prepareLabel)), /configuration was prepared/);
      assert.ok(calls.some(([action]) => action === (name === "stimulation" ? "stimStop" : "moduleStop")));
    });
  });
}

test("an invalid staged filter cannot block Processing until it is added to the chain", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true,
    processingRunning: false }, async ({ harness }) => {
    await harness.settle();
    harness.current.set("processing.decoder", { ...harness.current.get("processing.decoder"),
      requiresProcessingInputReview: false, processingInput: { schema_version: 2, outputs: [] } });
    const invalid = { selection: selection("filter"), plugin: "filter", profile: null, name: "", processingInput: null,
      parameters: { cutoff: -1 }, schema: { type: "object", properties: { cutoff: { type: "number", minimum: 0 } } } };
    harness.current.set("processing.filters", invalid);
    await harness.settle();
    assert.equal(button(harness.tree, "Start processing").props.disabled, false);
    harness.current.set("processing.filter.new", invalid);
    harness.current.setFilterOrder([...harness.current.filterOrder, "processing.filter.new"]);
    await harness.settle();
    assert.equal(button(harness.tree, "Start processing").props.disabled, true);
    assert.match(text(moduleRuntime(harness.tree, "Start processing")), /cutoff/);
  });
});

test("disagreeing Engine and runtime polls cannot report confirmed running parameters", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true }, async ({ harness }) => {
    await harness.settle();
    harness.updateRuntime({ running: false });
    await harness.settle();
    const state = moduleRuntime(harness.tree, "Start processing");
    assert.match(text(state), /Runtime status is unavailable/);
    assert.match(text(state), /running parameters cannot be confirmed/);
    assert.doesNotMatch(text(state), /module is using the current parameters/);
  });
});

const tabs = (tree) => elements(tree).filter((item) => item.props.role === "tab");
const tab = (tree, label) => {
  const result = tabs(tree).find((item) => text(item).replace(/^\d+/, "").startsWith(label));
  assert.ok(result, `Missing step ${label}`);
  return result;
};
const panel = (tree, id) => elements(tree).find((item) => item.props.role === "tabpanel" && item.props.id === `run-panel-${id}`);

test("the pipeline points at the engine first and keeps every step panel mounted", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision }, async ({ harness }) => {
    await harness.settle();
    assert.deepEqual(tabs(harness.tree).map((item) => item.props.id),
      ["run-step-acquisition", "run-step-processing", "run-step-stimulation", "run-step-actuation"]);
    assert.ok(!tabs(harness.tree).some((item) => item.props["data-next"]), "No module is next while the engine is stopped");
    assert.match(text(region(harness.tree, "Engine controls")), /Next step/);
    assert.equal(tab(harness.tree, "Acquisition").props["aria-selected"], true);
    for (const id of ["acquisition", "processing", "stimulation", "actuation"]) {
      assert.ok(panel(harness.tree, id), id);
      assert.equal(panel(harness.tree, id).props.hidden, id !== "acquisition", id);
    }
    assert.match(text(tab(harness.tree, "Actuation")), /Optional/);
  });
});

test("the next pipeline step follows module state until the user pins a step", async () => {
  await withScenario({ activation: async () => complete, acceptedRevision: complete.revision, running: true, processingRunning: false,
    sourceStatus: { connected: true, streaming: true } }, async ({ harness }) => {
    await harness.settle();
    assert.equal(tab(harness.tree, "Acquisition").props["data-phase"], "done");
    assert.equal(tab(harness.tree, "Signal processing").props["data-next"], true);
    assert.equal(panel(harness.tree, "processing").props.hidden, false);
    assert.doesNotMatch(text(region(harness.tree, "Engine controls")), /Next step/);
    tab(harness.tree, "Stimulation").props.onClick(); await harness.settle();
    assert.equal(tab(harness.tree, "Stimulation").props["aria-selected"], true);
    assert.equal(panel(harness.tree, "stimulation").props.hidden, false);
    assert.equal(panel(harness.tree, "processing").props.hidden, true);
    assert.equal(tab(harness.tree, "Signal processing").props["data-next"], true, "Pinning a step does not change what is next");
  });
});

test("an unconfigured step is marked instead of being suggested as next", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision, running: true,
    sourceStatus: { connected: true, streaming: true } }, async ({ harness }) => {
    await harness.settle();
    for (const label of ["Signal processing", "Stimulation", "Actuation"]) {
      assert.equal(tab(harness.tree, label).props["data-phase"], "unconfigured", label);
      assert.match(text(tab(harness.tree, label)), /Not configured/);
    }
    assert.ok(!tabs(harness.tree).some((item) => item.props["data-next"]));
    assert.match(text(harness.tree), /Pipeline is running/);
  });
});
