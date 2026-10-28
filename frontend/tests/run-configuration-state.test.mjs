/** Real load effects distinguish a first-use configuration from failed requests. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { signalConfigApi } = await server.ssrLoadModule("/src/api/signal-config.ts");
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
    try { tree = RunConsole(props); } finally { internal.current = previous; }
    const pending = effects; effects = [];
    pending.forEach((effect) => effect());
  }
  return {
    get tree() { return tree; }, render,
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
  profiles = async () => [], catalog = async () => [paradigm], running = false,
  sourceStatus = { connected: false, streaming: false },
  stimulationStatus = { active: false, process_alive: false, engine_connected: false, paradigm: null } } = {}, action) {
  const original = { api: { ...api }, plugin: { ...pluginConfigApi }, signal: { ...signalConfigApi }, document: globalThis.document };
  const calls = [];
  globalThis.document = { hidden: false };
  api.stimulationStatus = async () => stimulationStatus;
  signalConfigApi.get = async () => ({ readiness: { ready: true }, active_input: null });
  pluginConfigApi.activation = activation;
  pluginConfigApi.profiles = profiles;
  pluginConfigApi.catalog = catalog;
  for (const name of ["engineStart", "engineStop", "acqConnect", "acqDisconnect", "acqStreamStart", "acqStreamStop", "stimStart", "stimStop", "trialStart"]) {
    api[name] = async (...args) => { calls.push([name, ...args]); };
  }
  const props = {
    engine: { data: { running, acquisition_alive: running, processor_alive: running }, error: null, refresh: async () => {} },
    acquisition: { data: sourceStatus, error: null, pending: false, refresh: async () => {},
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name,
        { pending: false, error: null, run: async (...args) => { calls.push([name, ...args]); } }])) },
    runtime: { status: { accepted_revision: acceptedRevision, applied_revision: running ? acceptedRevision : null,
      running, recovery_required: false, generation: running ? "generation-1" : null },
      blocked: false, inProgress: false, refresh: async () => {} },
    feed: { generation: null, trials: [], acks: [] }, onBusyChange: noop,
    onEditSetup: () => calls.push(["editSetup"]), onResults: noop,
  };
  const harness = componentHarness(props);
  try { harness.render(); await action({ harness, calls }); }
  finally {
    harness.dispose();
    Object.assign(api, original.api); Object.assign(pluginConfigApi, original.plugin); Object.assign(signalConfigApi, original.signal);
    if (original.document === undefined) delete globalThis.document; else globalThis.document = original.document;
  }
}

test("a successful empty activation is first-use guidance, not a failed request", async () => {
  const pending = deferred();
  await withScenario({ activation: () => pending.promise }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /Loading/);
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    pending.resolve(null); await harness.settle();
    assert.match(text(harness.tree), /No data source configured/);
    assert.doesNotMatch(text(region(harness.tree, "Startup configuration")), /Loading|Unavailable/);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
    assert.equal(alerts(harness.tree), "");
    for (const label of ["Start engine", "Connect", "Start reading"]) assert.equal(button(harness.tree, label).props.disabled, true, label);
    const configure = button(harness.tree, "Configure data source");
    assert.equal(!!configure.props.disabled, false);
    configure.props.onClick();
    assert.deepEqual(calls, [["editSetup"]], "Guidance only navigates; it must not send runtime commands");
  });
});

test("a source-only configuration renders without processing or trial setup requirements", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Startup configuration")), /Loading|Unavailable/);
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
    const summary = text(region(harness.tree, "Startup configuration"));
    assert.match(summary, /Current configuration \(not saved as a profile\)/);
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
    const summary = text(region(harness.tree, "Startup configuration"));
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
    const summary = text(region(harness.tree, "Startup configuration"));
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
    assert.match(text(region(harness.tree, "Startup configuration")), /Unavailable/);
    assert.equal(preparation(harness.tree), "Signal preparationUnavailable");
    assert.doesNotMatch(text(harness.tree), /No data source configured|Loading/);
    for (const label of ["Start engine", "Connect", "Start reading"]) assert.equal(button(harness.tree, label).props.disabled, true, label);
    assert.deepEqual(calls, []);
  });
});

test("a failed stimulation catalog does not hide a valid source or imply acquisition is unavailable", async () => {
  await withScenario({ activation: async () => sourceOnly, acceptedRevision: sourceOnly.revision,
    catalog: async () => { throw new Error("Paradigm catalog offline"); } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Startup configuration")), /Unavailable|Loading/);
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
    assert.match(text(region(harness.tree, "Startup configuration")), /example.source/);
    assert.equal(button(harness.tree, "Connect").props.disabled, false);
    assert.equal(preparation(harness.tree), "Signal preparationSignal processing not enabled");
  });
});

test("changed configuration waits for the current response and ignores an older empty response", async () => {
  const old = deferred(), current = deferred();
  let request = 0;
  await withScenario({ activation: () => (++request === 1 ? old : current).promise }, async ({ harness, calls }) => {
    await harness.settle();
    harness.updateRuntime({ accepted_revision: sourceOnly.revision }); await harness.settle();
    old.resolve(null); await harness.settle();
    assert.doesNotMatch(text(harness.tree), /No data source configured/);
    assert.match(text(region(harness.tree, "Startup configuration")), /Loading/);
    assert.equal(button(harness.tree, "Connect").props.disabled, true);
    current.resolve(sourceOnly); await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /example.source/);
    assert.doesNotMatch(text(region(harness.tree, "Startup configuration")), /Loading|Unavailable/);
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
    assert.equal(requests, 2);
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
  await withScenario({ activation: () => ++requests === 1 ? Promise.resolve(missingDetector) : pending.promise,
    acceptedRevision: missingDetector.revision, running: true, sourceStatus: { connected: true, streaming: false },
    catalog: async () => { throw new Error("Trial catalog temporarily unavailable"); } }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(preparation(harness.tree), /Epoch detector missing/);
    assert.equal(button(harness.tree, "Start reading").props.disabled, true);
    button(harness.tree, "Retry loading configuration").props.onClick(); await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /Loading/);
    const read = button(harness.tree, "Start reading");
    assert.equal(read.props.disabled, true, "Loading cannot bypass decoder prerequisites from the accepted configuration");
    await read.props.onClick();
    assert.deepEqual(calls, []);
    assert.equal(button(harness.tree, "Stop engine").props.disabled, false);
    assert.equal(button(harness.tree, "Disconnect").props.disabled, false);
    pending.resolve(missingDetector); await harness.settle();
    assert.equal(button(harness.tree, "Start reading").props.disabled, true);
  });
});

test("retrying invalid trial controls keeps trial submission blocked and stop controls available", async () => {
  const pending = deferred();
  let requests = 0;
  const invalidParadigm = { ...paradigm, controls: { invalid: { field: "unsupported", type: "int" } } };
  await withScenario({ activation: () => ++requests === 1 ? Promise.resolve(complete) : pending.promise,
    acceptedRevision: complete.revision, running: true, sourceStatus: { connected: true, streaming: true },
    stimulationStatus: { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type },
    catalog: async () => [invalidParadigm] }, async ({ harness, calls }) => {
    await harness.settle();
    assert.match(alerts(harness.tree), /Unsupported trial control/);
    assert.equal(button(harness.tree, "Trigger trial").props.disabled, true);
    button(harness.tree, "Retry loading configuration").props.onClick(); await harness.settle();
    assert.match(text(region(harness.tree, "Startup configuration")), /Loading/);
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
