/** Exercise profile browsing and explicit activation through component events. */
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
const { connectSource } = await server.ssrLoadModule("/src/source-lifecycle.ts");

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
const definition = (kind, choice, label) => ({ kind, selection: choice, label, available: true, schema: clone(schema), controls: {} });
const profile = (id, kind, choice, name, gain) => ({ profile_id: id, kind, selection: choice, name, parameters: { gain, calibration: [3, 4] }, revision: 2 });

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
    profile("trigger-a", "trigger", trigger, "Hardware trigger", 1),
    profile("paradigm-a", "paradigm", paradigm, "Visual stimulation", 1),
  ];
  const bind = (slot, id) => {
    const saved = profiles.find((item) => item.profile_id === id);
    return { slot, profile_id: id, profile_revision: saved.revision, kind: saved.kind, selection: clone(saved.selection), parameters: clone(saved.parameters), ...(saved.processing_input ? { processing_input: clone(saved.processing_input) } : {}) };
  };
  return { profiles, definitions: [
    definition("acquisition", scanV1, "Scan45"), definition("acquisition", scanV2, "Scan45"),
    definition("acquisition", replay, "Replay"), definition("acquisition", unsavedPlugin, "New source"),
    definition("filter", bandpass, "Bandpass"), definition("filter", notch, "Notch"),
    definition("filter", emptyFilter, "New filter"), { ...definition("decoder", decoder, "Decoder"), channel_requirements: clone(channelRequirements) },
    definition("trigger", trigger, "Hardware trigger"), definition("paradigm", paradigm, "Visual stimulation"),
  ], activation: { revision: "accepted-revision", bindings: [bind("acquisition.source", "lab-a"),
    bind("processing.filter.first", "band-a"), bind("processing.filter.second", "notch-a"), bind("processing.decoder", "decoder-a")] } };
}

const sameDependencies = (left, right) => !!left && !!right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));

/** A persistent dispatcher runs this component's hooks without a browser or new test dependencies. */
function componentHarness(props) {
  const hooks = [];
  let cursor = 0;
  let needsRender = true;
  let tree;
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
    try { tree = PluginProfiles(props); } finally { internals.current = previous; }
    const effects = pendingEffects; pendingEffects = [];
    effects.forEach((effect) => effect());
  }
  return {
    get tree() { return tree; },
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
  return [tree, ...elements(tree.props.children)];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree === null || tree === undefined || typeof tree === "boolean" ? "" : String(tree);
}
function button(harness, label, scope = harness.tree) {
  const result = elements(scope).find((item) => item.type === "button" && text(item) === label)
    ?? (label === "New profile" ? elements(scope).find((item) => item.type === "button" && text(item) === "New configuration") : undefined);
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

test("profile search keeps a hidden draft open and searches only the selected plugin version", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    form(harness).props.onChange({ gain: 19, calibration: [3, 4] }); await harness.settle();
    await change(harness, "Search saved profiles", "  LAB B  ", "input");
    assert.deepEqual(listedProfiles(harness).map(text), ["Lab B"]);
    assert.match(text(harness.tree), /Showing 1 of 2 profiles/);
    assert.match(text(harness.tree), /The open profile is hidden by this search/);
    assert.equal(form(harness).props.value.gain, 19);
    assert.equal(control(harness, "Profile").props.value, "lab-a");
    assert.equal(dialog(harness), undefined);
    await click(harness, "Clear search");
    assert.equal(listedProfiles(harness).length, 2);
    assert.doesNotMatch(text(harness.tree), /The open profile is hidden by this search/);
    await change(harness, "Search saved profiles", "lab-a", "input");
    assert.equal(listedProfiles(harness).length, 1);
    assert.equal(listedProfiles(harness)[0].props["aria-current"], "true");
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("profile lists distinguish no search matches from no saved profiles", async () => {
  await withProfiles("acquisition", async (harness) => {
    await change(harness, "Search saved profiles", "Version 2", "input");
    assert.equal(listedProfiles(harness).length, 0);
    assert.match(text(harness.tree), /No profiles match this search/);
    await change(harness, "Search saved profiles", "   ", "input");
    assert.equal(listedProfiles(harness).length, 2);
    await change(harness, "Plugin and version", selectionKey(unsavedPlugin));
    assert.match(text(harness.tree), /No saved profiles for this plugin version/);
    assert.doesNotMatch(text(harness.tree), /No profiles match this search/);
  });
});

async function withProfiles(module, action, { engineRunning = false, streaming = false, applySucceeds = true, configure } = {}) {
  const originalApi = { ...api };
  const originalControlApi = { ...controlApi };
  const originalRuntimeApi = { ...runtimeConfigApi };
  const data = fixture();
  data.source = { connected: engineRunning, streaming, connection_id: engineRunning ? "connection-a" : null };
  data.metadata = { source_id: "scan45", connection_id: "connection-a",
    metadata: "Source device\nProtocol: 1.2\nExtensions: events", descriptor: null };
  data.events = [];
  data.profileKinds = [];
  configure?.(data);
  const runtimeStatus = { recovery_required: false, operation: null, running: engineRunning, accepted_revision: data.activation?.revision ?? null,
    applied_revision: engineRunning ? data.activation?.revision ?? null : null, generation: engineRunning ? "generation-a" : null };
  const calls = { create: [], update: [], apply: [], review: [] };
  api.catalog = async (kind) => clone(data.definitions.filter((item) => item.kind === kind));
  api.profiles = async (kind) => {
    data.profileKinds.push(kind);
    return clone(data.profiles.filter((item) => kind === undefined || item.kind === kind));
  };
  api.activation = async () => clone(data.activation);
  api.create = async (kind, choice, name, parameters, processing_input) => {
    const input = processing_input === undefined ? {} : { processing_input: clone(processing_input) };
    calls.create.push(clone({ kind, selection: choice, name, parameters, ...input }));
    const saved = { profile_id: `created-${calls.create.length}`, kind, selection: clone(choice), name, parameters: clone(parameters), revision: 1, ...input };
    data.profiles.push(saved); return clone(saved);
  };
  api.update = async (saved, name, parameters, processing_input) => {
    const input = processing_input === undefined ? {} : { processing_input: clone(processing_input) };
    calls.update.push(clone({ profile: saved, name, parameters, ...input }));
    const updated = { ...saved, name, parameters: clone(parameters), revision: saved.revision + 1, ...input };
    data.profiles = data.profiles.map((item) => item.profile_id === saved.profile_id ? updated : item); return clone(updated);
  };
  controlApi.acquisitionStatus = async () => clone(data.source);
  controlApi.engineStart = async () => { data.events.push("start"); runtimeStatus.running = true; runtimeStatus.applied_revision = data.activation.revision; runtimeStatus.generation = "generation-a"; };
  controlApi.acqConnect = async () => { data.events.push("connect"); data.source.connected = true; data.source.connection_id = "connection-a"; };
  controlApi.acqDisconnect = async () => { data.events.push("disconnect"); data.source.connected = false; data.source.connection_id = null; };
  controlApi.acqMetadata = async () => { data.events.push("metadata"); return clone(data.metadata); };
  runtimeConfigApi.status = async () => runtimeStatus;
  data.reviewDrafts = [];
  const runtime = {
    blocked: false, status: runtimeStatus,
    review: async (body, draft) => {
      data.reviewDrafts.push(clone(draft));
      const unchanged = draft?.profile && JSON.stringify(draft.parameters) === JSON.stringify(draft.profile.parameters);
      runtime.profileReview = draft ? { draft: clone(draft), mode: unchanged ? "existing" : "create",
        name: draft.profile && !unchanged ? `${draft.profile.name} copy` : draft.name || "New source",
        savedProfile: null, saveUncertain: false } : null;
      calls.review.push(clone(body));
      runtime.plan = { plan_id: `plan-${calls.review.length}`, expected_revision: body.expected_revision,
        target_revision: `accepted-${calls.review.length}`, errors: [], strategy: "select_on_start",
        changed_slots: body.refresh_slots, changed_settings: [], module: body.module };
      return clone(runtime.plan);
    },
    dismissPlan: () => { runtime.plan = null; runtime.profileReview = null; },
    setProfileReviewName: (name) => { runtime.profileReview.name = name; },
    setProfileReviewMode: (mode) => { runtime.profileReview.mode = mode;
      if (mode === "update") runtime.profileReview.name = runtime.profileReview.draft.profile.name; },
    apply: async (...args) => { calls.apply.push(args); },
    applySelection: async (body) => {
      data.events.push("apply");
      calls.apply.push(clone(body));
      if (!applySucceeds) return false;
      const retained = body.module ? (data.activation?.bindings ?? []).filter((item) => !item.slot.startsWith(`${body.module}.`)) : [];
      data.activation ??= { revision: null, bindings: [] };
      data.activation.bindings = [...retained, ...body.bindings.map((item) => {
        if (!item.profile_id) return { slot: item.slot, kind: "acquisition", selection: clone(item.selection),
          parameters: clone(item.parameters), profile_id: null, profile_revision: null };
        const profile = data.profiles.find((profile) => profile.profile_id === item.profile_id);
        return { ...clone(profile), slot: item.slot, profile_revision: profile.revision };
      })];
      return true;
    },
    refresh: async () => {},
  };
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
  acquisition.connect = command(connectSource);
  acquisition.disconnect = command(() => controlApi.acqDisconnect());
  acquisition.read = command(() => controlApi.acqStreamStart());
  acquisition.stopRead = command(() => controlApi.acqStreamStop());
  data.acquisition = acquisition;
  data.dirty = false;
  data.openRunStates = [];
  const props = { module, engineRunning, acquisition, runtime,
    onDirtyChange: (dirty) => { data.dirty = dirty; }, onRun: () => { data.openRunStates.push(data.dirty); } };
  harness = componentHarness(props);
  data.remount = async () => {
    harness.dispose();
    harness = componentHarness({ ...props, engineRunning: runtime.status.running });
    await harness.settle();
    return harness;
  };
  try {
    await harness.settle();
    await action(harness, calls, data);
  }
  finally { harness.dispose(); Object.assign(api, originalApi); Object.assign(controlApi, originalControlApi); Object.assign(runtimeConfigApi, originalRuntimeApi); }
}

async function acceptReviewed(harness, calls, data, { revision, request = calls.review.at(-1), plan = data.runtime.plan } = {}) {
  const profileReview = data.runtime.profileReview;
  let saved = profileReview?.savedProfile;
  if (profileReview && !saved) {
    const { draft, mode, name } = profileReview;
    saved = mode === "existing" ? draft.profile : mode === "update"
      ? await api.update(draft.profile, name, draft.parameters)
      : await api.create(draft.kind, draft.selection, name, draft.parameters);
    profileReview.savedProfile = saved;
    data.runtime.savedProfile = { reviewed_plan_id: plan.plan_id, profile: clone(saved) };
    harness.updateProps({ runtime: data.runtime }); await harness.settle();
  }
  const appliedRequest = saved ? { ...request, bindings: [{ slot: "acquisition.source", profile_id: saved.profile_id }] } : request;
  const applied = await data.runtime.applySelection(appliedRequest);
  const targetRevision = revision ?? plan.target_revision;
  data.runtime.operation = { operation_id: `operation-${calls.apply.length}`, plan_id: saved ? `${plan.plan_id}-saved` : plan.plan_id,
    ...(saved ? { reviewed_plan_id: plan.plan_id, profile: clone(saved) } : {}), target_revision: targetRevision, status: applied ? "succeeded" : "failed" };
  if (applied) {
    data.activation.revision = targetRevision;
    data.runtime.status.accepted_revision = targetRevision;
    data.runtime.status.applied_revision = data.runtime.status.running ? targetRevision : null;
  }
  data.runtime.status.operation = data.runtime.operation;
  if (applied) data.runtime.completedOperation = clone(data.runtime.operation);
  data.runtime.plan = null; data.runtime.profileReview = null;
  harness.updateProps({ runtime: data.runtime });
  await harness.settle();
}

async function applyReviewed(harness, calls, data, { mode, name } = {}) {
  await click(harness, "Review and apply configuration");
  if (mode) data.runtime.setProfileReviewMode(mode);
  if (name) data.runtime.setProfileReviewName(name);
  await acceptReviewed(harness, calls, data);
}

test("the profile selector is scoped to the selected plugin and exact version", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
    assert.equal(control(harness, "Profile").props.value, "lab-a");
    assert.deepEqual(options(control(harness, "Profile")).map((item) => item.value), ["lab-b", "lab-a"]);
    assert.equal(!!control(harness, "Plugin and version").props.disabled, false);
    await change(harness, "Plugin and version", selectionKey(scanV2));
    assert.equal(control(harness, "Profile").props.value, "lab-v2");
    assert.deepEqual(options(control(harness, "Profile")).map((item) => item.value), ["lab-v2"]);
    assert.equal(text(harness.tree).includes("Lab B"), false);
    await change(harness, "Plugin and version", selectionKey(scanV1));
    assert.equal(control(harness, "Profile").props.value, "lab-a", "Return to the bound profile before the first saved profile");
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

for (const modifiers of [{ ctrlKey: true }, { ctrlKey: false, metaKey: true, key: "S" }]) {
  test(`the profile save shortcut saves only the draft (${JSON.stringify(modifiers)})`, async () => {
    await withProfiles("processing", async (harness, calls) => {
      await change(harness, "Name", "Keyboard edit", "input");
      assert.equal(await saveShortcut(harness, modifiers), true);
      assert.equal(calls.update.length, 1);
      assert.equal(calls.update[0].name, "Keyboard edit");
      assert.equal(calls.review.length, 0);
      assert.equal(calls.apply.length, 0);
    });
  });
}

test("save shortcuts respect validation, dialogs, key repeats and composition", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await change(harness, "Name", "", "input");
    await saveShortcut(harness);
    assert.equal(calls.update.length, 0);
    await change(harness, "Name", "Keep this draft", "input");
    await saveShortcut(harness, { repeat: true });
    await saveShortcut(harness, { nativeEvent: { isComposing: true } });
    await saveShortcut(harness, { defaultPrevented: true });
    assert.equal(await saveShortcut(harness, { shiftKey: true }), false);
    await click(harness, "New profile");
    assert.ok(dialog(harness));
    await saveShortcut(harness);
    assert.equal(calls.update.length, 0);
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(control(harness, "Name", "input").props.value, "Keep this draft");
  });
});

test("keyboard saving restores the edited field after busy controls are enabled again", async () => {
  const originalDocument = globalThis.document;
  let restored = 0;
  globalThis.document = { activeElement: { isConnected: true, getClientRects: () => [{}], focus: () => { restored += 1; } } };
  try {
    await withProfiles("processing", async (harness) => {
      await change(harness, "Name", "Continue editing here", "input");
      await saveShortcut(harness);
      assert.equal(restored, 1);
      assert.equal(control(harness, "Name", "input").props.disabled, false);
    });
  } finally { if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; }
});

test("processing stage navigation retains the decoder draft and guards cross-profile changes", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await change(harness, "Name", "Unfinished decoder", "input");
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Channel mapping");
    assert.equal(dialog(harness), undefined);
    assert.equal(control(harness, "Name", "input").props.value, "Unfinished decoder");
    assert.deepEqual(input(harness).props.value, decoderInput(["C4"]));
    await click(harness, "Epoching");
    assert.ok(dialog(harness));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.deepEqual(input(harness).props.value, decoderInput(["C4"]));
    await click(harness, "Epoching");
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(input(harness), undefined);
    assert.equal(calls.create.length + calls.update.length + calls.review.length + calls.apply.length, 0);
  });
});

test("processing anchors align the focused section at the top and library expansion does not select a profile", async () => {
  const originalDocument = globalThis.document;
  const focused = [], scrolled = [];
  globalThis.document = { getElementById: (id) => ({
    focus(options) { focused.push([id, options]); }, scrollIntoView(options) { scrolled.push([id, options]); },
  }) };
  try {
    await withProfiles("processing", async (harness, calls) => {
      const toggle = button(harness, "Profiles & tools +");
      assert.equal(toggle.props["aria-expanded"], false);
      toggle.props.onClick(); await harness.settle();
      assert.equal(button(harness, "Profiles & tools −").props["aria-expanded"], true);
      await click(harness, "Channel mapping");
      assert.deepEqual(focused.at(-1), ["processing-channel-mapping", { preventScroll: true }]);
      assert.deepEqual(scrolled.at(-1), ["processing-channel-mapping", { block: "start" }]);
      let finishCatalog;
      api.catalog = () => new Promise((resolve) => { finishCatalog = resolve; });
      const beforeLoad = focused.length;
      await click(harness, "Epoching");
      assert.equal(focused.length, beforeLoad, "Keep pending focus until the target editor finishes loading");
      finishCatalog([]); await harness.settle();
      assert.equal(focused.at(-1)[0], "processing-parameters");
      assert.equal(calls.apply.length, 0);
      assert.equal(calls.review.length, 0);
    });
  } finally { if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; }
});

test("acquisition shows the plugin selector before its profiles on entry", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const selectors = elements(harness.tree).filter((item) => item.type === "select");
    assert.equal(selectors[0], control(harness, "Plugin and version"));
    assert.equal(selectors[1], control(harness, "Profile"));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
    assert.equal(control(harness, "Profile").props.value, "lab-a");
    assert.equal(!!button(harness, "New profile").props.disabled, false);
    assert.equal(form(harness).props.value.gain, 2);
    assert.deepEqual(data.events, []);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("acquisition keeps one review and apply action visible outside collapsed profile details", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const heading = elements(harness.tree).find((item) => item.type === "header" && item.props.className === "editor-heading");
    const action = button(harness, "Review and apply configuration", heading);
    assert.equal(!!action.props.disabled, false);
    assert.equal(elements(harness.tree).filter((item) => item.type === "button" && text(item) === "Review and apply configuration").length, 1);
    assert.equal(elements(harness.tree).filter((item) => item.type === "details").some((item) => elements(item).includes(action)), false);
    await click(harness, "New profile");
    assert.equal(!!button(harness, "Review and apply configuration").props.disabled, false, "A valid unnamed draft can be reviewed without a profile");
    assert.deepEqual(data.events, []);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("switching plugins from a new acquisition draft selects the target plugin's own profile", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "New profile");
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
    assert.equal(control(harness, "Profile").props.value, "");
    assert.deepEqual(options(control(harness, "Profile")).map((item) => item.value), ["", "lab-b", "lab-a"]);
    assert.equal(elements(harness.tree).some((item) => item.type === "input" && item.props.maxLength === 64), false);
    assert.equal(form(harness).props.value.gain, 1);
    await change(harness, "Plugin and version", selectionKey(scanV2));
    assert.equal(dialog(harness), undefined);
    assert.equal(control(harness, "Profile").props.value, "lab-v2");
    assert.deepEqual(options(control(harness, "Profile")).map((item) => item.value), ["lab-v2"]);
    assert.equal(control(harness, "Profile").props.value, "lab-v2");
    assert.equal(form(harness).props.value.gain, 4);
    await change(harness, "Plugin and version", selectionKey(scanV1));
    assert.equal(control(harness, "Profile").props.value, "lab-a");
    assert.equal(form(harness).props.value.gain, 2);
    assert.deepEqual(data.events, []);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("creating or selecting acquisition profiles requires discarding unsaved parameters", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    form(harness).props.onChange({ gain: 9, calibration: [3, 4] }); await harness.settle();
    await click(harness, "New configuration");
    assert.ok(dialog(harness));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(form(harness).props.value.gain, 9);
    await click(harness, "New configuration");
    dialog(harness).props.onConfirm(""); await harness.settle();
    form(harness).props.onChange({ gain: 11, calibration: [1, 2] }); await harness.settle();
    await change(harness, "Profile", "lab-a");
    assert.ok(dialog(harness));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(form(harness).props.value.gain, 11);
    await change(harness, "Profile", "lab-a");
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(form(harness).props.value.gain, 2);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

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
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
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
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { engineRunning: true });
});

for (const [label, action] of [
  ["profile", async (harness) => change(harness, "Profile", "lab-b")],
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
      assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    }, { engineRunning: true });
  });
}

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
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { engineRunning: true });
});


test("changed source parameters require final Review saving and Apply before connecting", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const otherBindings = clone(data.activation.bindings.filter((item) => item.kind !== "acquisition"));
    form(harness).props.onChange({ gain: 9, calibration: [3, 4] }); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(!!button(harness, "Review and apply configuration").props.disabled, false);
    assert.deepEqual(data.events, []);
    await applyReviewed(harness, calls, data);
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["apply", "start", "connect"]);
    assert.deepEqual(calls.apply[0].refresh_slots, ["acquisition.source"]);
    assert.equal(calls.apply[0].module, "acquisition");
    assert.equal(calls.apply[0].bindings.length, 1);
    assert.deepEqual(data.activation.bindings.filter((item) => item.kind !== "acquisition"), otherBindings);
    assert.equal(metadata(harness).props.value, null);
    assert.deepEqual(calls.review[0].bindings, [{ slot: "acquisition.source", selection: scanV1,
      parameters: { gain: 9, calibration: [3, 4] } }]);
    assert.equal(calls.update.length, 0);
    await click(harness, "Disconnect");
    assert.equal(metadata(harness).props.canRead, false);
  });
});

test("Acquisition connects without loading or submitting unrelated processing profiles", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.equal(calls.apply.length, 0);
    assert.ok(data.profileKinds.every((kind) => kind === "acquisition"));
    assert.deepEqual(data.activation, accepted);
  }, { configure: (data) => {
    const decoder = data.activation.bindings.find((item) => item.slot === "processing.decoder");
    decoder.profile_id = null;
    decoder.processing_input = null;
    decoder.processing_input_requires_review = true;
  } });
});

test("a new plugin draft cannot connect or write a profile before explicit reviewed Apply", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "New profile");
    await change(harness, "Plugin and version", selectionKey(unsavedPlugin));
    const prepare = button(harness, "Connect");
    assert.equal(prepare.props.disabled, true);
    prepare.props.onClick(); await harness.settle();
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    assert.deepEqual(data.events, []);
    assert.equal(elements(harness.tree).some((item) => item.type === "input" && item.props.maxLength === 64), false);
  });
});

test("an unsuccessful explicit apply keeps Connect unavailable for the changed profile", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Profile", "lab-b");
    await click(harness, "Review and apply configuration");
    assert.equal(await data.runtime.applySelection(calls.review[0]), false);
    await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.deepEqual(data.events, ["apply"]);
    assert.equal(metadata(harness).props.canRead, false);
  }, { applySucceeds: false });
});

test("Connect refuses a startup revision changed by another client", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    runtimeConfigApi.status = async () => ({ ...data.runtime.status, accepted_revision: "another-selection", applied_revision: "another-selection" });
    await click(harness, "Connect");
    assert.deepEqual(data.events, []);
    assert.match(text(harness.tree), /configuration changed/);
  });
});

test("metadata cannot read a disconnected, streaming, different or edited source", async () => {
  await withProfiles("acquisition", async (harness) => {
    assert.equal(metadata(harness).props.canRead, true);
    await change(harness, "Profile", "lab-b");
    assert.equal(metadata(harness).props.canRead, false);
    await change(harness, "Profile", "lab-a");
    form(harness).props.onChange({ gain: 8, calibration: [3, 4] }); await harness.settle();
    assert.equal(metadata(harness).props.canRead, false);
  }, { engineRunning: true });
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(metadata(harness).props.canRead, false);
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(calls.update.length, 0);
    assert.deepEqual(data.events, []);
  }, { engineRunning: true, streaming: true });
});

test("browsing profiles and a plugin without profiles never selects a runtime binding", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    await change(harness, "Profile", "lab-b");
    assert.equal(form(harness).props.value.gain, 3);
    await change(harness, "Plugin and version", selectionKey(unsavedPlugin));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(unsavedPlugin));
    assert.equal(control(harness, "Profile").props.value, "");
    assert.deepEqual(options(control(harness, "Profile")).map((item) => item.value), [""]);
    assert.deepEqual(form(harness).props.value, { gain: 1, calibration: [1, 2] });
    assert.equal(!!button(harness, "Review and apply configuration").props.disabled, false);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("New configuration uses current plugin defaults and final Review names the saved profile", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Plugin and version", selectionKey(scanV2));
    await click(harness, "New configuration");
    assert.deepEqual(form(harness).props.value, { gain: 1, calibration: [1, 2] });
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    await applyReviewed(harness, calls, data, { name: "New version 2 lab" });
    assert.deepEqual(calls.create, [{ kind: "acquisition", selection: scanV2, name: "New version 2 lab", parameters: { gain: 1, calibration: [1, 2] } }]);
    assert.equal(calls.update.length, 0);
    assert.equal(control(harness, "Profile").props.value, "created-1");
    assert.equal(data.dirty, false);
  });
});

test("changed acquisition parameters default to a separate profile while preserving the original", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const original = clone(data.profiles.find((item) => item.profile_id === "lab-a"));
    form(harness).props.onChange({ gain: 9, calibration: [10, 11] }); await harness.settle();
    await click(harness, "Review and apply configuration");
    assert.equal(data.runtime.profileReview.mode, "create");
    assert.equal(calls.create.length, 0);
    data.runtime.dismissPlan();
    assert.deepEqual(form(harness).props.value, { gain: 9, calibration: [10, 11] });
    await applyReviewed(harness, calls, data, { name: "Lab A alternative" });
    assert.deepEqual(calls.create, [{ kind: "acquisition", selection: scanV1, name: "Lab A alternative", parameters: { gain: 9, calibration: [10, 11] } }]);
    assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a"), original);
    assert.equal(calls.update.length, 0);
    assert.equal(control(harness, "Profile").props.value, "created-1");
  });
});

test("plugin changes require an explicit discard when the edited profile is dirty", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    form(harness).props.onChange({ gain: 11, calibration: [3, 4] }); await harness.settle();
    await change(harness, "Plugin and version", selectionKey(scanV2));
    assert.ok(dialog(harness));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(dialog(harness), undefined);
    assert.equal(form(harness).props.value.gain, 11);
    await change(harness, "Plugin and version", selectionKey(scanV2));
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV2));
    assert.equal(control(harness, "Profile").props.value, "lab-v2");
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("selecting no trigger reviews only trigger removal and preserves an inline acquisition source", async () => {
  await withProfiles("stimulation", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    await click(harness, "Trigger");
    assert.deepEqual(options(control(harness, "Plugin and version"))[0], { value: "", label: "None (no trigger output)" });
    await change(harness, "Plugin and version", "");
    assert.equal(form(harness), undefined);
    assert.equal(elements(harness.tree).some((item) => item.type === "button" && text(item) === "Save profile"), false);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    assert.deepEqual(data.activation, accepted, "Browsing None must not change the accepted trigger");
    await click(harness, "Review and apply profile");
    assert.deepEqual(calls.review, [{
      bindings: accepted.bindings.filter((item) => item.slot === "stimulation.paradigm")
        .map((item) => ({ slot: item.slot, profile_id: item.profile_id })),
      module: "stimulation", expected_revision: accepted.revision, refresh_slots: [], include_host_settings: false,
    }]);
    assert.equal(calls.create.length, 0); assert.equal(calls.update.length, 0); assert.equal(calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted, "Review still requires explicit application");
  }, { configure: (data) => {
    for (const [slot, profileId] of [["stimulation.trigger", "trigger-a"], ["stimulation.paradigm", "paradigm-a"]]) {
      const saved = data.profiles.find((item) => item.profile_id === profileId);
      data.activation.bindings.push({ ...clone(saved), slot, profile_revision: saved.revision });
    }
    const source = data.activation.bindings.find((item) => item.slot === "acquisition.source");
    source.profile_id = null; source.profile_revision = null;
    source.parameters = { gain: 8, calibration: [5, 6] };
  } });
});

test("an unbound trigger opens as None and can browse a saved hardware profile", async () => {
  await withProfiles("stimulation", async (harness, calls) => {
    await click(harness, "Trigger");
    assert.equal(control(harness, "Plugin and version").props.value, "");
    assert.equal(form(harness), undefined);
    await change(harness, "Plugin and version", selectionKey(trigger));
    assert.equal(control(harness, "Profile").props.value, "trigger-a");
    assert.equal(control(harness, "Name", "input").props.value, "Hardware trigger");
    await change(harness, "Plugin and version", "");
    assert.equal(control(harness, "Plugin and version").props.value, "");
    assert.equal(form(harness), undefined);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("selecting no trigger asks before discarding an edited hardware profile", async () => {
  await withProfiles("stimulation", async (harness, calls) => {
    await click(harness, "Trigger");
    await change(harness, "Plugin and version", selectionKey(trigger));
    await change(harness, "Name", "Unsaved trigger name", "input");
    await change(harness, "Plugin and version", "");
    assert.ok(dialog(harness));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(trigger));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(control(harness, "Name", "input").props.value, "Unsaved trigger name");
    await change(harness, "Plugin and version", "");
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(control(harness, "Plugin and version").props.value, "");
    assert.equal(form(harness), undefined);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("required plugin slots do not offer the no-trigger option", async () => {
  for (const module of ["acquisition", "processing", "stimulation"]) {
    await withProfiles(module, async (harness) => {
      assert.equal(options(control(harness, "Plugin and version")).some((item) => item.value === ""), false);
    });
  }
});

test("reviewing changed parameters preserves the old activation and profile until confirmation", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const accepted = clone(data.activation), original = clone(data.profiles);
    form(harness).props.onChange({ gain: 12, calibration: [3, 4] }); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    await click(harness, "Review and apply configuration");
    assert.equal(calls.create.length + calls.update.length + calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted);
    assert.deepEqual(data.profiles, original);
    assert.deepEqual(data.reviewDrafts[0].parameters, { gain: 12, calibration: [3, 4] });
    assert.equal(data.reviewDrafts[0].profile.profile_id, "lab-a");
    assert.equal(data.runtime.profileReview.mode, "create");
    assert.deepEqual(calls.review[0].refresh_slots, ["acquisition.source"]);
    assert.equal(calls.review[0].expected_revision, accepted.revision);
  });
});

test("profile save failures preserve edits and keep review unavailable", async () => {
  await withProfiles("processing", async (harness, calls) => {
    api.update = async () => { throw new Error("Profile changed in another console"); };
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Save profile");
    assert.match(text(harness.tree), /Profile changed in another console/);
    assert.match(text(harness.tree), /Unsaved changes/);
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["C4"]).outputs);
    assert.equal(button(harness, "Review and apply profile").props.disabled, true);
    assert.equal(button(harness, "Save profile").props.disabled, false);
    assert.equal(calls.apply.length, 0);
  });
});

test("profile saving preserves and distinguishes unapplied filter order", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await click(harness, "Filters");
    await click(harness, "Move down", filterRows(harness)[0]);
    await change(harness, "Name", "Renamed filter", "input");
    await click(harness, "Save profile");
    assert.match(text(harness.tree), /Profile saved/);
    assert.match(text(harness.tree), /Filter order not applied/);
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    assert.equal(calls.review.length, 0);
    assert.equal(calls.apply.length, 0);
  });
});

test("new acquisition drafts defer naming and saving to final Review", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "New configuration");
    assert.equal(elements(harness.tree).some((item) => item.type === "button" && text(item) === "Save profile"), false);
    assert.equal(!!button(harness, "Review and apply configuration").props.disabled, false);
    assert.equal(button(harness, "Connect").props.disabled, true);
    await click(harness, "Review and apply configuration");
    assert.ok(data.reviewDrafts[0].name.trim());
    assert.equal(data.reviewDrafts[0].profile, null);
    assert.equal(calls.create.length, 0);
  });
});

test("filter instances select a plugin and its profiles while retaining stable order", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await click(harness, "Filters");
    assert.equal(control(harness, "Filter 1 plugin").props.value, selectionKey(bandpass));
    assert.deepEqual(options(control(harness, "Filter 1 profile")).map((item) => item.value), ["band-a", "band-b"]);
    await change(harness, "Filter 1 profile", "band-b");
    await click(harness, "Move down", filterRows(harness)[0]);
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    assert.equal(control(harness, "Filter 2 profile").props.value, "band-b");
    await change(harness, "Plugin and version", selectionKey(notch));
    assert.equal(dialog(harness), undefined, "Browsing an editor must retain pending filter chain changes");
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    assert.equal(control(harness, "Filter 2 profile").props.value, "band-b");
    await click(harness, "Review and apply filter chain");
    assert.deepEqual(calls.review[0].bindings.filter((item) => item.slot.startsWith("processing.filter.")), [
      { slot: "processing.filter.second", profile_id: "notch-a" },
      { slot: "processing.filter.first", profile_id: "band-b" },
    ]);
    assert.equal(calls.apply.length, 0);
  });
});

for (const target of ["decoder", "filter chain"]) {
  test(`reviewing a ${target} preserves an active inline acquisition source`, async () => {
    await withProfiles("processing", async (harness, calls, data) => {
      const accepted = clone(data.activation);
      if (target === "decoder") {
        await change(harness, "Profile", "decoder-b");
        await click(harness, "Review and apply profile");
      } else {
        await click(harness, "Filters");
        await change(harness, "Filter 1 profile", "band-b");
        await click(harness, "Review and apply filter chain");
      }
      assert.equal(calls.review.length, 1);
      assert.equal(calls.review[0].module, "processing");
      assert.ok(calls.review[0].bindings.every((item) => item.slot.startsWith("processing.")));
      const changedSlot = target === "decoder" ? "processing.decoder" : "processing.filter.first";
      assert.deepEqual(calls.review[0].bindings.find((item) => item.slot === changedSlot), {
        slot: changedSlot, profile_id: target === "decoder" ? "decoder-b" : "band-b",
      });
      assert.equal(calls.review[0].bindings.length, accepted.bindings.filter((item) => item.slot.startsWith("processing.")).length);
      assert.deepEqual(data.activation, accepted, "Review does not save or apply the inline source");
      assert.equal(calls.create.length, 0);
      assert.equal(calls.update.length, 0);
      assert.equal(calls.apply.length, 0);
    }, { configure: (data) => {
      const source = data.activation.bindings.find((item) => item.slot === "acquisition.source");
      source.profile_id = null;
      source.profile_revision = null;
      source.parameters = { gain: 8, calibration: [5, 6] };
    } });
  });
}

test("a filter plugin without a profile cannot be applied until a saved profile is chosen", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await click(harness, "Filters");
    await change(harness, "Filter 1 plugin", selectionKey(emptyFilter));
    assert.equal(control(harness, "Filter 1 profile").props.value, "");
    assert.equal(!!button(harness, "Review and apply filter chain").props.disabled, true);
    await change(harness, "Filter 1 plugin", selectionKey(notch));
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    assert.equal(!!button(harness, "Review and apply filter chain").props.disabled, false);
    assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
  });
});

test("discarding an editor change preserves pending filter order, while Discard resets both", async () => {
  await withProfiles("processing", async (harness, calls) => {
    await click(harness, "Filters");
    await click(harness, "Move down", filterRows(harness)[0]);
    await change(harness, "Name", "Unsaved filter name", "input");
    await change(harness, "Plugin and version", selectionKey(notch));
    assert.ok(dialog(harness));
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(notch));
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    assert.equal(control(harness, "Filter 2 profile").props.value, "band-a");
    await click(harness, "Discard");
    assert.equal(dialog(harness).props.cancelLabel, "Keep editing");
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(control(harness, "Filter 1 profile").props.value, "notch-a");
    await click(harness, "Discard");
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(control(harness, "Filter 1 profile").props.value, "band-a");
    assert.equal(control(harness, "Filter 2 profile").props.value, "notch-a");
    assert.equal(!!button(harness, "Review and apply filter chain").props.disabled, true);
    assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
  });
});


test("decoder profiles retain independent ordered channels and wait for reviewed activation", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["C3", "C4"]).outputs);
    assert.deepEqual(input(harness).props.decoderSelection, decoder);
    assert.deepEqual(input(harness).props.channelRequirements, channelRequirements);
    assert.equal(metadata(harness), undefined, "Source metadata belongs on the acquisition page");
    const updatedInput = decoderInput(["C4", "C3"]);
    updatedInput.outputs[0].signal_type = "eeg";
    input(harness).props.onChange(updatedInput); await harness.settle();
    assert.equal(button(harness, "Review and apply profile").props.disabled, true);
    await click(harness, "Save profile");
    assert.deepEqual(calls.update[0].processing_input, updatedInput);
    assert.deepEqual(data.activation, accepted);
    assert.match(text(harness.tree), /saved changes that are not selected/);
    await change(harness, "Profile", "decoder-b");
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["P3", "P4"]).outputs);
    await change(harness, "Profile", "decoder-a");
    assert.deepEqual(input(harness).props.value, updatedInput);
    await click(harness, "Review and apply profile");
    assert.deepEqual(calls.review[0].refresh_slots, ["processing.decoder"]);
    assert.equal(calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted);
  });
});

test("new decoder profiles start with all channels until descriptor IDs are available", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    data.profiles.find((item) => item.profile_id === "decoder-a").processing_input.outputs = [];
    await click(harness, "Reload");
    assert.deepEqual(input(harness).props.value.outputs, []);
    await click(harness, "New profile");
    assert.deepEqual(input(harness).props.value, decoderInput([]));
    await change(harness, "Name", "Identity mapping", "input");
    await click(harness, "Save profile");
    assert.deepEqual(calls.create[0].processing_input.outputs, []);
    await change(harness, "Profile", "decoder-a");
    assert.deepEqual(input(harness).props.value.outputs, []);
    assert.equal(calls.update.length, 0);
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
    assert.equal(calls.review.length, 0);
  });
});

test("discard and duplication keep channel input inside its decoder profile", async () => {
  await withProfiles("processing", async (harness, calls) => {
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Discard");
    dialog(harness).props.onCancel(); await harness.settle();
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["C4"]).outputs);
    await click(harness, "Discard");
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["C3", "C4"]).outputs);
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Duplicate profile");
    dialog(harness).props.onConfirm("Decoder C"); await harness.settle();
    assert.deepEqual(calls.create[0].processing_input.outputs, decoderInput(["C4"]).outputs);
    assert.equal(calls.update.length, 0);
    await change(harness, "Profile", "decoder-a");
    assert.deepEqual(input(harness).props.value.outputs, decoderInput(["C3", "C4"]).outputs);
  });
});

test("renaming a decoder without channel mappings does not create a mapping implicitly", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    delete data.profiles.find((item) => item.profile_id === "decoder-a").processing_input;
    await click(harness, "Reload");
    assert.equal(input(harness).props.value, null);
    assert.equal(button(harness, "Duplicate profile").props.disabled, true);
    assert.match(text(harness.tree), /before duplicating this profile/);
    await change(harness, "Name", "Renamed decoder", "input");
    await click(harness, "Save profile");
    assert.equal("processing_input" in calls.update[0], false);
    assert.equal(input(harness).props.value, null);
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

test("Curry8 adopts ordered metadata labels including Trigger only after an explicit action", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(form(harness).props.schema.properties.channel_labels, undefined);
    await inspectSource(harness);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.deepEqual(form(harness).props.value, { gain: 2, calibration: [3, 4], sample_rate: 500,
      channel_count: 3, channel_labels: curryLabels }, "Use descriptor dimensions and ordered native labels");
    assert.match(text(harness.tree), /Channel labelsC3, C4, Trigger/);
    assert.equal(calls.update.length, 0);
    assert.equal(data.activation.bindings[0].parameters.channel_labels, undefined);
    await click(harness, "Review and apply configuration");
    assert.equal(calls.update.length, 0);
    assert.equal(data.activation.bindings[0].parameters.channel_labels, undefined);
    await acceptReviewed(harness, calls, data);
    assert.deepEqual(calls.create[0].parameters.channel_labels, curryLabels);
    await inspectSource(harness);
    assert.deepEqual(data.activation.bindings.find((item) => item.slot === "acquisition.source").parameters.channel_labels, curryLabels);
  }, { configure: observedCurryLabels });
});

test("Curry8 label adoption preserves duplicate and blank names without synthesizing a trigger label", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const labels = [" C3 ", " C3 ", ""];
    data.metadata.metadata = JSON.stringify({ channel_labels: labels });
    metadata(harness).props.onRead(); await harness.settle();
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.deepEqual(form(harness).props.value.channel_labels, labels);
    assert.equal(calls.update.length, 0);
  }, { engineRunning: true, configure: observedCurryLabels });
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
      assert.equal(calls.update.length, 0);
    }, { engineRunning: true, configure: (data) => observedCurryLabels(data, {
      sample_rate: 1000, channel_count: 3, channel_labels: curryLabels,
    }) });
  });
}

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

for (const mode of ["create", "update"]) {
  test(`Curry8 saved labels change only after explicit adoption and ${mode} confirmation`, async () => {
    const previousLabels = ["F3", "F4", "Event"];
    await withProfiles("acquisition", async (harness, calls, data) => {
      metadata(harness).props.onRead(); await harness.settle();
      metadata(harness).props.onUseObserved(); await harness.settle();
      assert.deepEqual(form(harness).props.value.channel_labels, curryLabels);
      assert.deepEqual(sourceBinding(data).parameters.channel_labels, previousLabels);
      await applyReviewed(harness, calls, data, { mode });
      assert.deepEqual(sourceBinding(data).parameters.channel_labels, curryLabels);
      assert.deepEqual((mode === "create" ? calls.create : calls.update)[0].parameters.channel_labels, curryLabels);
      if (mode === "create") assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a").parameters.channel_labels, previousLabels);
    }, { engineRunning: true, configure: (data) => observedCurryLabels(data, {
      sample_rate: 1000, channel_count: 3, channel_labels: previousLabels,
    }) });
  });
}

for (const pluginType of ["neuroscan_curry8_reader", "file_reader"]) {
  test(`${pluginType} saves connection parameters before connecting and adopts expectations explicitly`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      const originalProfiles = clone(data.profiles);
      await click(harness, "New profile");
      assert.equal(button(harness, "Connect").props.disabled, true);
      assert.deepEqual(data.events, []);
      await applyReviewed(harness, calls, data);
      assert.deepEqual(calls.create[0].parameters, { gain: 1, calibration: [1, 2] });
      await inspectSource(harness);
      assert.equal(metadata(harness).props.value.comparison.status, "unconfirmed");
      assert.equal(form(harness).props.value.sample_rate, undefined);
      assert.equal(form(harness).props.value.channel_count, undefined);
      assert.match(text(harness.tree), /Experiment expectations are not fully captured/);
      metadata(harness).props.onUseObserved(); await harness.settle();
      assert.equal(form(harness).props.value.sample_rate, 500);
      assert.equal(form(harness).props.value.channel_count, 3);
      assert.equal(calls.update.length, 0);
      assert.equal(data.activation.bindings.find((item) => item.slot === "acquisition.source").parameters.sample_rate, undefined);
      await applyReviewed(harness, calls, data, { mode: "update" });
      data.metadata.comparison = { status: "matched", sample_rate: { expected: 500, actual: 500 },
        channel_count: { expected: 3, actual: 3 } };
      await inspectSource(harness);
      assert.equal(metadata(harness).props.value.comparison.status, "matched");
      data.metadata.descriptor.sample_rate = 1000;
      data.metadata.comparison = { status: "mismatched", sample_rate: { expected: 500, actual: 1000 },
        channel_count: { expected: 3, actual: 3 } };
      metadata(harness).props.onRead(); await harness.settle();
      assert.equal(form(harness).props.value.sample_rate, 500);
      assert.equal(metadata(harness).props.value.comparison.status, "mismatched");
      assert.deepEqual(data.profiles.filter((item) => item.profile_id !== "created-1"), originalProfiles);
    }, { configure: (data) => observedSource(data, null, pluginType) });
  });
}

test("explicit metadata inspection can repeat on a connected source without promoting expectations", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await inspectSource(harness);
    await inspectSource(harness);
    assert.deepEqual(data.events, ["metadata", "metadata"]);
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.equal(data.activation.bindings[0].parameters.sample_rate, undefined);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { engineRunning: true, configure: observedCurryLabels });
});

test("an unsuccessful save-and-apply retains adopted observations for review", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    metadata(harness).props.onUseObserved(); await harness.settle();
    const original = clone(data.profiles);
    await click(harness, "Review and apply configuration");
    data.runtime.requestError = "Profile could not be saved.";
    harness.updateProps({ runtime: data.runtime }); await harness.settle();
    assert.deepEqual(form(harness).props.value, { gain: 2, calibration: [3, 4], sample_rate: 500, channel_count: 3 });
    assert.deepEqual(data.profiles, original);
    assert.equal(calls.apply.length, 0);
    assert.equal(data.dirty, true);
    data.runtime.dismissPlan();
    await applyReviewed(harness, calls, data, { mode: "update" });
    assert.equal(calls.update.length, 1);
    assert.equal(control(harness, "Profile").props.value, "lab-a");
    assert.equal(data.dirty, false);
  }, { engineRunning: true, configure: observedSource });
});

test("a metadata failure leaves the connected source visible and releases the editor for retry", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    controlApi.acqMetadata = async () => { throw new Error("Device metadata request timed out."); };
    await inspectSource(harness);
    assert.match(text(harness.tree), /Device metadata request timed out/);
    assert.match(text(harness.tree), /The current source is connected/);
    assert.equal(metadata(harness).props.busy, false);
    assert.equal(metadata(harness).props.value, null);
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(button(harness, "Disconnect").props.disabled, false);
    assert.equal(metadata(harness).props.canRead, true);
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { configure: observedCurryLabels });
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
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { engineRunning: true, configure: observedCurryLabels });
});

for (const pluginType of ["neuroscan_curry8_reader", "file_reader"]) {
  test(`${pluginType} establishes its baseline only after explicit adoption and saves a new revision`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      assert.match(text(harness.tree), /Expected experiment parameters/);
      assert.equal(elements(harness.tree).filter((item) => item.type.name === "SchemaForm").length, 1);
      metadata(harness).props.onRead(); await harness.settle();
      assert.equal(metadata(harness).props.value.comparison.status, "unconfirmed");
      assert.equal(metadata(harness).props.canUseObserved, true);
      assert.equal(form(harness).props.value.sample_rate, undefined);
      metadata(harness).props.onUseObserved(); await harness.settle();
      assert.deepEqual(form(harness).props.value, { gain: 2, calibration: [3, 4], sample_rate: 500, channel_count: 3 });
      assert.equal(metadata(harness).props.value, null);
      assert.equal(calls.update.length, 0);
      assert.equal(data.activation.bindings[0].parameters.sample_rate, undefined);
      await applyReviewed(harness, calls, data, { mode: "update" });
      assert.equal(calls.update[0].profile.revision, 2);
      assert.equal(calls.update[0].parameters.channel_count, 3);
      await inspectSource(harness);
      const applied = data.activation.bindings.find((item) => item.slot === "acquisition.source");
      assert.equal(applied.profile_revision, 3);
      assert.equal(applied.parameters.sample_rate, 500);
      assert.deepEqual(metadata(harness).props.value, data.metadata);
    }, { engineRunning: true, configure: (data) => observedSource(data, null, pluginType) });
  });
}

test("a mismatched source never overwrites expected values without explicit adoption save and apply", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    assert.equal(metadata(harness).props.value.comparison.status, "mismatched");
    assert.equal(form(harness).props.value.sample_rate, 1000);
    assert.equal(data.activation.bindings[0].parameters.sample_rate, 1000);
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.equal(button(harness, "Connect").props.disabled, true);
    await applyReviewed(harness, calls, data, { mode: "update" });
    await inspectSource(harness);
    assert.equal(calls.update[0].parameters.sample_rate, 500);
    assert.equal(calls.update[0].parameters.channel_count, 3);
    assert.equal(data.activation.bindings.find((item) => item.slot === "acquisition.source").profile_revision, 3);
  }, { engineRunning: true, configure: (data) => observedSource(data, { sample_rate: 1000, channel_count: 10 }) });
});

for (const pluginType of ["neuroscan_curry8_reader", "file_reader"]) {
  test(`${pluginType} observed values use the same default-copy Review as manual edits`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      const original = clone(data.profiles.find((item) => item.profile_id === "lab-a"));
      metadata(harness).props.onRead(); await harness.settle();
      assert.equal(metadata(harness).props.onSaveObservedProfile, undefined);
      metadata(harness).props.onUseObserved(); await harness.settle();
      await click(harness, "Review and apply configuration");
      assert.equal(data.runtime.profileReview.mode, "create");
      assert.equal(calls.create.length, 0);
      data.runtime.dismissPlan();
      assert.equal(form(harness).props.value.sample_rate, 500);
      assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a"), original);
      await applyReviewed(harness, calls, data, { name: "Observed experiment" });
      assert.equal(sourceBinding(data).profile_id, "created-1");
      assert.equal(sourceBinding(data).parameters.sample_rate, 500);
      assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a"), original);
    }, { engineRunning: true, configure: (data) => observedSource(data, { sample_rate: 1000, channel_count: 10 }, pluginType) });
  });
}

test("a stale adoption callback cannot apply metadata from an earlier source connection", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    metadata(harness).props.onRead(); await harness.settle();
    const staleAdopt = metadata(harness).props.onUseObserved;
    data.source.connection_id = "connection-b";
    await data.acquisition.refresh(); await harness.settle();
    staleAdopt(); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_labels, undefined);
    assert.equal(calls.update.length, 0);
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
    assert.equal(calls.update.length, 0);
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
    assert.equal(calls.update.length, 0);
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
    assert.equal(calls.update.length, 0);
  }, { engineRunning: true, configure: (data) => {
    observedSource(data, { sample_rate: 500, channel_count: 3 }, "neuroscan_scan45_reader");
    data.metadata.descriptor.provenance = "user-declared";
    data.metadata.comparison.status = "unverified";
  } });
});

test("new observed-source profiles leave expectations absent instead of inventing defaults", async () => {
  await withProfiles("acquisition", async (harness) => {
    await click(harness, "New profile");
    assert.equal(form(harness).props.value.sample_rate, undefined);
    assert.equal(form(harness).props.value.channel_count, undefined);
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.match(text(harness.tree), /Use source value/);
  }, { configure: observedSource });
});

test("acquisition naming and duplication are confined to final Review", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(elements(harness.tree).some((item) => item.type === "input" && item.props.maxLength === 64), false);
    assert.equal(elements(harness.tree).some((item) => item.type === "button" && ["Save profile", "Duplicate profile"].includes(text(item))), false);
    await click(harness, "Connect");
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    assert.deepEqual(data.events, ["start", "connect"]);
  });
});

test("a saved source connects from a stopped engine without inspecting metadata, saving or applying", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    assert.equal(metadata(harness).props.value, null);
  });
});

test("a new reported source can confirm automatic saving before observing optional expectations", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "New configuration");
    await applyReviewed(harness, calls, data, { name: "Connection only" });
    assert.equal(calls.create.length, 1);
    assert.equal(calls.create[0].parameters.sample_rate, undefined);
    assert.equal(calls.create[0].parameters.channel_count, undefined);
    assert.equal(!!button(harness, "Connect").props.disabled, false);
    assert.deepEqual(data.events, ["apply"]);
  }, { configure: observedSource });
});

test("source controls disconnect the shared current source while browsing another profile, but never while streaming", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Profile", "lab-b");
    assert.match(text(harness.tree), /Current source.*Lab A/);
    assert.equal(button(harness, "Disconnect").props.disabled, false);
    await button(harness, "Disconnect").props.onClick(); await harness.settle();
    assert.deepEqual(data.events, ["disconnect"]);
    assert.equal(control(harness, "Profile").props.value, "lab-b");
  }, { engineRunning: true });
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(button(harness, "Disconnect").props.disabled, true);
    await button(harness, "Disconnect").props.onClick(); await harness.settle();
    assert.deepEqual(data.events, []);
  }, { engineRunning: true, streaming: true });
});

test("same-render Connect clicks launch one source operation", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const prepare = button(harness, "Connect").props.onClick;
    prepare(); prepare(); await harness.settle();
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.equal(metadata(harness).props.busy, false);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("shared connection updates immediately enable or disable metadata and connection actions", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    assert.equal(button(harness, "Connect").props.disabled, false);
    Object.assign(data.runtime.status, { running: true, applied_revision: data.activation.revision, generation: "generation-a" });
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

test("connecting does not inspect metadata and disables inspection if another client starts reading", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    const connect = controlApi.acqConnect;
    controlApi.acqConnect = async () => { await connect(); data.source.streaming = true; };
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.equal(metadata(harness).props.value, null);
    assert.equal(metadata(harness).props.busy, false);
    assert.match(metadata(harness).props.readHint, /Stop reading in Run before getting fresh metadata/);
  });
});

test("acquisition save shortcut opens Review without persisting until confirmation", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    form(harness).props.onChange({ gain: 12, calibration: [3, 4] }); await harness.settle();
    assert.equal(await saveShortcut(harness), true);
    assert.equal(calls.review.length, 1);
    assert.equal(data.runtime.profileReview.mode, "create");
    assert.equal(calls.create.length + calls.update.length + calls.apply.length, 0);
  });
});

test("a connection failure stays beside Connect and keeps the started engine available for retry", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const connect = controlApi.acqConnect;
    controlApi.acqConnect = async () => { data.events.push("connect"); throw new Error("Command execution failed"); };
    await click(harness, "Connect");
    assert.match(text(harness.tree), /Connect:.*Command execution failed/);
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.equal(data.runtime.status.running, true);
    assert.equal(metadata(harness).props.busy, false);
    assert.equal(button(harness, "Connect").props.disabled, false);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    controlApi.acqConnect = connect;
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["start", "connect", "connect"]);
    assert.equal(metadata(harness).props.value, null);
  });
});


test("Connect only connects the shared source and leaves metadata inspection explicit", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.equal(elements(harness.tree).some((item) => item.type === "button" && text(item) === "Prepare & inspect"), false);
    await click(harness, "Connect");
    assert.deepEqual(data.events, ["start", "connect"]);
    assert.equal(metadata(harness).props.value, null);
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(button(harness, "Disconnect").props.disabled, false);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});


const sourceBinding = (data) => data.activation.bindings.find((item) => item.slot === "acquisition.source");
const withoutProfiles = (data) => { data.profiles = []; data.activation = null; };

test("a first-use source names and saves its profile only at final Review confirmation", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    assert.ok(button(harness, "New configuration"));
    assert.ok(control(harness, "Profile (optional)"));
    assert.equal(elements(harness.tree).some((item) => item.type === "input" && item.props.maxLength === 64), false);
    assert.equal(elements(harness.tree).some((item) => item.type === "button" && text(item) === "Save profile"), false);
    assert.equal(button(harness, "Connect").props.disabled, true);
    form(harness).props.onChange({ gain: 7, calibration: [5, 6] }); await harness.settle();
    assert.equal(data.dirty, true);
    await click(harness, "Review and apply configuration");
    assert.deepEqual(calls.review, [{ module: "acquisition", bindings: [{ slot: "acquisition.source",
      selection: scanV1, parameters: { gain: 7, calibration: [5, 6] } }],
      expected_revision: null, refresh_slots: ["acquisition.source"], include_host_settings: false }]);
    assert.equal(data.activation, null, "Review alone does not create the startup configuration");
    assert.equal(data.dirty, true, "An unconfirmed review does not discard the draft");
    assert.deepEqual(data.events, []);
    await acceptReviewed(harness, calls, data);
    assert.equal(sourceBinding(data).profile_id, "created-1");
    assert.equal(data.dirty, false, "Successfully applied parameters are persisted, even without a template");
    await click(harness, "Connect");
    await inspectSource(harness);
    assert.deepEqual(data.events, ["apply", "start", "connect", "metadata"]);
    await click(harness, "Open Run");
    assert.deepEqual(data.openRunStates, [false], "Open Run does not report already-applied parameters as unsaved");
    assert.equal(calls.create.length, 1);
    assert.equal(calls.update.length, 0);
    assert.equal(data.profiles[0].profile_id, "created-1");
  }, { configure: withoutProfiles });
});

for (const pluginType of ["neuroscan_curry8_reader", "file_reader"]) {
  test(`refresh restores an inline ${pluginType} selection, parameters and expected source values`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      const expected = clone(sourceBinding(data).parameters);
      assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
      assert.deepEqual(form(harness).props.value, expected);
      assert.equal(elements(harness.tree).some((item) => item.type === "input" && item.props.maxLength === 64), false);
      assert.equal(data.dirty, false);
      assert.equal(!!button(harness, "Connect").props.disabled, false);
      const refreshed = await data.remount();
      assert.deepEqual(form(refreshed).props.value, expected);
      assert.equal(data.dirty, false);
      await click(refreshed, "Connect");
      assert.equal(metadata(refreshed).props.canRead, true);
      assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
    }, { configure: (data) => {
      observedSource(data, { sample_rate: 500, channel_count: 3,
        ...(pluginType === "neuroscan_curry8_reader" ? { channel_labels: curryLabels } : {}) }, pluginType);
      const source = sourceBinding(data);
      source.profile_id = null; source.profile_revision = null;
      source.parameters.gain = 19;
      // A matching saved template and an earlier catalog entry must not replace the current inline setup.
      data.definitions.unshift(definition("acquisition", unsavedPlugin, "Other default source"));
    } });
  });
}

test("reapplying the automatically saved source does not create duplicate profiles", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await applyReviewed(harness, calls, data);
    const savedId = sourceBinding(data).profile_id;
    await click(harness, "Connect");
    const connection = clone(data.source), events = [...data.events];
    await applyReviewed(harness, calls, data);
    assert.equal(calls.create.length, 1);
    assert.equal(calls.update.length, 0);
    assert.equal(sourceBinding(data).profile_id, savedId);
    assert.deepEqual(data.source, connection);
    assert.deepEqual(data.events, [...events, "apply"]);
    assert.equal(data.dirty, false);
  }, { configure: withoutProfiles });
});

for (const outcome of ["cancelled", "review failed", "apply failed"]) {
  test(`a ${outcome} configuration review retains the source draft and leaves Connect disabled`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      form(harness).props.onChange({ gain: 11, calibration: [7, 8] }); await harness.settle();
      if (outcome === "review failed") data.runtime.review = async (body) => {
        calls.review.push(clone(body)); data.runtime.requestError = "Configuration changed elsewhere"; return null;
      };
      await click(harness, "Review and apply configuration");
      if (outcome === "cancelled") data.runtime.dismissPlan();
      if (outcome === "apply failed") await acceptReviewed(harness, calls, data);
      harness.updateProps({ runtime: data.runtime }); await harness.settle();
      assert.deepEqual(form(harness).props.value, { gain: 11, calibration: [7, 8] });
      assert.equal(data.dirty, true);
      assert.equal(button(harness, "Connect").props.disabled, true);
      assert.equal(calls.create.length, outcome === "apply failed" ? 1 : 0); assert.equal(calls.update.length, 0);
      await change(harness, "Plugin and version", selectionKey(replay));
      assert.ok(dialog(harness), "Leaving the draft still requires an explicit discard");
      dialog(harness).props.onCancel(); await harness.settle();
      assert.equal(form(harness).props.value.gain, 11);
    }, { applySucceeds: outcome !== "apply failed" });
  });
}

test("applying an earlier reviewed snapshot does not erase subsequent parameter edits", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    form(harness).props.onChange({ gain: 8, calibration: [3, 4] }); await harness.settle();
    await click(harness, "Review and apply configuration");
    form(harness).props.onChange({ gain: 9, calibration: [10, 11] }); await harness.settle();
    await acceptReviewed(harness, calls, data);
    assert.equal(sourceBinding(data).parameters.gain, 8);
    assert.deepEqual(form(harness).props.value, { gain: 9, calibration: [10, 11] });
    assert.equal(data.dirty, true);
    assert.equal(button(harness, "Connect").props.disabled, true);
  });
});

test("successful Apply adopts the profile selected in final Review and clears the saved draft", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    form(harness).props.onChange({ gain: 8, calibration: [3, 4] }); await harness.settle();
    await applyReviewed(harness, calls, data, { name: "Tested alternative" });
    assert.equal(form(harness).props.value.gain, 8);
    assert.equal(control(harness, "Profile").props.value, "created-1");
    assert.equal(calls.create[0].name, "Tested alternative");
    assert.equal(data.dirty, false);
    await click(harness, "Open Run");
    assert.deepEqual(data.openRunStates, [false]);
    assert.equal(dialog(harness), undefined);
  });
});

test("a successful unrelated operation never claims or discards the pending source draft", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    form(harness).props.onChange({ gain: 13, calibration: [3, 4] }); await harness.settle();
    await click(harness, "Review and apply configuration");
    const unrelated = { operation_id: "other-client", plan_id: "other-plan", status: "succeeded",
      target_revision: data.runtime.plan.target_revision };
    sourceBinding(data).parameters = { gain: 20, calibration: [3, 4] };
    data.activation.revision = unrelated.target_revision;
    Object.assign(data.runtime.status, { accepted_revision: unrelated.target_revision, operation: unrelated });
    data.runtime.operation = unrelated; data.runtime.completedOperation = unrelated;
    harness.updateProps({ runtime: data.runtime }); await harness.settle();
    assert.equal(form(harness).props.value.gain, 13);
    assert.equal(data.dirty, true);
    assert.equal(button(harness, "Connect").props.disabled, true);
    assert.equal(calls.apply.length, 0);
  });
});

test("Apply adopts normalized server parameters instead of leaving an applied draft blocked", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    form(harness).props.onChange({ gain: 6, calibration: [3, 4] }); await harness.settle();
    const apply = data.runtime.applySelection;
    data.runtime.applySelection = async (body) => {
      const result = await apply(body);
      sourceBinding(data).parameters.timeout = 1000;
      return result;
    };
    await applyReviewed(harness, calls, data);
    assert.deepEqual(form(harness).props.value, { gain: 6, calibration: [3, 4], timeout: 1000 });
    assert.equal(data.dirty, false);
    assert.equal(!!button(harness, "Connect").props.disabled, false);
  }, { configure: (data) => { data.definitions[0].schema.properties.timeout = { type: "integer", default: 1000 }; } });
});

test("a successful profile Apply with an unchanged activation revision still settles its draft", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const revision = data.activation.revision;
    const parameters = clone(sourceBinding(data).parameters);
    await click(harness, "New configuration");
    form(harness).props.onChange(parameters); await harness.settle();
    assert.equal(data.dirty, true);
    const review = data.runtime.review;
    data.runtime.review = async (body, draft) => {
      await review(body, draft); data.runtime.plan.target_revision = revision; return clone(data.runtime.plan);
    };
    await click(harness, "Review and apply configuration");
    assert.equal(calls.review[0].bindings[0].profile_id, undefined);
    await acceptReviewed(harness, calls, data, { revision });
    assert.equal(data.activation.revision, revision);
    assert.equal(sourceBinding(data).profile_id, "created-1");
    assert.deepEqual(form(harness).props.value, parameters);
    assert.equal(data.dirty, false);
    assert.equal(!!button(harness, "Connect").props.disabled, false);
  });
});

for (const pluginType of ["neuroscan_curry8_reader", "file_reader"]) {
  test(`a new ${pluginType} source automatically saves before applying adopted expectations`, async () => {
    await withProfiles("acquisition", async (harness, calls, data) => {
      assert.equal(form(harness).props.value.sample_rate, undefined);
      await applyReviewed(harness, calls, data);
      await inspectSource(harness);
      assert.equal(metadata(harness).props.canUseObserved, true);
      metadata(harness).props.onUseObserved(); await harness.settle();
      assert.equal(form(harness).props.value.sample_rate, 500);
      assert.equal(form(harness).props.value.channel_count, 3);
      assert.equal(data.dirty, true);
      assert.equal(sourceBinding(data).parameters.sample_rate, undefined);
      await applyReviewed(harness, calls, data);
      assert.equal(sourceBinding(data).parameters.sample_rate, 500);
      assert.equal(sourceBinding(data).parameters.channel_count, 3);
      assert.equal(data.dirty, false);
      assert.equal(calls.create.length, 2); assert.equal(calls.update.length, 0);
      const refreshed = await data.remount();
      assert.equal(form(refreshed).props.value.sample_rate, 500);
      assert.equal(form(refreshed).props.value.channel_count, 3);
      assert.equal(data.dirty, false);
    }, { configure: (data) => { observedSource(data, null, pluginType); withoutProfiles(data); } });
  });
}

test("new source defaults matching the applied configuration allow metadata adoption after connecting", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await click(harness, "New configuration");
    assert.deepEqual(form(harness).props.value, { gain: 1, calibration: [1, 2] });
    assert.equal(!!button(harness, "Connect").props.disabled, false);
    await inspectSource(harness);
    assert.equal(metadata(harness).props.canUseObserved, true);
    assert.equal(typeof metadata(harness).props.onUseObserved, "function");
    metadata(harness).props.onUseObserved(); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, 500);
    assert.equal(form(harness).props.value.channel_count, 3);
    assert.equal(sourceBinding(data).parameters.sample_rate, undefined);
    assert.equal(data.dirty, true, "Adopting observations still requires explicit reviewed Apply");
    assert.deepEqual(data.events, ["start", "connect", "metadata"]);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  }, { configure: (data) => {
    observedSource(data);
    sourceBinding(data).parameters = { gain: 1, calibration: [1, 2] };
  } });
});
