/** Decoder channel mappings preserve source descriptors and reject stale previews. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { ProcessingInput, processingInputErrors, emptyProcessingInput } = await server.ssrLoadModule("/src/ProcessingInput.tsx");
const { signalConfigApi: api } = await server.ssrLoadModule("/src/api/signal-config.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");
const clone = (value) => structuredClone(value);
const source = { source_id: "file_reader", sample_rate: 500, trigger_row_index: 3, provenance: "file-reported", connection_id: "connection-1",
  channels: ["C3", "C4", "M1"].map((name, index) => ({ channel_id: `sample-${index}`, index, name, signal_type: "eeg", unit: "V" })) };
const output = (name) => ({ name, source_channel_id: source.channels.find((channel) => channel.name === name)?.channel_id ?? name });
const mapping = (...names) => ({ schema_version: 2, outputs: names.map(output) });
const sourceBinding = { source_id: source.source_id, profile_id: "lab-recording", profile_name: "Lab recording", profile_revision: 3,
  state: "active", metadata_origin: "connected" };
const decoderSelection = { package_id: "example.motor", contribution_id: "main", version: "1.0.0" };
const channelRequirements = { description: "Use the motor channels for this decoder.", signal_type: "eeg", min_count: 2,
  required_names: ["C3"], default_names: ["C4", "C3"] };
const sourceConfig = (descriptor = source, revision = "source-1") => ({ acquisition_descriptor: clone(descriptor), source_revision: revision,
  source_binding: { ...sourceBinding, metadata_origin: descriptor ? "connected" : "unavailable" } });
const dependenciesEqual = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));

function harness(props) {
  const hooks = [];
  let cursor = 0, dirty = true, tree, effects = [];
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
    useRef(initial) { return hooks[cursor++] ??= { current: initial }; },
    useEffect(effect, deps) {
      const index = cursor++, previous = hooks[index];
      if (!previous || !dependenciesEqual(previous.deps, deps)) {
        hooks[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { hooks[index].cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  return {
    get tree() { return tree; }, props,
    rerender() { dirty = true; },
    async settle() {
      for (let round = 0; round < 30; round += 1) {
        if (dirty) {
          const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = internals.current;
          internals.current = dispatcher; cursor = 0; dirty = false;
          try { tree = ProcessingInput(props); } finally { internals.current = previous; }
          const pending = effects; effects = []; pending.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(dirty, false, "Component did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
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
function button(view, label) {
  const found = elements(view.tree).find((element) => element.type === "button" && (text(element) === label || element.props["aria-label"] === label));
  assert.ok(found, `Missing button ${label}`); return found;
}
function input(view, label, type = "input") {
  const all = elements(view.tree);
  const explicit = all.find((element) => element.type === type && element.props["aria-label"] === label);
  if (explicit) return explicit;
  const owner = all.find((element) => element.type === "label"
    && elements(element).some((child) => child.type === "span" && text(child) === label));
  const found = elements(owner).find((element) => element.type === type);
  assert.ok(found, `Missing ${type} ${label}`); return found;
}
async function click(view, label) {
  const target = button(view, label);
  assert.equal(!!target.props.disabled, false, `${label} is disabled`);
  await target.props.onClick(); await view.settle();
}
async function change(view, label, value, type = "input") {
  input(view, label, type).props.onChange({ target: { value } }); await view.settle();
}
async function withInput(action, { value = mapping("C3", "C4"), descriptor = source,
  requirements = undefined, selection = undefined } = {}) {
  const originalApi = { ...api };
  const calls = { change: [], preview: [] };
  api.get = async () => sourceConfig(descriptor);
  api.preview = async (processing_input, expected_source_revision, decoder_selection) => {
    calls.preview.push(clone({ processing_input, expected_source_revision, decoder_selection }));
    const outputs = processing_input.outputs.length ? processing_input.outputs : descriptor.channels.map((channel) => ({ name: channel.name, source_channel_id: channel.channel_id }));
    const indices = outputs.map((row) => descriptor.channels.findIndex((channel) => channel.channel_id === row.source_channel_id));
    return { plan: { source: clone(descriptor), source_indices: indices,
      output_channels: outputs.map((row, index) => ({ ...descriptor.channels[indices[index]], name: row.name,
        signal_type: row.signal_type ?? descriptor.channels[indices[index]]?.signal_type })),
      processing_input: clone(processing_input), config_revision: "accepted-1", fingerprint: "plan" },
      source_revision: expected_source_revision, warnings: [] };
  };
  const props = { value: clone(value), busy: false, configurationRevision: "accepted-1", decoderSelection: selection,
    channelRequirements: requirements, onChange: (next) => { calls.change.push(clone(next)); props.value = next; view.rerender(); } };
  const view = harness(props);
  try { await view.settle(); await action(view, calls); }
  finally { view.dispose(); Object.assign(api, originalApi); }
}

test("mapping orders stable channel IDs and decoder names without altering Acquisition metadata", async () => {
  const original = clone(source);
  await withInput(async (view, calls) => {
    await click(view, "Move input 2 up");
    await change(view, "Input 1 name", "MotorRight");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0], { processing_input: { schema_version: 2, outputs: [
      { source_channel_id: "sample-1", name: "MotorRight" }, output("C3"),
    ] }, expected_source_revision: "source-1", decoder_selection: decoderSelection });
    assert.match(text(view.tree), /C4 → MotorRight/);
    assert.match(text(view.tree), /sample values and events preserved/);
    assert.deepEqual(source, original);
    assert.doesNotMatch(text(view.tree), /reference|bipolar|exclusion|template|position|electrode|import/i);
  }, { selection: decoderSelection });
});

test("unknown Curry8 channel types are declared explicitly without changing the descriptor", async () => {
  const descriptor = { ...clone(source), source_id: "curry8", channels: source.channels.map((channel) => ({ ...channel, signal_type: "unknown" })) };
  const original = clone(descriptor);
  await withInput(async (view, calls) => {
    assert.equal(input(view, "Input 1 type", "select").props.value, "");
    assert.equal(calls.change.length, 0, "Decoder requirements must not infer EEG declarations");
    await change(view, "Input 1 type", "eeg", "select");
    assert.deepEqual(view.props.value.outputs[0], { ...output("C3"), signal_type: "eeg" });
    assert.match(text(view.tree), /Declared for this mapping/);
    await click(view, "Preview mapping");
    assert.equal(calls.preview[0].processing_input.outputs[0].signal_type, "eeg");
    await change(view, "Input 1 type", "", "select");
    assert.equal("signal_type" in view.props.value.outputs[0], false);
    assert.deepEqual(descriptor, original);
  }, { descriptor, requirements: channelRequirements });
});

test("reported channel types stay read-only and a conflicting declaration must be cleared explicitly", async () => {
  await withInput(async (view, calls) => {
    assert.equal(elements(view.tree).some((element) => element.props["aria-label"] === "Input 1 type"), false);
    assert.match(text(view.tree), /Reported by source/);
    assert.match(text(view.tree), /Saved ECG declaration conflicts/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    assert.equal(calls.change.length, 0);
    await click(view, "Use reported type");
    assert.equal("signal_type" in view.props.value.outputs[0], false);
    assert.equal(button(view, "Preview mapping").props.disabled, false);
  }, { value: { schema_version: 2, outputs: [{ ...output("C3"), signal_type: "ecg" }] } });
});

test("duplicate display names remain distinct through channel IDs and row labels", async () => {
  const descriptor = { ...clone(source), channels: [
    { channel_id: "amplifier-a", index: 0, name: "EEG", signal_type: "unknown" },
    { channel_id: "amplifier-b", index: 1, name: "EEG", signal_type: "unknown" },
  ], trigger_row_index: 2 };
  await withInput(async (view, calls) => {
    const options = elements(input(view, "Input 1 source", "select")).filter((element) => element.type === "option");
    assert.deepEqual(options.map((option) => [option.props.value, text(option)]), [["amplifier-a", "EEG (row 1)"], ["amplifier-b", "EEG (row 2)"]]);
    await change(view, "Input 1 source", "amplifier-b", "select");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].processing_input.outputs, [{ source_channel_id: "amplifier-b", name: "C3" }]);
    assert.equal(button(view, "Use plugin defaults").props.disabled, true, "Duplicate labels cannot choose a source ID implicitly");
  }, { descriptor, value: { schema_version: 2, outputs: [{ source_channel_id: "amplifier-a", name: "C3" }] },
    requirements: { default_names: ["EEG"] } });
});

test("plugin defaults require a connected descriptor with unique labels and never create source IDs from names", async () => {
  assert.deepEqual(emptyProcessingInput(["C3"]), mapping());
  await withInput(async (view, calls) => {
    assert.deepEqual(view.props.value.outputs, []);
    assert.equal(calls.change.length, 0);
    await click(view, "Use plugin defaults");
    assert.deepEqual(view.props.value, mapping("C4", "C3"));
    assert.equal(view.props.value.outputs.some((row) => "signal_type" in row), false);
    assert.match(text(view.tree), /Required channels: C3/);
  }, { value: mapping(), requirements: channelRequirements });
  await withInput(async (view) => {
    assert.equal(button(view, "Use plugin defaults").props.disabled, true);
    assert.match(text(view.tree), /require unambiguous channel names/);
  }, { value: mapping(), descriptor: null, requirements: channelRequirements });
});

test("profiles without a mapping create a fresh identity mapping without importing legacy settings", async () => {
  await withInput(async (view, calls) => {
    assert.equal(calls.change.length, 0);
    assert.doesNotMatch(text(view.tree), /import|reference|bipolar|template|montage/i);
    await click(view, "Create channel mapping");
    assert.deepEqual(calls.change, [mapping()]);
  }, { value: null, requirements: channelRequirements });
});

test("an unavailable descriptor keeps saved mappings editable and directs metadata retrieval to Acquisition", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /connect the data source and select Get Metadata/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await change(view, "Input 1 name", "Motor");
    assert.equal(view.props.value.outputs[0].name, "Motor");
  }, { descriptor: null });
});

test("All becomes an ordered subset and removing the last row never switches back to All", async () => {
  await withInput(async (view, calls) => {
    await click(view, "Use C4 only");
    assert.deepEqual(view.props.value, mapping("C4"));
    assert.equal(button(view, "Remove input 1").props.disabled, true);
    await change(view, "Add source channel", "sample-0", "select");
    await click(view, "Move input 2 up");
    assert.deepEqual(view.props.value, mapping("C3", "C4"));
    await click(view, "Remove input 2");
    assert.deepEqual(view.props.value, mapping("C3"));
    assert.equal(button(view, "Remove input 1").props.disabled, true);
    await click(view, "Use all channels");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview.at(-1).processing_input, mapping());
  }, { value: mapping() });
});

test("declaring a type in All materializes channel IDs without silently declaring other channels", async () => {
  const descriptor = { ...clone(source), channels: source.channels.map((channel) => ({ ...channel, signal_type: "unknown" })) };
  await withInput(async (view) => {
    await change(view, "Input 2 type", "eog", "select");
    assert.deepEqual(view.props.value.outputs, [output("C3"), { ...output("C4"), signal_type: "eog" }, output("M1")]);
  }, { value: mapping(), descriptor });
});

test("duplicate decoder names, source IDs, missing IDs and invalid types prevent a valid mapping", async () => {
  await withInput(async (view) => {
    const usedOption = elements(input(view, "Input 2 source", "select")).find((element) => element.type === "option" && element.props.value === "sample-0");
    assert.equal(usedOption.props.disabled, true);
    await change(view, "Input 2 name", "C3");
    assert.match(text(view.tree), /Algorithm channel names must be unique/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await change(view, "Input 2 name", "");
    assert.match(text(view.tree), /Input channel names cannot be empty/);
  });
  assert.deepEqual(processingInputErrors({ schema_version: 2, outputs: [output("C3"), { ...output("C3"), name: "Motor" }] }), ["Each Acquisition channel can be bound only once."]);
  assert.deepEqual(processingInputErrors({ schema_version: 2, outputs: [{ source_channel_id: "", name: "C3" }] }), ["Each input needs an Acquisition channel ID."]);
  assert.deepEqual(processingInputErrors({ schema_version: 2, outputs: [{ ...output("C3"), signal_type: "trigger" }] }), ["Declare a channel type from EEG, EOG, ECG, EMG, or MISC."]);
});

test("repairing a missing channel ID retains the decoder name and clears the previous source type declaration", async () => {
  await withInput(async (view, calls) => {
    assert.match(text(view.tree), /Channel old-id is unavailable/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await change(view, "Input 1 source", "sample-1", "select");
    assert.deepEqual(view.props.value.outputs, [{ source_channel_id: "sample-1", name: "Oz" }]);
    await click(view, "Preview mapping");
    assert.equal(calls.preview[0].processing_input.outputs[0].name, "Oz");
  }, { value: { schema_version: 2, outputs: [{ source_channel_id: "old-id", name: "Oz", signal_type: "eog" }] } });
});

test("preview failures retain mapping and report the error", async () => {
  await withInput(async (view) => {
    api.preview = async () => { throw new ApiError(400, "INVALID_MAPPING", "Decoder requires two EEG channels."); };
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Decoder requires two EEG channels/);
    assert.deepEqual(view.props.value, mapping("C3", "C4"));
  });
});

test("changing the edited decoder requirements invalidates a late preview without changing the draft", async () => {
  await withInput(async (view) => {
    let finish;
    api.preview = () => new Promise((resolve) => { finish = resolve; });
    await click(view, "Preview mapping");
    view.props.decoderSelection = { ...decoderSelection, version: "2.0.0" };
    view.props.channelRequirements = { min_count: 3 }; view.rerender(); await view.settle();
    finish({ source_revision: "source-1", plan: { source, source_indices: [0], output_channels: [source.channels[0]] }, warnings: [] });
    await view.settle();
    assert.doesNotMatch(text(view.tree), /sample values and events preserved/);
    assert.match(text(view.tree), /Minimum channel count: 3/);
    assert.deepEqual(view.props.value, mapping("C3", "C4"));
  }, { selection: decoderSelection, requirements: channelRequirements });
});

test("profile and preview APIs serialize only channel mappings and type declarations", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    return new Response(JSON.stringify({ data: {} }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const processing_input = { schema_version: 2, outputs: [{ ...output("C4"), signal_type: "eeg" }] };
  try {
    await pluginConfigApi.create("decoder", decoderSelection, "Motor", {}, processing_input);
    await pluginConfigApi.update({ profile_id: "motor", revision: 3 }, "Motor", {}, processing_input);
    await api.preview(processing_input, "source-1", decoderSelection);
    assert.deepEqual(requests[0].body.processing_input, processing_input);
    assert.deepEqual(requests[1].body, { name: "Motor", parameters: {}, expected_revision: 3, processing_input });
    assert.deepEqual(requests[2].body, { processing_input, expected_source_revision: "source-1", decoder_selection: decoderSelection });
    assert.equal(api.save, undefined);
  } finally { globalThis.fetch = originalFetch; }
});

const previewShown = (view) => /sample values and events preserved/.test(text(view.tree));
test("a changed Acquisition descriptor must be reviewed before previewing the unchanged mapping", async () => {
  await withInput(async (view, calls) => {
    const next = { ...clone(source), sample_rate: 1000, connection_id: "connection-2" };
    api.get = async () => sourceConfig(next, "source-2");
    await click(view, "Preview mapping");
    assert.equal(calls.preview.length, 0);
    assert.match(text(view.tree), /1000 Hz/);
    assert.match(text(view.tree), /source information changed/);
    assert.equal(previewShown(view), false);
    await click(view, "Preview mapping");
    assert.equal(calls.preview[0].expected_source_revision, "source-2");
    assert.equal(previewShown(view), true);
  });
});

test("refreshing a reconnected source invalidates preview and retains decoder mappings", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    api.get = async () => sourceConfig({ ...source, connection_id: "connection-2" }, "source-2");
    await click(view, "Refresh source");
    assert.equal(previewShown(view), false);
    assert.deepEqual(view.props.value, mapping("C3", "C4"));
  });
});

test("a late preview from a previous connection is discarded", async () => {
  await withInput(async (view) => {
    let finish;
    api.preview = () => new Promise((resolve) => { finish = resolve; });
    await click(view, "Preview mapping");
    api.get = async () => sourceConfig({ ...source, connection_id: "connection-2" }, "source-2");
    await click(view, "Refresh source");
    finish({ source_revision: "source-1", plan: { source, source_indices: [0], output_channels: [source.channels[0]] }, warnings: [] });
    await view.settle();
    assert.equal(previewShown(view), false);
    assert.match(text(view.tree), /source information changed/);
  });
});

test("a preview response cannot replace the displayed Acquisition descriptor", async () => {
  await withInput(async (view) => {
    api.preview = async () => ({ source_revision: "source-2", plan: { source: { ...source, sample_rate: 1000 }, source_indices: [], output_channels: [] }, warnings: [] });
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /500 Hz/);
    assert.doesNotMatch(text(view.tree), /1000 Hz/);
    assert.equal(previewShown(view), false);
  });
});

test("returning to the window refreshes the descriptor and removes its listener on unmount", async () => {
  const previousWindow = globalThis.window;
  const windowEvents = new EventTarget();
  globalThis.window = windowEvents;
  let reads = 0;
  try {
    await withInput(async (view) => {
      await click(view, "Preview mapping");
      api.get = async () => { reads += 1; return sourceConfig({ ...source, connection_id: "connection-2" }, "source-2"); };
      windowEvents.dispatchEvent(new Event("focus")); await view.settle();
      assert.equal(reads, 1);
      assert.equal(previewShown(view), false);
      assert.deepEqual(view.props.value, mapping("C3", "C4"));
    });
    windowEvents.dispatchEvent(new Event("focus"));
    assert.equal(reads, 1);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("an older descriptor request cannot overwrite a newer runtime configuration", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    let finish;
    api.get = () => new Promise((resolve) => { finish = resolve; });
    await click(view, "Refresh source");
    api.get = async () => sourceConfig({ ...source, sample_rate: 1000 }, "source-2");
    view.props.configurationRevision = "accepted-2"; view.rerender(); await view.settle();
    finish(sourceConfig()); await view.settle();
    assert.match(text(view.tree), /1000 Hz/);
    assert.equal(previewShown(view), false);
    assert.equal(button(view, "Preview mapping").props.disabled, false);
  });
});

test("All follows new descriptor channels while an explicit subset remains unchanged", async () => {
  await withInput(async (view, calls) => {
    const next = { ...clone(source), channels: [...source.channels, { channel_id: "sample-3", index: 3, name: "Oz", signal_type: "eeg" }], trigger_row_index: 4 };
    api.get = async () => sourceConfig(next, "source-2");
    await click(view, "Refresh source");
    assert.equal(input(view, "Input 4 name").props.value, "Oz");
    assert.deepEqual(view.props.value, mapping());
    assert.equal(calls.change.length, 0);
    await click(view, "Use Oz only");
    api.get = async () => sourceConfig(source, "source-3");
    await click(view, "Refresh source");
    assert.deepEqual(view.props.value.outputs, [{ source_channel_id: "sample-3", name: "Oz" }]);
    assert.match(text(view.tree), /Channel sample-3 is unavailable/);
  }, { value: mapping() });
});

test("changing a source display name preserves mappings to its stable channel ID", async () => {
  await withInput(async (view, calls) => {
    const next = clone(source); next.channels[0].name = "Renamed electrode";
    api.get = async () => sourceConfig(next, "source-2");
    await click(view, "Refresh source");
    assert.equal(input(view, "Input 1 source", "select").props.value, "sample-0");
    assert.equal(input(view, "Input 1 name").props.value, "C3");
    assert.deepEqual(view.props.value, mapping("C3", "C4"));
    assert.equal(calls.change.length, 0);
    assert.equal(button(view, "Preview mapping").props.disabled, false);
    assert.doesNotMatch(text(view.tree), /Channel sample-0 is unavailable/);
  });
});
