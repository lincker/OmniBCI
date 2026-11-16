import { runtimeController } from "./module-fixtures.mjs";
/** Exercise profile browsing and module actions through component events. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { PluginProfiles } = await server.ssrLoadModule("/src/PluginProfiles.tsx");
const { pluginConfigApi: api, selectionKey } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { api: controlApi } = await server.ssrLoadModule("/src/api/client.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const { useCurrentConfiguration } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { useRuntimeControls } = await server.ssrLoadModule("/src/hooks/useRuntimeControls.ts");
const { ModuleRuntime } = await server.ssrLoadModule("/src/ModuleRuntime.tsx");
const { signalConfigApi } = await server.ssrLoadModule("/src/api/signal-config.ts");

const selection = (packageId, version = "1.0.0") => ({ package_id: packageId, contribution_id: "main", version });
const scanV1 = selection("example.scan45");
const scanV2 = selection("example.scan45", "2.0.0");
const replay = selection("example.replay");
const unsavedPlugin = selection("example.new-source");
const bandpass = selection("example.bandpass");
const notch = selection("example.notch");
const decoder = selection("example.decoder");
const trigger = selection("example.trigger");
const paradigm = selection("example.paradigm");
const effector = selection("example.effector");
const channelRequirements = { description: "Select the configured motor channels.", signal_type: "eeg", min_count: 2,
  required_names: ["C3"], default_names: ["C4", "C3"] };
const channelIds = { C3: "sample-0", C4: "sample-1", P3: "sample-2", P4: "sample-3" };
const decoderInput = (names) => ({ schema_version: 2, outputs: names.map((name) => ({ name, source_channel_id: channelIds[name] })) });
const emptyFilter = selection("example.new-filter");
const clone = (value) => structuredClone(value);
const schema = {
  type: "object", required: ["gain", "calibration"], properties: {
    gain: { type: "integer", minimum: 1, default: 1 },
    calibration: { type: "array", default: [1, 2], items: { type: "integer" } },
  },
};
const definition = (kind, choice, label) => ({ kind, selection: choice, label, configuration_version: 1, available: true, schema: clone(schema), controls: {} });
const profile = (id, kind, choice, name, gain) => ({ profile_id: id, kind, selection: choice, name, configuration_version: 1, parameters: { gain, calibration: [3, 4] }, revision: 2 });

function fixture() {
  const profiles = [
    profile("lab-b", "acquisition", scanV1, "Lab B", 3),
    profile("lab-a", "acquisition", scanV1, "Lab A", 2),
    profile("lab-v2", "acquisition", scanV2, "Version 2 lab", 4),
    profile("replay", "acquisition", replay, "Recorded data", 5),
    profile("band-a", "filter", bandpass, "Band A", 6),
    profile("band-b", "filter", bandpass, "Band B", 7),
    profile("notch-a", "filter", notch, "Notch A", 8),
    { ...profile("decoder-a", "decoder", decoder, "Decoder A", 1), processing_input: decoderInput(["C3", "C4"]) },
    { ...profile("decoder-b", "decoder", decoder, "Decoder B", 2), processing_input: decoderInput(["P3", "P4"]) },
    profile("detector-a", "epoch_detector", selection("example.detector"), "Detector", 1),
    profile("trigger-a", "trigger", trigger, "Hardware trigger", 1),
    profile("paradigm-a", "paradigm", paradigm, "Visual stimulation", 1),
    profile("effector-a", "effector", effector, "Test effector", 1),
  ];
  const bind = (slot, id) => {
    const saved = profiles.find((item) => item.profile_id === id);
    return { slot, configuration_version: 1, profile_id: id, profile_revision: saved.revision, kind: saved.kind, selection: clone(saved.selection), parameters: clone(saved.parameters), ...(saved.processing_input ? { processing_input: clone(saved.processing_input) } : {}) };
  };
  return { profiles, definitions: [
    definition("acquisition", scanV1, "Scan45"), definition("acquisition", scanV2, "Scan45"),
    definition("acquisition", replay, "Replay"), definition("acquisition", unsavedPlugin, "New source"),
    definition("filter", bandpass, "Bandpass"), definition("filter", notch, "Notch"),
    definition("filter", emptyFilter, "New filter"), { ...definition("decoder", decoder, "Decoder"), channel_requirements: clone(channelRequirements) },
    definition("epoch_detector", selection("example.detector"), "Epoch detector"),
    definition("trigger", trigger, "Hardware trigger"), definition("paradigm", paradigm, "Visual stimulation"),
    definition("effector", effector, "Effector"),
  ], activation: { revision: "accepted-revision", bindings: [bind("acquisition.source", "lab-a"),
    bind("processing.filter.first", "band-a"), bind("processing.filter.second", "notch-a"), bind("processing.decoder", "decoder-a")] } };
}

const sameDependencies = (left, right) => !!left && !!right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));

/** A persistent dispatcher runs this component's hooks without a browser or new test dependencies. */
function componentHarness(props) {
  const hooks = [];
  let cursor = 0;
  let needsRender = true;
  let tree; let controls;
  let pendingEffects = [];
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      if (!hooks[index]) hooks[index] = { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (next) => {
        const value = typeof next === "function" ? next(hooks[index].value) : next;
        if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; needsRender = true; }
      }];
    },
    useRef(initial) { const index = cursor++; return hooks[index] ??= { current: initial }; },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!hooks[index] || !sameDependencies(hooks[index].deps, deps)) hooks[index] = { value: callback, deps };
      return hooks[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      const previous = hooks[index];
      if (!previous || !sameDependencies(previous.deps, deps)) {
        hooks[index] = { deps, cleanup: previous?.cleanup };
        pendingEffects.push(() => { hooks[index].cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  function render() {
    const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
    const previous = internals.current;
    internals.current = dispatcher;
    cursor = 0; needsRender = false;
    try {
      props.current = useCurrentConfiguration(props.runtime.status.accepted, props.runtime.completedRestorations);
      const runtime = runtimeController(props.runtime);
      const engine = { data: { running: props.engineRunning, processor_alive: false, actuation_alive: false, actuation_connected: false, ...props.engine },
        error: props.engineAvailable === false ? new Error("Engine unavailable") : null, refresh: async () => {} };
      const runtimeControls = useRuntimeControls(runtime, props.current, props.acquisition, engine);
      controls = runtimeControls;
      tree = PluginProfiles({ ...props, runtime, runtimeControls });
    } finally { internals.current = previous; }
    const effects = pendingEffects; pendingEffects = [];
    effects.forEach((effect) => effect());
  }
  return {
    get tree() { return tree; },
    get controls() { return controls; },
    updateProps(next) { Object.assign(props, next); needsRender = true; },
    async settle() {
      for (let round = 0; round < 60; round += 1) {
        if (needsRender) render();
        await Promise.resolve();
      }
      assert.equal(needsRender, false, "Component did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!React.isValidElement(tree)) return [];
  if (tree.type === ModuleRuntime) return elements(ModuleRuntime(tree.props));
  return [tree, ...elements(tree.props.children)];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree) && tree.type === ModuleRuntime) return text(ModuleRuntime(tree.props));
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree === null || tree === undefined || typeof tree === "boolean" ? "" : String(tree);
}
function button(harness, label, scope = harness.tree) {
  const result = elements(scope).find((item) => item.type === "button" && text(item) === label);
  assert.ok(result, `Missing button: ${label}`);
  return result;
}
function control(harness, label, type = "select") {
  const all = elements(harness.tree);
  const explicit = all.find((item) => item.type === type && item.props["aria-label"] === label);
  if (explicit) return explicit;
  const labels = ["Profile", "Profile (optional)"].includes(label) ? ["Profile", "Profile (optional)", "Existing profile (optional)"] : [label];
  const owner = all.find((item) => item.type === "label" && (labels.includes(text(item)) || elements(item.props.children)
    .some((child) => child.type === "span" && labels.includes(text(child)))));
  assert.ok(owner, `Missing label: ${label}`);
  const result = elements(owner).find((item) => item.type === type)
    ?? all.find((item) => item.type === type && item.props.id === owner.props.htmlFor);
  assert.ok(result, `Missing ${type}: ${label}`);
  return result;
}
const options = (select) => elements(select).filter((item) => item.type === "option").map((item) => ({ value: item.props.value, label: text(item) }));
const form = (harness) => elements(harness.tree).find((item) => item.type.name === "SchemaForm");
const input = (harness) => elements(harness.tree).find((item) => item.type.name === "ProcessingInput");
const listedProfiles = (harness) => elements(harness.tree).filter((item) => item.type === "button"
  && item.props.className?.includes("profile-item"));
const metadata = (harness) => elements(harness.tree).find((item) => item.type.name === "AcquisitionMetadata");
const dialog = (harness) => elements(harness.tree).find((item) => item.type.name === "ConfigDialog");
const filterRows = (harness) => elements(harness.tree).filter((item) => item.type === "ol" && item.props.className === "filter-chain")
  .flatMap((item) => React.Children.toArray(item.props.children));

async function click(harness, label, scope) {
  const target = button(harness, label, scope);
  assert.equal(!!target.props.disabled, false, `${label} should be enabled`);
  await target.props.onClick(); await harness.settle();
}
async function inspectSource(harness) {
  if (!button(harness, "Connect").props.disabled) await click(harness, "Connect");
  assert.equal(metadata(harness).props.canRead, true, "Metadata requires the connected selected source");
  metadata(harness).props.onRead(); await harness.settle();
}

async function change(harness, label, value, type = "select") {
  const target = control(harness, label, type);
  assert.equal(!!target.props.disabled, false, `${label} should be enabled`);
  await target.props.onChange({ target: { value } }); await harness.settle();
}

async function saveShortcut(harness, overrides = {}) {
  let prevented = false;
  harness.tree.props.onKeyDown({ key: "s", ctrlKey: true, metaKey: false, altKey: false,
    repeat: false, defaultPrevented: false, nativeEvent: {}, preventDefault() { prevented = true; }, ...overrides });
  await harness.settle();
  return prevented;
}

async function withProfiles(module, action, { engineRunning = false, streaming = false, connectSucceeds = true, configure } = {}) {
  const originalApi = { ...api };
  const originalControlApi = { ...controlApi };
  const originalRuntimeApi = { ...runtimeConfigApi };
  const originalSignalApi = { ...signalConfigApi };
  const originalDocument = globalThis.document;
  globalThis.document = { hidden: false };
  const data = fixture();
  data.source = { connected: engineRunning, streaming, connection_id: engineRunning ? "connection-a" : null };
  data.metadata = { source_id: "scan45", connection_id: "connection-a",
    metadata: "Source device\nProtocol: 1.2\nExtensions: events", descriptor: null };
  data.events = [];
  data.profileKinds = [];
  configure?.(data);
  const modules = ["acquisition", "processing", "stimulation", "actuation"];
  data.revisions = Object.fromEntries([...modules, "engine"].map((module) => [module, data.activation ? `${module}-accepted` : null]));
  data.modules = () => ({ modules: Object.fromEntries(modules.map((module) => [module, {
    module, revision: data.revisions[module], settings: Object.fromEntries(Object.entries(data.activation?.settings ?? {})
      .filter(([key]) => key.startsWith(`${module.toUpperCase()}.`) || module === "processing" && key === "ALGORITHM.baseline_duration")),
    bindings: clone((data.activation?.bindings ?? []).filter((binding) => binding.slot.startsWith(`${module}.`))),
  }])), engine: { revision: data.revisions.engine, settings: {} } });
  const runtimeStatus = { recovery: {}, operations: {}, running: engineRunning, accepted: clone(data.revisions),
    applied: engineRunning ? clone(data.revisions) : null, generation: engineRunning ? "generation-a" : null };
  const calls = { create: [], connect: [] };
  api.catalog = async (kind) => clone(data.definitions.filter((item) => item.kind === kind));
  api.profiles = async (kind) => {
    data.profileKinds.push(kind);
    return clone(data.profiles.filter((item) => kind === undefined || item.kind === kind));
  };
  api.modules = async () => data.modules();
  api.module = async (module) => data.modules().modules[module];
  api.create = async (kind, choice, name, parameters, processing_input) => {
    const input = processing_input === undefined ? {} : { processing_input: clone(processing_input) };
    calls.create.push(clone({ kind, selection: choice, name, parameters, ...input }));
    const saved = { profile_id: `created-${calls.create.length}`, kind, selection: clone(choice), name, parameters: clone(parameters), revision: 1, configuration_version: 1, ...input };
    data.profiles.push(saved); return clone(saved);
  };
  controlApi.acquisitionStatus = async () => clone(data.source);
  controlApi.stimulationStatus = async () => ({ active: false, process_alive: false, engine_connected: false, paradigm: null });
  signalConfigApi.get = async () => null;
  data.moduleRequests = [];
  const startModule = async (module, { operation_id, configuration }) => {
    data.moduleRequests.push({ module, configuration: clone(configuration) });
    data.activation ??= { bindings: [] };
    data.activation.bindings = [...data.activation.bindings.filter((item) => !item.slot.startsWith(`${module}.`)),
      ...clone(configuration.bindings)];
    data.revisions[module] = `${module}-started`;
    runtimeStatus.accepted = clone(data.revisions); runtimeStatus.applied = clone(data.revisions);
    return { operation_id, module, status: "succeeded", target_revisions: { [module]: data.revisions[module] },
      result: { configuration_applied: true, action: "started" } };
  };
  controlApi.moduleStart = startModule;
  controlApi.stimStart = (body) => startModule("stimulation", body);
  controlApi.engineStart = async () => { data.events.push("start"); runtimeStatus.running = true; runtimeStatus.applied = clone(data.revisions); runtimeStatus.generation = "generation-a"; };
  controlApi.acqConnect = async ({ operation_id, configuration: body }) => {
    data.events.push("connect"); calls.connect.push(clone(body));
    if (!connectSucceeds) return { operation_id, module: "acquisition", status: "failed", error: "Source configuration rejected", result: null };
    data.activation ??= { revision: null, bindings: [] };
    data.activation.bindings = [...data.activation.bindings.filter((item) => !item.slot.startsWith("acquisition.")),
      ...body.bindings.map((item) => ({ ...clone(item), kind: "acquisition", configuration_version: item.configuration_version ?? 1,
        profile_id: null, profile_revision: null }))];
    data.revisions.acquisition = `connection-${calls.connect.length}`;
    data.revisions.engine ??= "engine-first";
    runtimeStatus.running = true; runtimeStatus.generation = "generation-a";
    runtimeStatus.accepted = clone(data.revisions); runtimeStatus.applied = clone(data.revisions);
    data.source.connected = true; data.source.connection_id = "connection-a";
    return { operation_id, module: "acquisition", status: "succeeded", target_revisions: { acquisition: data.revisions.acquisition },
      result: { configuration_applied: true, action: "connected" } };
  };
  controlApi.acqDisconnect = async () => { data.events.push("disconnect"); data.source.connected = false; data.source.connection_id = null; };
  controlApi.acqMetadata = async () => { data.events.push("metadata"); return clone(data.metadata); };
  runtimeConfigApi.status = async () => runtimeStatus;
  const runtime = { blocked: false, status: runtimeStatus, refresh: async () => {} };
  data.runtime = runtime;
  let harness;
  const acquisition = {
    data: clone(data.source), error: null, pending: false,
    refresh: async () => {
      try { acquisition.data = await controlApi.acquisitionStatus(); acquisition.error = null; }
      catch (cause) { acquisition.error = cause; }
      harness?.updateProps({ acquisition: { ...acquisition }, engineRunning: runtime.status.running });
    },
  };
  const command = (execute) => {
    const state = { pending: false, error: null, run: async (...args) => {
      state.pending = true; state.error = null; acquisition.pending = true;
      harness.updateProps({ acquisition: { ...acquisition } });
      try { await execute(...args); }
      catch (cause) { state.error = String(cause); }
      finally {
        await acquisition.refresh();
        state.pending = false; acquisition.pending = false;
        harness.updateProps({ acquisition: { ...acquisition } });
      }
    } };
    return state;
  };
  acquisition.connect = command(async (body) => {
    const result = await controlApi.acqConnect({ operation_id: crypto.randomUUID(), configuration: body });
    if (result.result?.configuration_applied) data.current().acknowledge("acquisition", body, result.target_revisions.acquisition);
    if (result.status !== "succeeded") throw new Error(result.error);
  });
  acquisition.disconnect = command(() => controlApi.acqDisconnect());
  acquisition.read = command(() => controlApi.acqStreamStart());
  acquisition.stopRead = command(() => controlApi.acqStreamStop());
  data.acquisition = acquisition;
  data.openRunCalls = 0;
  const props = { module, engineRunning, acquisition, runtime, onRun: () => { data.openRunCalls += 1; } };
  harness = componentHarness(props);
  data.remount = async () => {
    harness.dispose();
    harness = componentHarness({ ...props, engineRunning: runtime.status.running });
    await harness.settle();
    return harness;
  };
  try {
    await harness.settle();
    data.current = () => props.current;
    await action(harness, calls, data);
  }
  finally {
    harness.dispose(); Object.assign(api, originalApi); Object.assign(controlApi, originalControlApi);
    Object.assign(runtimeConfigApi, originalRuntimeApi); Object.assign(signalConfigApi, originalSignalApi);
    if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument;
  }
}

async function saveDraft(harness, { name = "Saved current values" } = {}) {
  await click(harness, "Save as profile");
  dialog(harness).props.onConfirm(name);
  await harness.settle();
}

for (const [module, slot, profileId] of [
  ["acquisition", "acquisition.source", "lab-a"], ["processing", "processing.decoder", "decoder-a"],
  ["stimulation", "stimulation.paradigm", "paradigm-a"], ["actuation", "actuation.effector", "effector-a"],
]) {
  test(`${module} initializes from the selected snapshot instead of a newer saved profile`, async () => {
    await withProfiles(module, async (h, calls, data) => {
      assert.equal(form(h).props.value.gain, 2);
      assert.equal(control(h, "Load saved profile").props.value, "");
      assert.equal(data.current().hasChanges(module, data.modules().modules[module]), false);
      assert.deepEqual(calls.create, []);
    }, { configure: (data) => {
      const saved = data.profiles.find((item) => item.profile_id === profileId);
      data.activation.bindings = data.activation.bindings.filter((item) => item.slot !== slot);
      data.activation.bindings.push({ ...clone(saved), slot, profile_revision: saved.revision,
        parameters: { gain: 2, calibration: [3, 4] } });
      saved.parameters.gain = 17; saved.revision += 1;
    } });
  });
  test(`${module} retains edited current values without manual Apply or profile Update actions`, async () => {
    await withProfiles(module, async (h, calls, data) => {
      const saved = clone(data.profiles);
      form(h).props.onChange({ gain: 23, calibration: [4, 5] }); await h.settle();
      const labels = elements(h.tree).filter((item) => item.type === "button").map(text);
      assert.ok(!labels.includes("Apply current configuration"));
      assert.ok(!labels.includes("Update saved profile"));
      assert.equal(dialog(h), undefined);
      const request = data.current().request(module);
      assert.ok(request.bindings.every((item) => item.slot.startsWith(`${module}.`) && !item.profile_id));
      assert.equal(request.bindings.find((item) => item.slot === slot).parameters.gain, 23);
      assert.deepEqual(data.profiles, saved);
      assert.equal(data.current().hasChanges(module, data.modules().modules[module]), true);
      assert.deepEqual(calls, { create: [], connect: [] });
    });
  });
}

test("Connect applies displayed source values without touching profiles or other modules", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    const others = clone(data.activation.bindings.filter((item) => item.kind !== "acquisition"));
    form(h).props.onChange({ gain: 9, calibration: [3, 4] }); await h.settle();
    await click(h, "Connect");
    assert.deepEqual(data.events, ["connect"]);
    assert.equal(calls.connect[0].bindings[0].parameters.gain, 9);
    assert.equal(calls.create.length, 0);
    assert.deepEqual(data.activation.bindings.filter((item) => item.kind !== "acquisition"), others);
    assert.equal(metadata(h).props.value, null);
  });
});

test("a first-use source connects without saved profiles or an existing activation", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    assert.equal(form(h).props.value.gain, 1);
    await click(h, "Connect");
    assert.deepEqual(data.events, ["connect"]);
    assert.equal(calls.connect[0].expected_revision, null);
    assert.equal(calls.create.length, 0);
  }, { configure: (data) => { data.profiles = []; data.activation = null; } });
});

test("failed application retains current edits and stops before connecting", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    form(h).props.onChange({ gain: 9, calibration: [3, 4] }); await h.settle();
    await click(h, "Connect");
    assert.deepEqual(data.events, ["connect"]);
    assert.equal(form(h).props.value.gain, 9);
    assert.equal(data.current().hasChanges("acquisition", data.modules().modules.acquisition), true);
    assert.equal(calls.create.length, 0);
  }, { connectSucceeds: false });
});

test("loading copies a profile without applying it or linking subsequent edits to saved values", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    const activation = clone(data.activation);
    await change(h, "Load saved profile", "lab-b");
    assert.equal(form(h).props.value.gain, 3);
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    assert.equal(data.profiles.find((profile) => profile.profile_id === "lab-b").parameters.gain, 3);
    assert.deepEqual(data.activation, activation);
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

test("Save as creates a snapshot without overwriting the loaded profile or applying values", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    const originalProfiles = clone(data.profiles);
    const activation = clone(data.activation);
    await change(h, "Load saved profile", "lab-a");
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    await saveDraft(h, { name: "New experiment" });
    assert.equal(calls.create[0].parameters.gain, 12);
    assert.equal(calls.create[0].name, "New experiment");
    await saveDraft(h, { name: "Another experiment" });
    assert.equal(calls.create.length, 2);
    assert.equal(calls.connect.length, 0);
    assert.deepEqual(data.profiles.slice(0, originalProfiles.length), originalProfiles);
    assert.deepEqual(data.activation, activation);
    assert.equal(control(h, "Load saved profile").props.value, "created-2");
    assert.equal(form(h).props.value.gain, 12);
  });
});

test("failed saves retain current values and their loaded profile", async () => {
  await withProfiles("acquisition", async (h, calls) => {
    await change(h, "Load saved profile", "lab-a");
    form(h).props.onChange({ gain: 11, calibration: [5, 6] }); await h.settle();
    api.create = async () => { throw new Error("Save unavailable"); };
    await saveDraft(h);
    assert.match(text(h.tree), /Save unavailable/);
    assert.equal(form(h).props.value.gain, 11);
    assert.equal(control(h, "Load saved profile").props.value, "lab-a");
    assert.equal(calls.connect.length, 0);
  });
});

test("replacing edited current values requires explicit confirmation", async () => {
  await withProfiles("acquisition", async (h) => {
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    await change(h, "Load saved profile", "lab-b");
    assert.equal(form(h).props.value.gain, 12);
    dialog(h).props.onCancel(); await h.settle();
    assert.equal(form(h).props.value.gain, 12);
    await change(h, "Load saved profile", "lab-b");
    dialog(h).props.onConfirm(); await h.settle();
    assert.equal(form(h).props.value.gain, 3);
  });
});

test("switching processing stages retains decoder values and mapping without discarding", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    input(h).props.onChange(decoderInput(["P3", "P4"])); await h.settle();
    await click(h, "Epoch boundary detector");
    assert.equal(dialog(h), undefined);
    form(h).props.onChange({ gain: 8, calibration: [7, 8] }); await h.settle();
    await click(h, "Decoder");
    assert.equal(form(h).props.value.gain, 12);
    assert.deepEqual(input(h).props.value.outputs.map((item) => item.name), ["P3", "P4"]);
    const request = data.current().request("processing");
    assert.equal(request.bindings.find((item) => item.slot === "processing.epoch_detector").parameters.gain, 8);
    assert.equal(request.bindings.find((item) => item.slot === "processing.decoder").processing_input.outputs[0].name, "P3");
    assert.equal(data.current().dirty, true);
    assert.equal(calls.connect.length, 0);
  });
});

test("Open Run retains current edits in the shared controller without a save prompt", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    await click(h, "Open Run");
    assert.equal(data.openRunCalls, 1);
    assert.equal(dialog(h), undefined);
    assert.equal(data.current().dirty, true);
    assert.equal(data.current().request("acquisition").bindings[0].parameters.gain, 12);
    assert.equal(calls.create.length + calls.connect.length, 0);
  });
});

test("filter instances retain independent values and order without saved profiles", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    await click(h, "Filters");
    await change(h, "Plugin and version", selectionKey(emptyFilter));
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    await click(h, "Add filter instance");
    const added = data.current().filterOrder.at(-1);
    await click(h, "New filter instance");
    form(h).props.onChange({ gain: 23, calibration: [7, 8] }); await h.settle();
    await click(h, "Add filter instance");
    const second = data.current().filterOrder.at(-1);
    assert.equal(data.current().entries[added].parameters.gain, 12);
    assert.equal(data.current().entries[second].parameters.gain, 23);
    assert.equal(data.current().entries[added].profile, null);
    await click(h, "Move up", filterRows(h).at(-1));
    assert.ok(!elements(h.tree).some((item) => item.type === "button" && text(item) === "Apply current filter chain"));
    const filters = data.current().request("processing").bindings.filter((item) => item.slot.startsWith("processing.filter."));
    assert.deepEqual(filters.map((item) => item.slot), data.current().filterOrder);
    assert.equal(filters.at(-2).parameters.gain, 23);
    assert.equal(filters.at(-1).parameters.gain, 12);
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

test("adding a valid filter edits current configuration while runtime or decoder application is blocked", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    data.runtime.blocked = true;
    h.updateProps({ runtime: data.runtime }); await h.settle();
    await click(h, "Filters");
    await change(h, "Plugin and version", selectionKey(emptyFilter));
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    assert.equal(button(h, "Add filter instance").props.disabled, false);
    await click(h, "Add filter instance");
    assert.equal(data.current().entries[data.current().filterOrder.at(-1)].parameters.gain, 12);
    assert.throws(() => data.current().request("processing"), /Configure the decoder input/);
    assert.equal(data.activation.bindings.filter((item) => item.kind === "filter").length, 2);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { configure: (data) => {
    const decoder = data.activation.bindings.find((binding) => binding.slot === "processing.decoder");
    decoder.processing_input = null;
  } });
});

test("editing one filter cannot mutate another instance with the same profile", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    await click(h, "Filters");
    elements(filterRows(h)[0]).find((item) => item.type === "button" && text(item).startsWith("Edit filter 1")).props.onClick(); await h.settle();
    form(h).props.onChange({ gain: 44, calibration: [1, 2] }); await h.settle();
    assert.equal(data.current().entries["processing.filter.first"].parameters.gain, 44);
    assert.equal(data.current().entries["processing.filter.second"].parameters.gain, 6);
    const request = data.current().request("processing");
    assert.equal(request.bindings.find((item) => item.slot === "processing.filter.first").parameters.gain, 44);
    assert.equal(calls.connect.length, 0);
  }, { configure: (data) => {
    const first = data.activation.bindings.find((item) => item.slot === "processing.filter.first");
    const second = data.activation.bindings.find((item) => item.slot === "processing.filter.second");
    Object.assign(second, clone(first), { slot: "processing.filter.second" });
  } });
});

test("removing filters leaves the runtime unchanged until application", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    await click(h, "Filters"); await click(h, "Remove", filterRows(h)[0]);
    assert.equal(data.activation.bindings.filter((item) => item.kind === "filter").length, 2);
    const request = data.current().request("processing");
    assert.deepEqual(request.bindings.filter((item) => item.slot.startsWith("processing.filter.")).map((item) => item.slot), ["processing.filter.second"]);
    assert.equal(calls.connect.length, 0);
  });
});

test("no trigger removes only the current trigger binding until the next runtime action", async () => {
  await withProfiles("stimulation", async (h, calls, data) => {
    await click(h, "Trigger"); await change(h, "Plugin and version", "");
    const request = data.current().request("stimulation");
    assert.ok(!request.bindings.some((item) => item.slot === "stimulation.trigger"));
    assert.deepEqual(calls, { create: [], connect: [] });
    assert.ok(data.activation.bindings.some((item) => item.slot === "stimulation.trigger"));
  }, { configure: (data) => {
    const saved = data.profiles.find((item) => item.profile_id === "trigger-a");
    data.activation.bindings.push({ ...clone(saved), slot: "stimulation.trigger", profile_revision: saved.revision });
  } });
});

test("invalid parameters block saving and connection", async () => {
  await withProfiles("acquisition", async (h, calls) => {
    form(h).props.onChange({ gain: 0, calibration: [3, 4] }); await h.settle();
    for (const label of ["Save as profile", "Connect"]) assert.equal(button(h, label).props.disabled, true);
    assert.equal(calls.connect.length, 0);
  });
});

test("profile search stays scoped to the plugin version and leaves current edits intact", async () => {
  await withProfiles("acquisition", async (h) => {
    form(h).props.onChange({ gain: 12, calibration: [5, 6] }); await h.settle();
    await change(h, "Search saved profiles", "Lab B", "input");
    assert.equal(listedProfiles(h).length, 1);
    assert.equal(form(h).props.value.gain, 12);
    assert.ok(!options(control(h, "Load saved profile")).some((item) => item.value === "lab-v2"));
  });
});

test("Save shortcut opens Save as and respects repeated keys and validation", async () => {
  await withProfiles("acquisition", async (h, calls) => {
    await saveShortcut(h, { repeat: true }); assert.equal(dialog(h), undefined);
    await saveShortcut(h); assert.equal(dialog(h).props.title, "Save as profile");
    assert.equal(calls.create.length, 0);
    dialog(h).props.onCancel(); await h.settle();
    form(h).props.onChange({ gain: 0, calibration: [3, 4] }); await h.settle();
    await saveShortcut(h); assert.equal(dialog(h), undefined);
  });
});

test("metadata is fetched explicitly and current parameter edits invalidate it", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    await inspectSource(h);
    assert.deepEqual(data.events, ["metadata"]);
    assert.equal(metadata(h).props.value.metadata, data.metadata.metadata);
    assert.equal(calls.create.length + calls.connect.length, 0);
    form(h).props.onChange({ gain: 9, calibration: [3, 4] }); await h.settle();
    assert.equal(metadata(h).props.value, null);
    assert.equal(metadata(h).props.canRead, false);
    assert.equal(button(h, "Connect").props.disabled, true);
    assert.match(text(h.tree), /Current parameters differ from the connected source/);
  }, { engineRunning: true });
});

test("a replaced connection invalidates metadata even while still connected", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    await inspectSource(h); data.source.connection_id = "replacement";
    await data.acquisition.refresh(); await h.settle();
    assert.equal(metadata(h).props.value, null);
    assert.equal(metadata(h).props.canRead, true);
    assert.equal(calls.connect.length, 0);
  }, { engineRunning: true });
});

test("streaming blocks connection changes and metadata retrieval", async () => {
  await withProfiles("acquisition", async (h) => {
    form(h).props.onChange({ gain: 9, calibration: [3, 4] }); await h.settle();
    assert.equal(button(h, "Connect").props.disabled, true);
    assert.equal(button(h, "Disconnect").props.disabled, true);
    assert.equal(metadata(h).props.canRead, false);
  }, { engineRunning: true, streaming: true });
});

test("refreshing the library preserves current values despite changed saved profiles", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    form(h).props.onChange({ gain: 8, calibration: [3, 4] }); await h.settle();
    data.profiles.find((item) => item.profile_id === "lab-a").parameters.gain = 17;
    await click(h, "Reload");
    assert.equal(form(h).props.value.gain, 8);
    assert.equal(calls.create.length + calls.connect.length, 0);
  });
});

function observedSource(data, expected = null, pluginType = "neuroscan_curry8_reader") {
  const sourceDefinition = data.definitions.find((item) => selectionKey(item.selection) === selectionKey(scanV1));
  sourceDefinition.plugin_type = pluginType;
  Object.assign(sourceDefinition.schema.properties, {
    sample_rate: { type: "integer", minimum: 1, title: "Expected sample rate (Hz)" },
    channel_count: { type: "integer", minimum: 2, title: "Expected output rows including event row" },
  });
  if (pluginType === "neuroscan_curry8_reader") sourceDefinition.schema.properties.channel_labels = {
    type: "array", minItems: 1, items: { type: "string" }, title: "Channel labels",
  };
  const saved = data.profiles.find((item) => item.profile_id === "lab-a");
  if (expected) Object.assign(saved.parameters, expected);
  data.activation.bindings.find((item) => item.slot === "acquisition.source").parameters = clone(saved.parameters);
  const descriptor = { source_id: pluginType, connection_id: "connection-a", sample_rate: 500,
    signal_channel_count: 2, output_row_count: 3, trigger_row_index: 2, channels: [],
    native_event: "final-row", provenance: pluginType === "file_reader" ? "file-reported" : "device-reported" };
  data.metadata = { source_id: pluginType, connection_id: "connection-a", metadata: "Device information", descriptor,
    comparison: { status: expected ? "mismatched" : "unconfirmed",
      sample_rate: { expected: expected?.sample_rate ?? null, actual: 500 },
      channel_count: { expected: expected?.channel_count ?? null, actual: 3 } } };
}

const curryLabels = ["C3", "C4", "Trigger"];
function observedCurryLabels(data, expected = null) {
  observedSource(data, expected);
  data.metadata.metadata = JSON.stringify({ sample_rate: 1000, channel_count: 10,
    channel_labels: curryLabels, protocol_version: 803, data_size: 4, allow_amplifier_control: true });
}


test("metadata is fetched explicitly without changing profiles or applied configuration", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const savedProfiles = clone(data.profiles);
    const accepted = clone(data.activation);
    assert.equal(metadata(harness).props.value, null);
    assert.equal(metadata(harness).props.onChange, undefined);
    metadata(harness).props.onRead(); await harness.settle();
    assert.deepEqual(data.events, ["metadata"]);
    assert.deepEqual(metadata(harness).props.value, data.metadata);
    assert.deepEqual(form(harness).props.value, { gain: 2, calibration: [3, 4] });
    assert.deepEqual(data.profiles, savedProfiles);
    assert.deepEqual(data.activation, accepted);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true });
});

test("failed metadata retrieval clears stale data and retains unsaved profile edits", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    metadata(harness).props.onRead(); await harness.settle();
    assert.ok(metadata(harness).props.value);
    controlApi.acqMetadata = async () => { throw new Error("Device metadata request timed out."); };
    metadata(harness).props.onRead(); await harness.settle();
    assert.match(text(harness.tree), /Device metadata request timed out/);
    assert.equal(metadata(harness).props.value, null);
    assert.equal(metadata(harness).props.busy, false);
    assert.equal(form(harness).props.value.gain, 2);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true });
});

test("reconnecting between polls clears metadata even while connected and runtime generation stay unchanged", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    assert.equal(metadata(harness).props.value.connection_id, "connection-a");
    const generation = (await runtimeConfigApi.status()).generation;
    data.source.connection_id = "connection-b";
    await data.acquisition.refresh(); await harness.settle();
    assert.equal(data.source.connected, true);
    assert.equal((await runtimeConfigApi.status()).generation, generation);
    assert.equal(metadata(harness).props.value, null, "The previous connection's metadata must be removed");
    assert.equal(metadata(harness).props.canRead, true, "The reconnected source can be read explicitly");
    assert.deepEqual(data.events, ["metadata"], "A status update must not fetch metadata implicitly");
    data.metadata.connection_id = "connection-b";
    metadata(harness).props.onRead(); await harness.settle();
    assert.equal(metadata(harness).props.value.connection_id, "connection-b");
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true });
});

test("required plugin slots do not offer the no-trigger option", async () => {
  for (const module of ["acquisition", "processing", "stimulation"]) {
    await withProfiles(module, async (harness) => {
      assert.equal(options(control(harness, "Plugin and version")).some((item) => item.value === ""), false);
    });
  }
});

test("invalid manual source rows block decoder profile saving", async () => {
  await withProfiles("processing", async (harness, calls) => {
    input(harness).props.onChange({ schema_version: 2, outputs: [{ name: "Oz", source_channel_index: -1 }] });
    await harness.settle();
    assert.equal(button(harness, "Save as profile").props.disabled, true);
    assert.equal(calls.create.length, 0);
  });
});

test("the input editor receives the browsed decoder requirements independently of the active decoder", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    const otherDecoder = selection("example.other-decoder");
    const otherRequirements = { min_count: 1, required_names: ["P3"], default_names: ["P3", "P4"] };
    data.definitions.push({ ...definition("decoder", otherDecoder, "Other decoder"), channel_requirements: otherRequirements });
    await click(harness, "Reload");
    await change(harness, "Plugin and version", selectionKey(otherDecoder));
    assert.deepEqual(input(harness).props.decoderSelection, otherDecoder);
    assert.deepEqual(input(harness).props.channelRequirements, otherRequirements);
    assert.deepEqual(input(harness).props.value.outputs, []);
    assert.deepEqual(data.activation.bindings.find((binding) => binding.slot === "processing.decoder").selection, decoder);
    assert.equal(calls.connect.length, 0);
  });
});

test("Curry8 label adoption preserves duplicate and blank names without synthesizing a trigger label", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const labels = [" C3 ", " C3 ", ""];
    data.metadata.metadata = JSON.stringify({ channel_labels: labels });
    metadata(harness).props.onRead(); await harness.settle();
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.deepEqual(form(harness).props.value.channel_labels, labels);
    assert.equal(calls.create.length, 0);
  }, { engineRunning: true, configure: observedCurryLabels });
});

test("File Reader does not import Curry8 labels from opaque metadata", async () => {
  await withProfiles("acquisition", async (harness) => {
    metadata(harness).props.onRead(); await harness.settle();
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.doesNotMatch(text(harness.tree), /Channel labels/);
  }, { engineRunning: true, configure: (data) => {
    observedSource(data, null, "file_reader");
    data.metadata.metadata = JSON.stringify({ channel_labels: curryLabels });
  } });
});

test("explicit metadata inspection can repeat on a connected source without promoting expectations", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await inspectSource(harness);
    await inspectSource(harness);
    assert.deepEqual(data.events, ["metadata", "metadata"]);
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.equal(data.activation.bindings[0].parameters.sample_rate, undefined);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true, configure: observedCurryLabels });
});

test("a delayed inspection response cannot be adopted after the source reconnects", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    let finishRead;
    controlApi.acqMetadata = () => new Promise((resolve) => { finishRead = resolve; });
    await inspectSource(harness);
    data.source.connection_id = "connection-b";
    await data.acquisition.refresh(); await harness.settle();
    finishRead(clone(data.metadata)); await harness.settle();
    assert.equal(metadata(harness).props.value, null);
    assert.equal(metadata(harness).props.canUseObserved, false);
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true, configure: observedCurryLabels });
});

test("a stale adoption callback cannot apply metadata from an earlier source connection", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    const staleAdopt = metadata(harness).props.onUseObserved;
    data.source.connection_id = "connection-b";
    await data.acquisition.refresh(); await harness.settle();
    staleAdopt(); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.equal(calls.create.length, 0);
    assert.equal(metadata(harness).props.canUseObserved, false);
  }, { engineRunning: true, configure: observedCurryLabels });
});

test("observations from an unexpected connection cannot be adopted", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    data.metadata.connection_id = "connection-old";
    metadata(harness).props.onRead(); await harness.settle();
    assert.equal(metadata(harness).props.canUseObserved, false);
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(calls.create.length, 0);
  }, { engineRunning: true, configure: observedSource });
});

test("streaming prevents adoption of a previously observed source", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    data.source.streaming = true;
    await data.acquisition.refresh(); await harness.settle();
    assert.equal(metadata(harness).props.canUseObserved, false);
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(calls.create.length, 0);
  }, { engineRunning: true, configure: observedSource });
});

test("Scan45 retains editable declarations and cannot adopt them as verified observations", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    assert.match(text(harness.tree), /Advanced acquisition defaults/);
    assert.equal(elements(harness.tree).filter((item) => item.type.name === "SchemaForm").length, 2);
    metadata(harness).props.onRead(); await harness.settle();
    assert.equal(metadata(harness).props.value.comparison.status, "unverified");
    assert.equal(metadata(harness).props.canUseObserved, false);
    assert.equal(metadata(harness).props.onUseObserved, undefined);
    assert.equal(metadata(harness).props.onSaveObservedProfile, undefined);
    assert.equal(calls.create.length, 0);
  }, { engineRunning: true, configure: (data) => {
    observedSource(data, { sample_rate: 500, channel_count: 3 }, "neuroscan_scan45_reader");
    data.metadata.descriptor.provenance = "user-declared";
    data.metadata.comparison.status = "unverified";
  } });
});

test("source controls disconnect the shared current source while browsing another profile, but never while streaming", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Load saved profile", "lab-b");
    assert.match(text(harness.tree), /Loaded profile: Lab B/);
    assert.doesNotMatch(text(harness.tree), /Current source.*Lab A/);
    assert.equal(button(harness, "Disconnect").props.disabled, false);
    await button(harness, "Disconnect").props.onClick(); await harness.settle();
    assert.deepEqual(data.events, ["disconnect"]);
    assert.equal(control(harness, "Load saved profile").props.value, "lab-b");
  }, { engineRunning: true });
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(button(harness, "Disconnect").props.disabled, true);
    await button(harness, "Disconnect").props.onClick(); await harness.settle();
    assert.deepEqual(data.events, []);
  }, { engineRunning: true, streaming: true });
});

for (const streaming of [false, true]) test(`source recovery allows Disconnect with stale streaming=${streaming}`, async () => {
  await withProfiles("acquisition", async (harness, _calls, data) => {
    data.runtime.status.recovery = { acquisition: "Worker state unknown" };
    data.runtime.scopes = { acquisition: { blocked: true, recoveryRequired: true } };
    harness.updateProps({ runtime: data.runtime }); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    await click(harness, "Disconnect");
    assert.deepEqual(data.events, ["disconnect"]);
  }, { streaming });
});

test("shared connection updates immediately enable or disable metadata and connection actions", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    assert.equal(button(harness, "Connect").props.disabled, false);
    Object.assign(data.runtime.status, { running: true, applied: clone(data.revisions), generation: "generation-a" });
    Object.assign(data.source, { connected: true, connection_id: "external-connection" });
    await data.acquisition.refresh(); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(button(harness, "Disconnect").props.disabled, false);
    assert.equal(metadata(harness).props.canRead, true);
    Object.assign(data.source, { connected: false, connection_id: null });
    await data.acquisition.refresh(); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, false);
    assert.equal(button(harness, "Disconnect").props.disabled, true);
    assert.equal(metadata(harness).props.canRead, false);
  });
});

test("shared status errors suppress a previously connected source", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    controlApi.acquisitionStatus = async () => { throw new Error("Connection status unavailable"); };
    await data.acquisition.refresh(); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(button(harness, "Disconnect").props.disabled, true);
    assert.equal(metadata(harness).props.canRead, false);
    assert.match(text(harness.tree), /Acquisition status is unavailable/);
  }, { engineRunning: true });
});

for (const raw of ["Device information", "null", "[]", "{}", '{"channel_labels":null}',
  '{"channel_labels":"C3,C4,Trigger"}', '{"channel_labels":[]}', '{"channel_labels":["C3","C4"]}',
  '{"channel_labels":["C3",4,"Trigger"]}', '{"channel_labels":["C3",null,"Trigger"]}']) {
  test(`Curry8 adoption omits stale labels when metadata is ${raw}`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      assert.deepEqual(form(harness).props.value.channel_labels, curryLabels);
      data.metadata.metadata = raw;
      metadata(harness).props.onRead(); await harness.settle();
      metadata(harness).props.onUseObserved(); await harness.settle();
      assert.equal(form(harness).props.value.channel_labels, undefined);
      assert.equal(form(harness).props.value.sample_rate, 500);
      assert.equal(form(harness).props.value.channel_count, 3);
      assert.equal(calls.create.length, 0);
    }, { engineRunning: true, configure: (data) => observedCurryLabels(data, {
      sample_rate: 1000, channel_count: 3, channel_labels: curryLabels,
    }) });
  });
}


for (const [label, action] of [
  ["profile", async (harness) => change(harness, "Load saved profile", "lab-b")],
  ["plugin", async (harness) => change(harness, "Plugin and version", selectionKey(replay))],
  ["parameters", async (harness) => { form(harness).props.onChange({ gain: 8, calibration: [3, 4] }); await harness.settle(); }],
  ["connection state", async (harness) => click(harness, "Disconnect")],
]) {
  test(`changing ${label} clears metadata from the previous source`, async () => {
    await withProfiles("acquisition", async (harness, calls) => {
      metadata(harness).props.onRead(); await harness.settle();
      assert.ok(metadata(harness).props.value);
      await action(harness);
      assert.equal(metadata(harness).props.value, null);
      assert.equal(metadata(harness).props.canRead, false);
      assert.deepEqual(calls, { create: [], connect: [] });
    }, { engineRunning: true });
  });
}


test("invalid decoder mappings require explicit configuration before a processing request", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    assert.equal(input(h).props.value, null);
    assert.equal(button(h, "Save as profile").props.disabled, true);
    assert.match(data.current().validationError("processing"), /Configure the decoder input/);
    assert.throws(() => data.current().request("processing"), /Configure the decoder input/);
    await click(h, "Filters");
    assert.throws(() => data.current().request("processing"), /Configure the decoder input/);
    await click(h, "Decoder");
    input(h).props.onChange({ schema_version: 2, outputs: [] }); await h.settle();
    assert.equal(data.current().validationError("processing"), null);
    const request = data.current().request("processing");
    assert.deepEqual(request.bindings.find((binding) => binding.slot === "processing.decoder").processing_input,
      { schema_version: 2, outputs: [] });
    assert.equal(calls.connect.length, 0);
  }, { configure: (data) => {
    const binding = data.activation.bindings.find((item) => item.slot === "processing.decoder");
    binding.processing_input = null;
  } });
});

test("loading a invalid decoder profile cannot silently select all channels", async () => {
  await withProfiles("processing", async (h, _, data) => {
    await change(h, "Load saved profile", "decoder-b");
    assert.equal(input(h).props.value, null);
    assert.throws(() => data.current().request("processing"), /Configure the decoder input/);
    assert.equal(button(h, "Save as profile").props.disabled, true);
  }, { configure: (data) => { data.profiles.find((item) => item.profile_id === "decoder-b").processing_input = null; } });
});

test("Connect normalizes schema defaults for application and comparison", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    form(h).props.onChange({ gain: 9 }); await h.settle();
    await click(h, "Connect");
    assert.deepEqual(calls.connect[0].bindings[0].parameters, { gain: 9, calibration: [1, 2] });
    assert.equal(data.current().dirty, false);
    assert.equal(data.current().hasChanges("acquisition", data.modules().modules.acquisition), false);
    assert.equal(metadata(h).props.canRead, true);
    assert.equal(form(h).props.value.calibration, undefined, "Normalization does not mutate the editable object");
  }, { configure: (data) => {
    data.definitions.find((item) => selectionKey(item.selection) === selectionKey(scanV1)).schema.required = ["gain"];
  } });
});

test("reloading after an activation read fails blocks further commands until retry succeeds", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    form(h).props.onChange({ gain: 9, calibration: [1, 2] }); await h.settle();
    const activation = api.module;
    api.module = async () => { throw new Error("Configuration unavailable"); };
    await click(h, "Reload");
    assert.equal(data.current().modules.acquisition.ready, false);
    assert.match(text(h.tree), /Configuration unavailable/);
    assert.equal(button(h, "Connect").props.disabled, true);
    assert.equal(form(h).props.value.gain, 9);
    api.module = activation;
    await click(h, "Reload");
    assert.equal(data.current().modules.acquisition.ready, true);
    assert.equal(button(h, "Connect").props.disabled, false);
    assert.equal(form(h).props.value.gain, 9);
    assert.equal(calls.connect.length, 0);
  });
});

test("same-render Connect clicks submit the current configuration only once", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    const connect = button(h, "Connect").props.onClick;
    connect(); connect(); await h.settle();
    assert.deepEqual(data.events, ["connect"]);
    assert.equal(calls.connect.length, 1);
    assert.equal(calls.create.length, 0);
  });
});

test("confirmed profile saving remains successful when subsequent catalog reads fail", async () => {
  await withProfiles("acquisition", async (h, calls) => {
    form(h).props.onChange({ gain: 9, calibration: [1, 2] }); await h.settle();
    const create = api.create;
    api.create = async (...args) => {
      const saved = await create(...args);
      api.catalog = async () => { throw new Error("Catalog unavailable"); };
      return saved;
    };
    await saveDraft(h);
    assert.equal(calls.create.length, 1);
    assert.equal(control(h, "Load saved profile").props.value, "created-1");
    assert.equal(form(h).props.value.gain, 9);
    assert.doesNotMatch(text(h.tree), /Catalog unavailable/);
  });
});

test("an unavailable inline plugin remains visible without a saved profile", async () => {
  await withProfiles("acquisition", async (h, calls) => {
    const plugins = options(control(h, "Plugin and version"));
    assert.ok(plugins.some((item) => item.value === selectionKey(scanV1) && /unavailable/.test(item.label)));
    assert.equal(control(h, "Plugin and version").props.value, selectionKey(scanV1));
    assert.equal(button(h, "Connect").props.disabled, true);
    assert.equal(calls.connect.length, 0);
  }, { configure: (data) => {
    data.profiles = data.profiles.filter((item) => selectionKey(item.selection) !== selectionKey(scanV1));
    data.definitions = data.definitions.filter((item) => selectionKey(item.selection) !== selectionKey(scanV1));
    const source = data.activation.bindings.find((item) => item.slot === "acquisition.source");
    source.profile_id = null; source.profile_revision = null;
  } });
});

test("a failed processing refresh leaves unrelated module requests available", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    const current = data.current();
    assert.equal(current.modules.processing.ready, true);
    let rejectRefresh;
    const pending = new Promise((resolve, reject) => { rejectRefresh = reject; });
    api.module = () => pending;
    const refresh = current.reload("processing");
    await h.settle();
    assert.equal(data.current().modules.acquisition.ready, true);
    const request = data.current().request("actuation");
    assert.equal(request.expected_revision, data.revisions.actuation, "A newer projection must not replace the draft baseline");
    rejectRefresh(new Error("Baseline refresh failed"));
    await refresh; await h.settle();
    assert.equal(data.current().modules.processing.ready, false);
    assert.match(data.current().modules.processing.error, /Baseline refresh failed/);
    assert.doesNotThrow(() => data.current().request("actuation"));
    assert.equal(calls.connect.length, 0);
  });
});


test("unrelated module revisions do not invalidate a connected source or replace current drafts", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    assert.equal(metadata(h).props.canRead, true);
    const processing = data.current().get("processing.decoder");
    data.current().set("processing.decoder", { ...processing, parameters: { gain: 91, calibration: [1, 2] } });
    data.revisions.stimulation = "stimulation-next";
    data.runtime.status.accepted = clone(data.revisions);
    data.runtime.status.applied = clone(data.revisions);
    h.updateProps({ runtime: data.runtime }); await h.settle();
    assert.equal(metadata(h).props.canRead, true);
    assert.equal(data.current().get("processing.decoder").parameters.gain, 91);
    assert.equal(data.current().request("acquisition").expected_revision, "acquisition-accepted");
  }, { engineRunning: true });
});

test("a successful history revert replaces only that module current values and filter order", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    form(h).props.onChange({ gain: 91, calibration: [1, 2] }); await h.settle();
    const decoder = data.activation.bindings.find((binding) => binding.slot === "processing.decoder");
    decoder.parameters.gain = 44;
    const filters = data.activation.bindings.filter((binding) => binding.kind === "filter");
    data.activation.bindings = [...data.activation.bindings.filter((binding) => binding.kind !== "filter"), ...filters.reverse()];
    data.revisions.processing = "restored-processing";
    data.runtime.status.accepted = clone(data.revisions);
    data.runtime.completedRestorations = { processing: { module: "processing", operationId: "restore-operation", revision: "restored-processing" } };
    h.updateProps({ runtime: data.runtime }); await h.settle();
    assert.equal(data.current().get("processing.decoder").parameters.gain, 44);
    assert.deepEqual(data.current().filterOrder, filters.map((binding) => binding.slot));
    assert.equal(form(h).props.value.gain, 91);
    assert.equal(data.current().hasChanges("processing", data.modules().modules.processing), false);
    assert.equal(data.current().hasChanges("acquisition", data.modules().modules.acquisition), true);
  });
});

test("a changed shared engine revision invalidates source observations without changing its module hash", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    assert.equal(metadata(h).props.canRead, true);
    data.runtime.status.accepted = { ...data.runtime.status.accepted, engine: "new-engine" };
    h.updateProps({ runtime: data.runtime }); await h.settle();
    assert.equal(metadata(h).props.canRead, false);
  }, { engineRunning: true });
});


test("an intervening module change after revert preserves current edits and allows explicit recovery", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    form(h).props.onChange({ gain: 91, calibration: [1, 2] }); await h.settle();
    data.revisions.processing = "newer-than-restore";
    data.runtime.status.accepted = clone(data.revisions);
    data.runtime.completedRestorations = { processing: { module: "processing", operationId: "restore-operation", revision: "restored-processing" } };
    h.updateProps({ runtime: data.runtime }); await h.settle();
    assert.equal(data.current().modules.processing.ready, false);
    assert.equal(data.current().modules.acquisition.ready, true);
    assert.match(data.current().modules.processing.error, /restored module changed again/);
    assert.equal(form(h).props.value.gain, 91);
    await data.current().reload("processing"); await h.settle();
    assert.equal(data.current().modules.acquisition.ready, true);
    assert.equal(form(h).props.value.gain, 91);
  });
});

test("Processing edits pre-trigger duration as current module settings without saving defaults or a profile", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    const duration = () => elements(h.tree).find((item) => item.type === "input" && item.props.id === "processing-pre-trigger-duration");
    assert.equal(duration().props.value, "0");
    duration().props.onChange({ target: { value: "1.5" } }); await h.settle();
    const body = data.current().request("processing");
    assert.equal(body.settings["ALGORITHM.baseline_duration"], 1.5);
    assert.equal(data.current().dirty, true);
    assert.deepEqual(calls, { create: [], connect: [] });
    duration().props.onChange({ target: { value: "-1" } }); await h.settle();
    assert.throws(() => data.current().request("processing"), /non-negative/);
  });
});

for (const module of ["stimulation", "actuation"]) {
  test(`${module} settings stay with the module across plugin profiles and navigation`, async () => {
    await withProfiles(module, async (h, calls, data) => {
      const prefix = module.toUpperCase();
      assert.equal(control(h, "Run mode").props.value, "managed");
      assert.equal(control(h, "Listen port", "input").props.value, module === "stimulation" ? "8765" : "8001");
      await change(h, "Run mode", "external");
      await change(h, "Listen address", "192.0.2.50", "input");
      await change(h, "Listen port", "9012", "input");
      if (module === "stimulation") await change(h, "Renderer", "pyopengl");
      else {
        await change(h, "Watchdog timeout (s)", "2.5", "input");
        await change(h, "Maximum command age (s)", "0", "input");
      }
      const expected = clone(data.current().request(module).settings);
      assert.equal(expected[`${prefix}.auto_spawn`], false);
      assert.equal(expected[`${prefix}.server_ip`], "192.0.2.50");
      assert.equal(expected[`${prefix}.server_port`], 9012);
      const choice = module === "stimulation" ? paradigm : effector;
      await change(h, "Plugin and version", selectionKey(choice));
      await change(h, "Load saved profile", module === "stimulation" ? "paradigm-a" : "effector-a");
      assert.deepEqual(data.current().request(module).settings, expected);
      await click(h, "Save as profile");
      dialog(h).props.onConfirm("Module plugin copy"); await h.settle();
      assert.equal(calls.create.length, 1);
      assert.deepEqual(calls.create[0].parameters, { gain: 1, calibration: [3, 4] });
      assert.equal("settings" in calls.create[0], false);
      h.updateProps({ module: "acquisition" }); await h.settle();
      h.updateProps({ module }); await h.settle();
      assert.equal(control(h, "Run mode").props.value, "external");
      assert.equal(control(h, "Listen port", "input").props.value, "9012");
      assert.deepEqual(data.current().request(module).settings, expected);
      assert.deepEqual(data.current().request(module === "stimulation" ? "actuation" : "stimulation").settings, {});
    });
  });

  test(`${module} rejects invalid current module settings without changing other modules`, async () => {
    await withProfiles(module, async (h, _, data) => {
      const prefix = module.toUpperCase();
      const invalid = [["auto_spawn", "false"], ["server_ip", "  "], ["server_ip", 127],
        ["server_port", ""], ["server_port", 0], ["server_port", 65536], ["server_port", 12.5], ["server_port", Infinity],
        ...(module === "stimulation" ? [["renderer", "unsupported"]]
          : [["watchdog_timeout_s", 0], ["watchdog_timeout_s", -1], ["watchdog_timeout_s", NaN],
            ["max_command_age_s", -1], ["max_command_age_s", Infinity]])];
      const original = clone(data.current().modules[module].activation);
      for (const [setting, value] of invalid) {
        data.current().setSetting(module, `${prefix}.${setting}`, value); await h.settle();
        assert.throws(() => data.current().request(module), /mode|address|port|renderer|timeout|age/i, `${setting}=${value}`);
        assert.doesNotThrow(() => data.current().request("acquisition"));
        data.current().restoreModule(original); await h.settle();
      }
    });
  });
}

test("a remote accepted revision cannot rebase an edited module draft", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    const original = data.current().request("acquisition");
    form(h).props.onChange({ gain: 23, calibration: [4, 5] }); await h.settle();
    data.revisions.acquisition = "other-client";
    data.activation.bindings.find((binding) => binding.slot === "acquisition.source").parameters.gain = 88;
    await data.current().reload("acquisition"); await h.settle();
    assert.equal(data.current().modules.acquisition.activation.revision, "other-client");
    assert.equal(data.current().request("acquisition").expected_revision, original.expected_revision);
    assert.equal(form(h).props.value.gain, 23);
  });
});

test("confirming an interrupted action preserves edits made after submission and advances only its own baseline", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    form(h).props.onChange({ gain: 12, calibration: [1, 2] }); await h.settle();
    const submitted = data.current().request("acquisition");
    form(h).props.onChange({ gain: 31, calibration: [4, 5] }); await h.settle();
    data.revisions.acquisition = "own-confirmed";
    data.activation.bindings.find((binding) => binding.slot === "acquisition.source").parameters = clone(submitted.bindings[0].parameters);
    data.current().acknowledge("acquisition", submitted, "own-confirmed");
    await h.settle();
    assert.equal(form(h).props.value.gain, 31);
    assert.equal(data.current().request("acquisition").expected_revision, "own-confirmed");
    assert.equal(data.current().dirty, true);
    data.revisions.acquisition = "other-client";
    await data.current().reload("acquisition"); await h.settle();
    assert.equal(form(h).props.value.gain, 31);
    assert.equal(data.current().request("acquisition").expected_revision, "own-confirmed");
  });
});

for (const editedAfterSubmission of [false, true]) test(`a module read started before confirmation cannot overwrite its accepted draft (later edits=${editedAfterSubmission})`, async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    form(h).props.onChange({ gain: 12, calibration: [1, 2] }); await h.settle();
    const submitted = data.current().request("acquisition");
    const old = data.modules().modules.acquisition;
    let finish;
    const read = api.module;
    let reads = 0;
    api.module = (module) => ++reads === 1 ? new Promise((resolve) => { finish = resolve; }) : read(module);
    const loading = data.current().reload("acquisition");
    data.revisions.acquisition = "confirmed";
    data.activation.bindings.find((binding) => binding.slot === "acquisition.source").parameters = clone(submitted.bindings[0].parameters);
    data.current().acknowledge("acquisition", submitted, "confirmed");
    if (editedAfterSubmission) form(h).props.onChange({ gain: 31, calibration: [4, 5] });
    finish(old); await loading; await h.settle();
    assert.equal(data.current().request("acquisition").expected_revision, "confirmed");
    assert.equal(form(h).props.value.gain, editedAfterSubmission ? 31 : 12);
  });
});

test("an explicit conflict reload replaces only that module and uses the newly fetched revision", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    assert.equal(elements(h.tree).some((item) => item.type === "button" && text(item) === "Load accepted configuration"), false);
    form(h).props.onChange({ gain: 23, calibration: [4, 5] });
    data.current().setSetting("processing", "ALGORITHM.baseline_duration", 3);
    const source = data.current().get("acquisition.source");
    data.current().set("acquisition.source", { ...source, parameters: { gain: 42, calibration: [1, 2] } });
    await h.settle();
    data.revisions.processing = "remote-first";
    await data.current().reload("processing"); await h.settle();
    assert.equal(form(h).props.value.gain, 23);
    assert.match(text(h.tree), /replaces this module's current edits/);
    data.revisions.processing = "remote-latest";
    data.activation.settings = { "ALGORITHM.baseline_duration": 0.75 };
    data.activation.bindings.find((binding) => binding.slot === "processing.decoder").parameters.gain = 88;
    const filters = data.activation.bindings.filter((binding) => binding.kind === "filter");
    data.activation.bindings = [...data.activation.bindings.filter((binding) => binding.kind !== "filter"), ...filters.reverse()];
    await button(h, "Load accepted configuration").props.onClick(); await h.settle();
    const request = data.current().request("processing");
    assert.equal(request.expected_revision, "remote-latest");
    assert.equal(request.settings["ALGORITHM.baseline_duration"], 0.75);
    assert.equal(request.bindings.find((binding) => binding.slot === "processing.decoder").parameters.gain, 88);
    assert.deepEqual(data.current().filterOrder, filters.map((binding) => binding.slot));
    assert.equal(data.current().get("acquisition.source").parameters.gain, 42);
    assert.equal(elements(h.tree).some((item) => item.type === "button" && text(item) === "Load accepted configuration"), false);
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

test("confirmation refreshes the accepted module after invalidating an already observed revision read", async () => {
  await withProfiles("acquisition", async (h, _, data) => {
    form(h).props.onChange({ gain: 12, calibration: [1, 2] }); await h.settle();
    const submitted = data.current().request("acquisition");
    data.revisions.acquisition = "confirmed";
    data.activation.bindings.find((binding) => binding.slot === "acquisition.source").parameters = clone(submitted.bindings[0].parameters);
    const accepted = data.modules().modules.acquisition;
    const read = api.module;
    let finish;
    let calls = 0;
    api.module = async (module) => module === "acquisition" && ++calls === 1
      ? new Promise((resolve) => { finish = resolve; }) : read(module);
    data.runtime.status.accepted = clone(data.revisions);
    h.updateProps({ runtime: data.runtime }); await h.settle();
    assert.ok(finish, "The accepted revision must already have triggered its read");
    data.current().acknowledge("acquisition", submitted, "confirmed");
    finish(accepted); await h.settle();
    const current = data.current();
    assert.equal(current.modules.acquisition.activation.revision, "confirmed");
    assert.equal(current.modules.acquisition.conflicted, false);
    assert.equal(current.hasChanges("acquisition", current.modules.acquisition.activation), false);
    assert.equal(current.request("acquisition").expected_revision, "confirmed");
  });
});

test("loading a profile with already accepted values reports Loaded without an applied profile badge", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    await change(h, "Load saved profile", "lab-a");
    const loaded = listedProfiles(h).find((item) => text(item).startsWith("Lab A"));
    assert.ok(loaded);
    assert.match(text(loaded), /Loaded/);
    assert.doesNotMatch(text(loaded), /Applied|Selected/);
    assert.equal(elements(h.tree).some((item) => item.props.className === "active-indicator"), false);
    assert.match(text(h.tree), /Loaded profile: Lab A/);
    assert.equal(data.current().hasChanges("acquisition", data.current().modules.acquisition.activation), false);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true });
});

test("restoring loaded profile values clears Modified without saving or applying", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    await change(h, "Load saved profile", "lab-b");
    const original = clone(form(h).props.value);
    form(h).props.onChange({ ...original, gain: 17 }); await h.settle();
    assert.match(text(h.tree), /Loaded profile: Lab B · Modified/);
    form(h).props.onChange(original); await h.settle();
    assert.match(text(h.tree), /Loaded profile: Lab B/);
    assert.doesNotMatch(text(h.tree), /Loaded profile: Lab B · Modified/);
    assert.equal(data.current().get("acquisition.source").profile.profile_id, "lab-b");
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

test("saving a modified profile reports the new saved copy without using it at runtime", async () => {
  await withProfiles("acquisition", async (h, calls, data) => {
    await change(h, "Load saved profile", "lab-b");
    const accepted = clone(data.activation);
    form(h).props.onChange({ gain: 13, calibration: [5, 6] }); await h.settle();
    await saveDraft(h, { name: "Saved draft" });
    assert.match(text(h.tree), /Current values saved as a new profile/);
    assert.match(text(h.tree), /Loaded profile: Saved draft/);
    assert.doesNotMatch(text(h.tree), /Loaded profile: Saved draft · Modified/);
    assert.equal(calls.create.length, 1);
    assert.equal(calls.connect.length, 0);
    assert.deepEqual(data.activation, accepted);
    assert.equal(data.profiles.find((item) => item.profile_id === "lab-b").parameters.gain, 3);
  });
});

for (const [module, slot, profileId] of [
  ["acquisition", "acquisition.source", "lab-b"],
  ["processing", "processing.decoder", "decoder-b"],
]) test(`deleting a loaded ${module} profile retains its current parameters and mapping`, async () => {
  await withProfiles(module, async (h, calls, data) => {
    await change(h, "Load saved profile", profileId);
    const before = clone(data.current().get(slot));
    const request = clone(data.current().request(module));
    const accepted = clone(data.activation);
    const deleted = [];
    api.delete = async (profile) => {
      deleted.push(profile.profile_id);
      data.profiles = data.profiles.filter((item) => item.profile_id !== profile.profile_id);
    };
    await click(h, "Delete profile");
    dialog(h).props.onConfirm(); await h.settle();
    const current = data.current().get(slot);
    assert.deepEqual(deleted, [profileId]);
    assert.equal(current.profile, null);
    assert.equal(current.sourceOrigin, "current");
    assert.deepEqual(current.selection, before.selection);
    assert.deepEqual(current.parameters, before.parameters);
    assert.deepEqual(current.processingInput, before.processingInput);
    assert.deepEqual(data.current().request(module), request);
    assert.equal(control(h, "Load saved profile").props.value, "");
    assert.ok(!options(control(h, "Load saved profile")).some((item) => item.value === profileId));
    assert.doesNotMatch(text(h.tree), /Loaded profile:/);
    assert.equal(button(h, "Save as profile").props.disabled, false);
    assert.deepEqual(data.activation, accepted);
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

test("a loaded filter remains explicitly outside the processing chain until added", async () => {
  await withProfiles("processing", async (h, calls, data) => {
    const before = clone(data.current().request("processing"));
    await click(h, "Filters");
    await change(h, "Load saved profile", "band-b");
    assert.match(text(h.tree), /This filter is not in the processing chain\. Add it before starting processing\./);
    assert.deepEqual(data.current().request("processing"), before);
    assert.equal(data.current().hasChanges("processing", data.current().modules.processing.activation), false);
    await click(h, "Add filter instance");
    const after = data.current().request("processing");
    assert.equal(after.bindings.length, before.bindings.length + 1);
    assert.equal(after.bindings.find((item) => item.slot === data.current().filterOrder.at(-1)).parameters.gain, 7);
    assert.equal(data.current().hasChanges("processing", data.current().modules.processing.activation), true);
    assert.doesNotMatch(text(h.tree), /This filter is not in the processing chain|Filter changes will be used when reading starts/);
    assert.deepEqual(calls, { create: [], connect: [] });
  });
});

for (const [module, start, slot] of [
  ["processing", "Start processing", "processing.decoder"],
  ["stimulation", "Launch stimulation", "stimulation.paradigm"],
  ["actuation", "Start actuation", "actuation.effector"],
]) test(`${module} Setup shows status only and Run starts it with current parameters, no Profile save or acquisition connection`, async () => {
  await withProfiles(module, async (h, calls, data) => {
    const others = clone(data.activation.bindings.filter((item) => !item.slot.startsWith(`${module}.`)));
    assert.ok(!elements(h.tree).some((node) => node.type === "button" && text(node) === start), "Setup must not offer lifecycle actions");
    assert.match(text(h.tree), /Not running/);
    await click(h, "Operate in Run");
    assert.equal(data.openRunCalls, 1);
    if (module === "processing") {
      assert.equal(h.controls.module(module).start.reason !== null, true);
      assert.match(h.controls.module(module).start.reason, /requires an epoch detector/);
      await click(h, "Epoch boundary detector");
      await click(h, "Decoder");
    }
    form(h).props.onChange({ gain: 19, calibration: [3, 4] }); await h.settle();
    assert.equal(h.controls.module(module).start.reason, null);
    await h.controls.module(module).start.run(); await h.settle();
    assert.equal(data.moduleRequests.length, 1);
    const request = data.moduleRequests[0];
    assert.equal(request.module, module);
    assert.ok(request.configuration.bindings.every((item) => item.slot.startsWith(`${module}.`)));
    assert.equal(request.configuration.bindings.find((item) => item.slot === slot).parameters.gain, 19);
    assert.deepEqual(calls, { create: [], connect: [] });
    assert.deepEqual(data.activation.bindings.filter((item) => !item.slot.startsWith(`${module}.`)), others);
    assert.equal(form(h).props.value.gain, 19);
    assert.equal(data.current().hasChanges(module, data.current().modules[module].activation), false);
  }, { engineRunning: true, configure: (data) => { data.source.connected = false; } });
});

test("invalid current parameters edited in Setup block module startup locally without requiring a Profile", async () => {
  await withProfiles("actuation", async (h, calls, data) => {
    form(h).props.onChange({ gain: 0, calibration: [3, 4] }); await h.settle();
    const start = h.controls.module("actuation").start;
    assert.notEqual(start.reason, null);
    assert.deepEqual(data.moduleRequests, []);
    assert.deepEqual(calls, { create: [], connect: [] });
  }, { engineRunning: true });
});
