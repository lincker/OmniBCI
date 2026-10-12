/** Exercise profile browsing and explicit activation through component events. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { PluginProfiles } = await server.ssrLoadModule("/src/PluginProfiles.tsx");
const { pluginConfigApi: api, selectionKey } = await server.ssrLoadModule("/src/api/plugin-config.ts");

const selection = (packageId, version = "1.0.0") => ({ package_id: packageId, contribution_id: "main", version });
const scanV1 = selection("example.scan45");
const scanV2 = selection("example.scan45", "2.0.0");
const replay = selection("example.replay");
const unsavedPlugin = selection("example.new-source");
const bandpass = selection("example.bandpass");
const notch = selection("example.notch");
const decoder = selection("example.decoder");
const channelRequirements = { description: "Select the configured motor channels.", signal_type: "eeg", min_count: 2,
  required_names: ["C3"], default_names: ["C4", "C3"] };
const decoderInput = (outputs) => ({ schema_version: 1, outputs, reference: { mode: "original", channels: [] }, excluded_channels: [] });
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
    profile("replay", "acquisition", replay, "Recorded session", 5),
    profile("band-a", "filter", bandpass, "Band A", 6),
    profile("band-b", "filter", bandpass, "Band B", 7),
    profile("notch-a", "filter", notch, "Notch A", 8),
    { ...profile("decoder-a", "decoder", decoder, "Decoder A", 1), processing_input: decoderInput(["C3", "C4"]) },
    { ...profile("decoder-b", "decoder", decoder, "Decoder B", 2), processing_input: decoderInput(["P3", "P4"]) },
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
    async settle() {
      for (let round = 0; round < 24; round += 1) {
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
  const result = elements(scope).find((item) => item.type === "button" && text(item) === label);
  assert.ok(result, `Missing button: ${label}`);
  return result;
}
function control(harness, label, type = "select") {
  const all = elements(harness.tree);
  const explicit = all.find((item) => item.type === type && item.props["aria-label"] === label);
  if (explicit) return explicit;
  const owner = all.find((item) => item.type === "label" && (text(item) === label || elements(item.props.children)
    .some((child) => child.type === "span" && text(child) === label)));
  assert.ok(owner, `Missing label: ${label}`);
  const result = elements(owner).find((item) => item.type === type)
    ?? all.find((item) => item.type === type && item.props.id === owner.props.htmlFor);
  assert.ok(result, `Missing ${type}: ${label}`);
  return result;
}
const options = (select) => elements(select).filter((item) => item.type === "option").map((item) => ({ value: item.props.value, label: text(item) }));
const form = (harness) => elements(harness.tree).find((item) => item.type.name === "SchemaForm");
const input = (harness) => elements(harness.tree).find((item) => item.type.name === "ProcessingInput");
const montage = (harness) => elements(harness.tree).find((item) => item.type.name === "AcquisitionMontage");
const dialog = (harness) => elements(harness.tree).find((item) => item.type.name === "ConfigDialog");
const filterRows = (harness) => elements(harness.tree).filter((item) => item.type === "ol" && item.props.className === "filter-chain")
  .flatMap((item) => React.Children.toArray(item.props.children));

async function click(harness, label, scope) {
  const target = button(harness, label, scope);
  assert.equal(!!target.props.disabled, false, `${label} should be enabled`);
  await target.props.onClick(); await harness.settle();
}
async function change(harness, label, value, type = "select") {
  const target = control(harness, label, type);
  assert.equal(!!target.props.disabled, false, `${label} should be enabled`);
  await target.props.onChange({ target: { value } }); await harness.settle();
}

async function withProfiles(module, action, { engineRunning = false } = {}) {
  const originalApi = { ...api };
  const data = fixture();
  const calls = { create: [], update: [], apply: [], review: [] };
  api.catalog = async (kind) => clone(data.definitions.filter((item) => item.kind === kind));
  api.profiles = async () => clone(data.profiles);
  api.activation = async () => clone(data.activation);
  api.create = async (kind, choice, name, parameters, manual_montage, processing_input) => {
    const montage = { ...(manual_montage === undefined ? {} : { manual_montage: clone(manual_montage) }),
      ...(processing_input === undefined ? {} : { processing_input: clone(processing_input) }) };
    calls.create.push(clone({ kind, selection: choice, name, parameters, ...montage }));
    const saved = { profile_id: `created-${calls.create.length}`, kind, selection: clone(choice), name, parameters: clone(parameters), revision: 1, ...montage };
    data.profiles.push(saved); return clone(saved);
  };
  api.update = async (saved, name, parameters, manual_montage, processing_input) => {
    const montage = { ...(manual_montage === undefined ? {} : { manual_montage: clone(manual_montage) }),
      ...(processing_input === undefined ? {} : { processing_input: clone(processing_input) }) };
    calls.update.push(clone({ profile: saved, name, parameters, ...montage }));
    const updated = { ...saved, name, parameters: clone(parameters), revision: saved.revision + 1, ...montage };
    data.profiles = data.profiles.map((item) => item.profile_id === saved.profile_id ? updated : item); return clone(updated);
  };
  const harness = componentHarness({ module, engineRunning, runtime: {
    blocked: false, status: { running: false, accepted_revision: data.activation.revision, applied_revision: null },
    review: async (body) => { calls.review.push(clone(body)); },
    apply: async (...args) => { calls.apply.push(args); },
  } });
  try { await harness.settle(); await action(harness, calls, data); }
  finally { harness.dispose(); Object.assign(api, originalApi); }
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

const manualMontage = () => ({ source_id: "scan45", sample_rate: 500, channels: [
  { index: 0, name: "C3", signal_type: "eeg", unit: "uV" },
  { index: 1, name: "C4", signal_type: "eeg", unit: "uV" },
], trigger_row_index: 2, connection_id: "", native_event: "final-row", provenance: "user-declared" });

test("manual montage saves with profile parameters and remains pending explicit apply", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    montage(harness).props.onChange(manualMontage()); await harness.settle();
    assert.equal(!!button(harness, "Review and apply profile").props.disabled, true);
    await click(harness, "Save profile");
    assert.deepEqual(calls.update[0].manual_montage, manualMontage());
    assert.equal("acquisition_montage" in calls.update[0], false);
    assert.deepEqual(data.activation, accepted);
    assert.equal(calls.review.length, 0);
    await click(harness, "Review and apply profile");
    assert.deepEqual(calls.review[0].refresh_slots, ["acquisition.source"]);
  });
});

test("manual montage dimensions and source parameters stay synchronized", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    form(harness).props.onChange({ gain: 2, calibration: [3, 4], sample_rate: 1000, channel_number: 4 }); await harness.settle();
    montage(harness).props.onChange({ ...manualMontage(), signal_channel_count: 2, output_row_count: 3 }); await harness.settle();
    assert.equal(form(harness).props.value.sample_rate, 500);
    assert.equal(form(harness).props.value.channel_number, 3);
    form(harness).props.onChange({ ...form(harness).props.value, sample_rate: 250, channel_number: 4 }); await harness.settle();
    assert.equal(montage(harness).props.value.sample_rate, 250);
    assert.equal(montage(harness).props.value.channels.length, 3);
    assert.equal(montage(harness).props.value.channels[2].name_provenance, "generated");
    assert.equal(montage(harness).props.value.trigger_row_index, 3);
    assert.equal(montage(harness).props.value.signal_channel_count, undefined);
    assert.equal(montage(harness).props.value.output_row_count, undefined);
    assert.equal(calls.update.length, 0);
  });
});

test("reading source observations retains unsaved edits and advances only the observation revision", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Name", "Edited lab", "input");
    form(harness).props.onChange({ gain: 9, calibration: [3, 4] });
    await harness.settle();
    montage(harness).props.onChange(manualMontage()); await harness.settle();
    let readId;
    api.readMontage = async (id) => {
      readId = id;
      const saved = data.profiles.find((profile) => profile.profile_id === id);
      saved.acquisition_montage = { ...manualMontage(), provenance: "device-reported", connection_id: "observed-connection" };
      saved.revision += 1;
    };
    montage(harness).props.onRead(); await harness.settle();
    assert.equal(readId, "lab-a");
    assert.equal(control(harness, "Name", "input").props.value, "Edited lab");
    assert.equal(form(harness).props.value.gain, 9);
    assert.deepEqual(montage(harness).props.value, manualMontage());
    assert.equal(montage(harness).props.observed.connection_id, "observed-connection");
    await click(harness, "Save profile");
    assert.equal(calls.update[0].profile.revision, 3);
    assert.equal(calls.update[0].parameters.gain, 9);
    assert.equal(calls.update[0].name, "Edited lab");
  }, { engineRunning: true });
});

test("a failed metadata validation still shows its saved observation without discarding edits", async () => {
  await withProfiles("acquisition", async (harness, _, data) => {
    await change(harness, "Name", "Unsaved lab", "input");
    api.readMontage = async (id) => {
      const saved = data.profiles.find((profile) => profile.profile_id === id);
      saved.acquisition_montage = { ...manualMontage(), provenance: "device-reported" }; saved.revision += 1;
      throw new Error("Manual montage conflicts with source units.");
    };
    montage(harness).props.onRead(); await harness.settle();
    assert.match(text(harness.tree), /Manual montage conflicts with source units/);
    assert.equal(montage(harness).props.observed.provenance, "device-reported");
    assert.equal(control(harness, "Name", "input").props.value, "Unsaved lab");
  }, { engineRunning: true });
});

test("a concurrent writable profile change cannot be hidden by a metadata refresh", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    await change(harness, "Name", "My unsaved name", "input");
    api.readMontage = async (id) => {
      const saved = data.profiles.find((profile) => profile.profile_id === id);
      saved.parameters.gain = 77; saved.revision += 1;
    };
    montage(harness).props.onRead(); await harness.settle();
    assert.match(text(harness.tree), /profile was edited elsewhere/);
    assert.equal(control(harness, "Name", "input").props.value, "My unsaved name");
    await click(harness, "Save profile");
    assert.equal(calls.update[0].profile.revision, 2, "Keep the stale guard so the backend rejects an overwrite");
  }, { engineRunning: true });
});

test("browsing profiles and a plugin without profiles never selects a runtime binding", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    await change(harness, "Profile", "lab-b");
    assert.equal(form(harness).props.value.gain, 3);
    await change(harness, "Plugin and version", selectionKey(unsavedPlugin));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(unsavedPlugin));
    assert.deepEqual(form(harness).props.value, { gain: 1, calibration: [1, 2] });
    assert.equal(!!button(harness, "Review and apply profile").props.disabled, true);
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("New profile uses the current plugin defaults and Save creates without applying", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    await change(harness, "Plugin and version", selectionKey(scanV2));
    await click(harness, "New profile");
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV2));
    assert.deepEqual(form(harness).props.value, { gain: 1, calibration: [1, 2] });
    await change(harness, "Name", "New version 2 lab", "input");
    await click(harness, "Save profile");
    assert.deepEqual(calls.create, [{ kind: "acquisition", selection: scanV2, name: "New version 2 lab", parameters: { gain: 1, calibration: [1, 2] } }]);
    assert.equal(calls.update.length, 0); assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
    assert.equal(control(harness, "Profile").props.value, "created-1");
  });
});

test("Duplicate profile isolates parameters and creates a new profile without changing the original", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const original = clone(data.profiles.find((item) => item.profile_id === "lab-a"));
    form(harness).props.onChange({ gain: 9, calibration: [10, 11] }); await harness.settle();
    await click(harness, "Duplicate profile");
    assert.equal(dialog(harness).props.title, "Duplicate profile");
    assert.equal(calls.create.length, 0, "Opening the name dialog must not save a copy");
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(calls.create.length, 0, "Cancelling the name dialog must not save a copy");
    assert.deepEqual(form(harness).props.value, { gain: 9, calibration: [10, 11] });
    await click(harness, "Duplicate profile");
    dialog(harness).props.onConfirm("Lab A alternative"); await harness.settle();
    assert.deepEqual(calls.create, [{ kind: "acquisition", selection: scanV1, name: "Lab A alternative", parameters: { gain: 9, calibration: [10, 11] } }]);
    assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a"), original);
    assert.equal(calls.update.length, 0); assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
    assert.equal(control(harness, "Profile").props.value, "created-1");
    form(harness).props.onChange({ gain: 15, calibration: [20, 21] }); await harness.settle();
    await click(harness, "Save profile");
    assert.equal(calls.update[0].profile.profile_id, "created-1");
    assert.deepEqual(data.profiles.find((item) => item.profile_id === "lab-a"), original);
  });
});

test("plugin changes require an explicit discard when the edited profile is dirty", async () => {
  await withProfiles("acquisition", async (harness, calls) => {
    await change(harness, "Name", "Unsaved name", "input");
    await change(harness, "Plugin and version", selectionKey(scanV2));
    assert.ok(dialog(harness));
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV1));
    dialog(harness).props.onCancel(); await harness.settle();
    assert.equal(dialog(harness), undefined);
    assert.equal(control(harness, "Name", "input").props.value, "Unsaved name");
    await change(harness, "Plugin and version", selectionKey(scanV2));
    dialog(harness).props.onConfirm(""); await harness.settle();
    assert.equal(control(harness, "Plugin and version").props.value, selectionKey(scanV2));
    assert.equal(control(harness, "Name", "input").props.value, "Version 2 lab");
    assert.deepEqual(calls, { create: [], update: [], apply: [], review: [] });
  });
});

test("saving changed parameters keeps the old activation until an explicit review", async () => {
  await withProfiles("acquisition", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    form(harness).props.onChange({ gain: 12, calibration: [3, 4] }); await harness.settle();
    assert.equal(!!button(harness, "Review and apply profile").props.disabled, true);
    await click(harness, "Save profile");
    assert.equal(calls.update.length, 1); assert.equal(calls.create.length, 0);
    assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted);
    await click(harness, "Review and apply profile");
    assert.equal(calls.review.length, 1);
    assert.deepEqual(calls.review[0].refresh_slots, ["acquisition.source"]);
    assert.deepEqual(calls.review[0].bindings.find((item) => item.slot === "acquisition.source"), { slot: "acquisition.source", profile_id: "lab-a" });
    assert.equal(calls.review[0].expected_revision, accepted.revision);
    assert.equal("include_signal_setup" in calls.review[0], false);
    assert.equal(calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted);
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
    assert.equal(control(harness, "Filter 1 profile").props.value, "band-a");
    assert.equal(control(harness, "Filter 2 profile").props.value, "notch-a");
    assert.equal(!!button(harness, "Review and apply filter chain").props.disabled, true);
    assert.equal(calls.review.length, 0); assert.equal(calls.apply.length, 0);
  });
});


test("decoder profiles retain independent ordered channels and wait for reviewed activation", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    const accepted = clone(data.activation);
    assert.deepEqual(input(harness).props.value.outputs, ["C3", "C4"]);
    assert.deepEqual(input(harness).props.decoderSelection, decoder);
    assert.deepEqual(input(harness).props.channelRequirements, channelRequirements);
    assert.equal(montage(harness), undefined, "Processing must not edit source facts");
    const updatedInput = { ...decoderInput(["C4", "C3"]), reference: { mode: "single", channels: ["M1"] } };
    input(harness).props.onChange(updatedInput); await harness.settle();
    assert.equal(button(harness, "Review and apply profile").props.disabled, true);
    await click(harness, "Save profile");
    assert.deepEqual(calls.update[0].processing_input, updatedInput);
    assert.deepEqual(data.activation, accepted);
    assert.match(text(harness.tree), /saved changes that are not selected/);
    await change(harness, "Profile", "decoder-b");
    assert.deepEqual(input(harness).props.value.outputs, ["P3", "P4"]);
    await change(harness, "Profile", "decoder-a");
    assert.deepEqual(input(harness).props.value, updatedInput);
    await click(harness, "Review and apply profile");
    assert.deepEqual(calls.review[0].refresh_slots, ["processing.decoder"]);
    assert.equal(calls.apply.length, 0);
    assert.deepEqual(data.activation, accepted);
  });
});

test("new decoder profiles initialize plugin channel defaults while saved all-source profiles stay unchanged", async () => {
  await withProfiles("processing", async (harness, calls, data) => {
    data.profiles.find((item) => item.profile_id === "decoder-a").processing_input.outputs = [];
    await click(harness, "Reload");
    assert.deepEqual(input(harness).props.value.outputs, []);
    await click(harness, "New profile");
    assert.deepEqual(input(harness).props.value, decoderInput(["C4", "C3"]));
    await change(harness, "Name", "Plugin defaults", "input");
    await click(harness, "Save profile");
    assert.deepEqual(calls.create[0].processing_input.outputs, ["C4", "C3"]);
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
    assert.deepEqual(input(harness).props.value.outputs, ["P3", "P4"]);
    assert.deepEqual(data.activation.bindings.find((binding) => binding.slot === "processing.decoder").selection, decoder);
    assert.equal(calls.review.length, 0);
  });
});

test("discard and duplication keep channel input inside its decoder profile", async () => {
  await withProfiles("processing", async (harness, calls) => {
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Discard");
    assert.deepEqual(input(harness).props.value.outputs, ["C3", "C4"]);
    input(harness).props.onChange(decoderInput(["C4"])); await harness.settle();
    await click(harness, "Duplicate profile");
    dialog(harness).props.onConfirm("Decoder C"); await harness.settle();
    assert.deepEqual(calls.create[0].processing_input.outputs, ["C4"]);
    assert.equal(calls.update.length, 0);
    await change(harness, "Profile", "decoder-a");
    assert.deepEqual(input(harness).props.value.outputs, ["C3", "C4"]);
  });
});

test("saving a legacy decoder name does not adopt a new channel input implicitly", async () => {
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
