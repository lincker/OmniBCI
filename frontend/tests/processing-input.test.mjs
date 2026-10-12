/** Decoder input previews and explicit imports preserve profile-owned drafts. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { ProcessingInput, processingInputErrors } = await server.ssrLoadModule("/src/ProcessingInput.tsx");
const { signalConfigApi: api } = await server.ssrLoadModule("/src/api/signal-config.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");
const clone = (value) => structuredClone(value);
const source = { source_id: "file_reader", sample_rate: 500, trigger_row_index: 3, provenance: "file-reported", connection_id: "connection-1",
  channels: ["C3", "C4", "M1"].map((name, index) => ({ index, name, signal_type: "eeg", unit: "V" })), original_reference: "Cz" };
const setup = { schema_version: 1, source_id: source.source_id,
  assignments: source.channels.map((channel) => ({ source_index: channel.index, expected_name: channel.name, electrode: channel.name })),
  outputs: ["C3", "C4"], reference: { mode: "original", channels: [] }, excluded_channels: [] };
const sourceBinding = { source_id: source.source_id, profile_id: "lab-recording", profile_name: "Lab recording", profile_revision: 3,
  state: "selected", metadata_origin: "profile" };
const decoderSelection = { package_id: "example.motor", contribution_id: "main", version: "1.0.0" };
const channelRequirements = { description: "Use the motor channels for this decoder.", signal_type: "eeg", min_count: 2,
  required_names: ["C3"], default_names: ["C4", "C3"] };
const sourceConfig = (metadata = source, revision = "source-1") => ({ source_metadata: clone(metadata), source_revision: revision,
  source_binding: { ...sourceBinding, metadata_origin: metadata ? "profile" : "unavailable" } });
const dependenciesEqual = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));

function harness(props) {
  const hooks = [];
  let cursor = 0, dirty = true, tree, effects = [];
  const dispatcher = {
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
async function withInput(action, { legacy = null, value = undefined, sourceMetadata = source,
  requirements = undefined, selection = undefined } = {}) {
  const originalApi = { ...api };
  const calls = { change: [], preview: [] };
  api.get = async () => ({ ...sourceConfig(sourceMetadata), legacy_setup: clone(legacy) });
  api.preview = async (processing_input, expected_source_revision, decoder_selection) => {
    calls.preview.push(clone({ processing_input, expected_source_revision, decoder_selection }));
    return { plan: { source: clone(sourceMetadata), output_channels: [{ index: 0, name: "C4", signal_type: "eeg", unit: "V", reference: "M1" }],
      coefficients: [[0, 1, -1]], warnings: [], fingerprint: "plan" }, source_revision: expected_source_revision,
      warnings: ["Reference M1 is not an output channel."] };
  };
  const props = { value: value === undefined ? { schema_version: 1, outputs: ["C3", "C4"], reference: { mode: "original", channels: [] }, excluded_channels: [] } : clone(value),
    busy: false, configurationRevision: "accepted-1", decoderSelection: selection, channelRequirements: requirements, onChange: (next) => {
      calls.change.push(clone(next)); props.value = next; view.rerender();
    } };
  const view = harness(props);
  try { await view.settle(); await action(view, calls); }
  finally { view.dispose(); Object.assign(api, originalApi); }
}

test("ordered channels and reference-only dependencies reach the preview without saving", async () => {
  await withInput(async (view, calls) => {
    await change(view, "Reference mode", "single", "select");
    await change(view, "Reference channels", "M1");
    await click(view, "Remove input 1");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].processing_input.reference, { mode: "single", channels: ["M1"] });
    assert.deepEqual(calls.preview[0].processing_input.outputs, ["C4"]);
    assert.equal(calls.preview[0].expected_source_revision, "source-1");
    assert.match(text(view.tree), /Reference M1 is not an output channel/);
    await change(view, "Input 1 source", "C3", "select");
    await change(view, "Input 1 name", "C3");
    assert.doesNotMatch(text(view.tree), /Reference M1 is not an output channel/);
    assert.equal(elements(view.tree).some((element) => element.type === "button" && /Save|apply/.test(text(element))), false);
  });
});

test("reference fields retain a typed delimiter while parsing channel names", async () => {
  await withInput(async (view) => {
    await change(view, "Reference mode", "average", "select");
    await change(view, "Reference channels", "C4,");
    assert.equal(input(view, "Reference channels").props.value, "C4,");
    await change(view, "Reference channels", "C4, C3");
    assert.deepEqual(view.props.value.reference.channels, ["C4", "C3"]);
    view.props.value = { ...view.props.value, reference: { mode: "average", channels: ["M1"] } }; view.rerender(); await view.settle();
    assert.equal(input(view, "Reference channels").props.value, "M1", "Discard restores saved values");
  });
});

test("source facts are visible only as read-only context", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Acquisition channels3 available/);
    assert.match(text(view.tree), /Original reference: Cz/);
    assert.match(text(view.tree), /managed in Acquisition/);
    assert.match(text(view.tree), /Data source: Lab recording · file_reader · profile revision 3/);
    assert.match(text(view.tree), /Selected for next engine start · Saved Acquisition profile information/);
    assert.match(text(view.tree), /automatically uses the current Acquisition source/);
    assert.equal(elements(view.tree).some((element) => element.props["aria-label"] === "Source identifier"), false);
    assert.doesNotMatch(text(view.tree), /Electrode template|Source identifier/);
  });
});

test("plugin requirements describe final input without replacing a saved all-source selection", async () => {
  await withInput(async (view, calls) => {
    assert.match(text(view.tree), /Use the motor channels for this decoder/);
    assert.match(text(view.tree), /Signal type: eeg · Minimum channel count: 2/);
    assert.match(text(view.tree), /Required channels: C3/);
    assert.match(text(view.tree), /Plugin default channels: C4, C3/);
    assert.match(text(view.tree), /This set enters Epoch before filters and Decoder/);
    assert.match(text(view.tree), /All follows the current Acquisition source/);
    assert.deepEqual(view.props.value.outputs, []);
    assert.equal(calls.change.length, 0);
    assert.equal(elements(view.tree).some((element) => element.props["aria-label"] === "Source identifier"), false);
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].decoder_selection, decoderSelection);
  }, { requirements: channelRequirements, selection: decoderSelection,
    value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("plugin defaults are explicit and preserve reference and exclusions", async () => {
  await withInput(async (view, calls) => {
    await change(view, "Reference mode", "single", "select");
    await change(view, "Reference channels", "M1");
    await change(view, "Excluded channels", "C3");
    await click(view, "Use plugin defaults");
    assert.deepEqual(view.props.value.outputs, ["C4", "C3"]);
    assert.deepEqual(view.props.value.reference, { mode: "single", channels: ["M1"] });
    assert.deepEqual(view.props.value.excluded_channels, ["C3"]);
    assert.equal(calls.preview.length, 0);
    assert.equal(input(view, "Input 1 name").props.value, "C4");
    assert.equal(input(view, "Input 2 name").props.value, "C3");
  }, { requirements: channelRequirements });
});

test("plugin defaults do not discard bipolar derivations", async () => {
  const value = { schema_version: 1, outputs: [{ name: "C3-C4", positive: "C3", negative: "C4" }],
    reference: { mode: "bipolar", channels: [] }, excluded_channels: [] };
  await withInput(async (view, calls) => {
    assert.equal(button(view, "Use plugin defaults").props.disabled, true);
    assert.match(text(view.tree), /Choose a non-bipolar reference mode/);
    assert.deepEqual(view.props.value, value);
    assert.equal(calls.change.length, 0);
  }, { requirements: channelRequirements, value });
});

test("plugins without a declaration do not invent requirements or named defaults", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /This plugin has not declared channel requirements/);
    assert.equal(elements(view.tree).some((element) => element.type === "button" && text(element) === "Use plugin defaults"), false);
    assert.doesNotMatch(text(view.tree), /Minimum channel count/);
  });
});

test("changing the edited decoder or its requirements rejects stale previews and preserves the draft", async () => {
  await withInput(async (view, calls) => {
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].decoder_selection, decoderSelection);
    const savedPreview = api.preview;
    let resolvePreview;
    api.preview = () => new Promise((resolve) => { resolvePreview = resolve; });
    await click(view, "Preview mapping");
    view.props.decoderSelection = { ...decoderSelection, version: "2.0.0" };
    view.rerender(); await view.settle();
    resolvePreview({ source_revision: "source-1", plan: { source, output_channels: source.channels,
      coefficients: [], fingerprint: "stale-decoder", warnings: [] }, warnings: [] });
    await view.settle();
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.deepEqual(view.props.value.outputs, ["C3", "C4"]);
    api.preview = savedPreview;
    await click(view, "Preview mapping");
    assert.equal(calls.preview.at(-1).decoder_selection.version, "2.0.0");
    assert.match(text(view.tree), /trigger preserved/);
    view.props.channelRequirements = { ...channelRequirements, min_count: 3 };
    view.rerender(); await view.settle();
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.match(text(view.tree), /Minimum channel count: 3/);
  }, { requirements: channelRequirements, selection: decoderSelection });
});

test("offline input remains editable and directs source declarations to Acquisition", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Source information is unavailable/);
    assert.match(text(view.tree), /Offline preview does not validate a connected device/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await click(view, "Remove input 1");
    assert.deepEqual(view.props.value.outputs, ["C4"]);
  }, { sourceMetadata: null });
});

test("a source montage conflict keeps its Acquisition context visible and explains why preview is unavailable", async () => {
  await withInput(async (view) => {
    api.get = async () => ({ ...sourceConfig(null, "source-conflict"),
      source_error: "Manual montage conflicts with the source channel unit for C3." });
    await click(view, "Refresh source");
    assert.match(text(view.tree), /Data source: Lab recording · file_reader · profile revision 3/);
    const alert = elements(view.tree).find((element) => element.props.role === "alert");
    assert.equal(text(alert), "Manual montage conflicts with the source channel unit for C3.");
    assert.doesNotMatch(text(view.tree), /Read the source or fill its missing montage information/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await click(view, "Remove input 1");
    assert.deepEqual(view.props.value.outputs, ["C4"]);
  });
});

test("legacy input is adopted explicitly and source declarations are not copied", async () => {
  await withInput(async (view, calls) => {
    assert.equal(calls.change.length, 0);
    assert.match(text(view.tree), /Move that information to the Acquisition profile/);
    assert.equal(elements(view.tree).some((element) => element.type === "input"), false);
    await click(view, "Import previous channel selection");
    assert.deepEqual(calls.change[0], { schema_version: 1, outputs: ["C3", "C4"], reference: { mode: "original", channels: [] }, excluded_channels: [] });
    assert.match(text(view.tree), /Move that information to the Acquisition profile/, "The warning remains visible while saving the imported draft");
    assert.equal("assignments" in view.props.value, false);
    assert.equal("source_id" in view.props.value, false);
  }, { value: null, legacy: setup });
});

test("bipolar pairs preserve their positive and negative inputs", async () => {
  await withInput(async (view) => {
    await change(view, "Reference mode", "bipolar", "select");
    await click(view, "Add bipolar pair");
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await change(view, "Pair 1 name", "C3-C4");
    await change(view, "Pair 1 positive", "C3");
    await change(view, "Pair 1 negative", "C4");
    assert.deepEqual(view.props.value.outputs, [{ name: "C3-C4", positive: "C3", negative: "C4" }]);
    assert.deepEqual(processingInputErrors(view.props.value), []);
  });
});

test("preview failures preserve bindings and show the missing source channel", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Source P3 is unavailable/);
    api.preview = async () => { throw new ApiError(400, "INVALID_MAPPING", "Channel P3 is unavailable in the source montage."); };
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Channel P3 is unavailable/);
    assert.equal(input(view, "Input 1 source", "select").props.value, "P3");
  }, { value: { schema_version: 1, outputs: ["P3"], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("changing the acquisition selection invalidates a previous preview without changing the decoder draft", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /trigger preserved/);
    api.get = async () => sourceConfig({ ...source, channels: [{ index: 0, name: "P3", signal_type: "eeg" }] }, "source-2");
    view.props.configurationRevision = "accepted-2"; view.rerender(); await view.settle();
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.match(text(view.tree), /Acquisition channels1 available/);
    assert.match(text(view.tree), /Source C3 is unavailable/);
    assert.deepEqual(view.props.value.outputs, ["C3", "C4"]);
  });
});

test("profile API stores decoder input with revision checks and preview has no global save endpoint", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    return new Response(JSON.stringify({ data: {} }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const processing_input = { schema_version: 1, outputs: ["C4", "C3"], reference: { mode: "single", channels: ["M1"] }, excluded_channels: [] };
  const selection = { package_id: "example.decoder", contribution_id: "main", version: "1.0.0" };
  try {
    await pluginConfigApi.create("decoder", selection, "Motor", {}, undefined, processing_input);
    await pluginConfigApi.update({ profile_id: "motor", revision: 3 }, "Motor", {}, undefined, processing_input);
    await api.preview(processing_input, "source-1", selection);
    assert.deepEqual(requests[0].body.processing_input, processing_input);
    assert.equal(requests[1].body.expected_revision, 3);
    assert.deepEqual(requests[1].body.processing_input, processing_input);
    assert.equal(requests[2].url, "/api/v1/config/signal/preview");
    assert.deepEqual(requests[2].body, { processing_input, expected_source_revision: "source-1", decoder_selection: selection });
    assert.equal(api.save, undefined);
  } finally { globalThis.fetch = originalFetch; }
});

test("preview refreshes changed Acquisition context and requires review before resolving the unchanged draft", async () => {
  await withInput(async (view, calls) => {
    assert.match(text(view.tree), /Acquisition channels3 available/);
    const refreshed = { ...clone(source), connection_id: "connection-2", original_reference: "M2",
      channels: ["P3", "P4", "M1"].map((name, index) => ({ index, name, signal_type: "eeg", unit: "V" })) };
    api.get = async () => sourceConfig(refreshed, "source-2");
    await click(view, "Preview mapping");
    assert.equal(calls.preview.length, 0, "A changed source needs an explicit preview against its displayed context");
    assert.match(text(view.tree), /Acquisition channels3 available/);
    assert.match(text(view.tree), /Original reference: M2/);
    assert.match(text(view.tree), /source information changed/);
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    await click(view, "Preview mapping");
    assert.equal(calls.preview[0].expected_source_revision, "source-2");
    assert.match(text(view.tree), /trigger preserved/);
    await click(view, "Use P4 only");
    assert.match(text(view.tree), /Acquisition channels3 available/, "The current source remains visible after editing the input");
  }, { value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("refreshing a reconnected source invalidates the preview without changing the decoder draft", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    const draft = clone(view.props.value);
    api.get = async () => ({ ...sourceConfig({ ...source, connection_id: "connection-2" }, "source-2"),
      source_binding: { ...sourceBinding, state: "active", metadata_origin: "connected" } });
    await click(view, "Refresh source");
    assert.match(text(view.tree), /Applied Acquisition source · Connected source information/);
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.deepEqual(view.props.value, draft);
  });
});

test("a source refresh rejects a late preview from the previous connection", async () => {
  await withInput(async (view) => {
    let resolvePreview;
    api.preview = () => new Promise((resolve) => { resolvePreview = resolve; });
    await click(view, "Preview mapping");
    assert.equal(typeof resolvePreview, "function");
    api.get = async () => sourceConfig({ ...source, connection_id: "connection-2" }, "source-2");
    await click(view, "Refresh source");
    resolvePreview({ source_revision: "source-1", plan: { source, output_channels: source.channels, coefficients: [], fingerprint: "stale", warnings: [] }, warnings: [] });
    await view.settle();
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.match(text(view.tree), /source information changed/);
    assert.deepEqual(view.props.value.outputs, ["C3", "C4"]);
  });
});

test("a preview response cannot replace the displayed Acquisition context", async () => {
  await withInput(async (view) => {
    api.preview = async () => ({ source_revision: "source-2", plan: { source: { ...source, original_reference: "Pz" },
      output_channels: source.channels, coefficients: [], fingerprint: "other-source", warnings: [] }, warnings: [] });
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Original reference: Cz/);
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.match(text(view.tree), /source information changed/);
  });
});

test("returning to the window refreshes Acquisition context and removes its listener on unmount", async () => {
  const previousWindow = globalThis.window;
  const windowEvents = new EventTarget();
  globalThis.window = windowEvents;
  let reads = 0;
  try {
    await withInput(async (view) => {
      await click(view, "Preview mapping");
      api.get = async () => { reads += 1; return sourceConfig({ ...source, connection_id: "connection-2" }, "source-2"); };
      windowEvents.dispatchEvent(new Event("focus"));
      await view.settle();
      assert.equal(reads, 1);
      assert.doesNotMatch(text(view.tree), /trigger preserved/);
      assert.match(text(view.tree), /source information changed/);
      assert.deepEqual(view.props.value.outputs, ["C3", "C4"]);
    });
    windowEvents.dispatchEvent(new Event("focus"));
    assert.equal(reads, 1, "Unmount removes the focus listener");
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("an older source request cannot overwrite a newer configuration or restore its preview", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    let resolveSource;
    api.get = () => new Promise((resolve) => { resolveSource = resolve; });
    await click(view, "Refresh source");
    api.get = async () => sourceConfig({ ...source, original_reference: "Pz" }, "source-2");
    view.props.configurationRevision = "accepted-2"; view.rerender(); await view.settle();
    resolveSource(sourceConfig()); await view.settle();
    assert.match(text(view.tree), /Original reference: Pz/);
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.equal(button(view, "Preview mapping").props.disabled, false);
  });
});

test("All can become an ordered subset and removing the last input never means All", async () => {
  await withInput(async (view, calls) => {
    assert.deepEqual(view.props.value.outputs, []);
    await click(view, "Use C4 only");
    assert.deepEqual(view.props.value.outputs, ["C4"]);
    assert.equal(button(view, "Remove input 1").props.disabled, true);
    await click(view, "Add C3");
    assert.deepEqual(view.props.value.outputs, ["C4", "C3"]);
    await click(view, "Move input 2 up");
    assert.deepEqual(view.props.value.outputs, ["C3", "C4"]);
    await click(view, "Remove input 2");
    assert.deepEqual(view.props.value.outputs, ["C3"]);
    assert.equal(button(view, "Remove input 1").props.disabled, true);
    assert.match(text(view.tree), /Keep at least one selected input/);
    await click(view, "Use all channels");
    assert.deepEqual(view.props.value.outputs, []);
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview.at(-1).processing_input.outputs, []);
  }, { value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("required slots bind an Acquisition source under an explicit algorithm name before Epoch", async () => {
  await withInput(async (view, calls) => {
    assert.match(text(view.tree), /Required channels to bind/);
    await change(view, "Bind required Oz", "C4", "select");
    assert.deepEqual(view.props.value.outputs, ["C3", { name: "Oz", source: "C4" }]);
    assert.equal(elements(view.tree).some((element) => element.props["aria-label"] === "Bind required Oz"), false);
    const left = elements(view.tree).find((element) => element.props["aria-label"] === "Available Acquisition channels");
    assert.match(text(left), /C4Row 2 · eeg · V→ Oz/);
    const flow = elements(view.tree).find((element) => element.props["aria-label"] === "Processing data flow");
    assert.equal(text(flow), "AcquisitionChannel bindingEpochFilters (optional)Decoder");
    assert.match(text(view.tree), /Epoch timing and baseline settings remain/);
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview.at(-1).processing_input.outputs, ["C3", { name: "Oz", source: "C4" }]);
    assert.deepEqual(source.channels.map((channel) => channel.name), ["C3", "C4", "M1"]);
  }, { requirements: { ...channelRequirements, required_names: ["C3", "Oz"], default_names: [] },
    value: { schema_version: 1, outputs: ["C3"], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("binding an already selected source to a required slot replaces its name once", async () => {
  await withInput(async (view) => {
    await change(view, "Bind required Oz", "C4", "select");
    assert.deepEqual(view.props.value.outputs, ["C3", { name: "Oz", source: "C4" }, "M1"]);
    assert.equal(input(view, "Input 2 source", "select").props.value, "C4");
    assert.equal(input(view, "Input 2 name").props.value, "Oz");
    await change(view, "Input 2 name", "C4");
    assert.deepEqual(view.props.value.outputs, ["C3", "C4", "M1"]);
    assert.deepEqual(processingInputErrors(view.props.value), []);
  }, { requirements: { ...channelRequirements, required_names: ["Oz"], default_names: [] },
    value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});

test("duplicate names and sources cannot produce a valid binding preview", async () => {
  await withInput(async (view) => {
    const secondSource = input(view, "Input 2 source", "select");
    const usedOption = elements(secondSource).find((element) => element.type === "option" && element.props.value === "C3");
    assert.equal(usedOption.props.disabled, true);
    assert.equal(button(view, "Add C3").props.disabled, true);
    await change(view, "Input 2 name", "C3");
    assert.match(text(view.tree), /Algorithm channel names must be unique/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    await change(view, "Input 2 name", "");
    assert.match(text(view.tree), /Input channel names cannot be empty/);
    assert.equal(button(view, "Preview mapping").props.disabled, true);
    const duplicateSources = { ...view.props.value, outputs: ["C3", { name: "Oz", source: "C3" }] };
    assert.deepEqual(processingInputErrors(duplicateSources), ["Each Acquisition channel can be bound only once."]);
  });
});

test("a missing aliased source can be repaired without renaming its algorithm input", async () => {
  const value = { schema_version: 1, outputs: [{ name: "Oz", source: "Old7" }],
    reference: { mode: "single", channels: ["M1"] }, excluded_channels: [] };
  await withInput(async (view, calls) => {
    assert.match(text(view.tree), /Source Old7 is unavailable/);
    assert.equal(calls.change.length, 0);
    await change(view, "Input 1 source", "C4", "select");
    assert.deepEqual(view.props.value.outputs, [{ name: "Oz", source: "C4" }]);
    assert.deepEqual(view.props.value.reference, value.reference);
    assert.doesNotMatch(text(view.tree), /Source Old7 is unavailable/);
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview.at(-1).processing_input.outputs, [{ name: "Oz", source: "C4" }]);
  }, { value });
});

test("All follows refreshed source channels while an explicit subset remains unchanged", async () => {
  await withInput(async (view, calls) => {
    const refreshed = { ...clone(source), channels: [...source.channels, { index: 3, name: "Oz", signal_type: "eeg", unit: "V" }], trigger_row_index: 4 };
    api.get = async () => sourceConfig(refreshed, "source-2");
    await click(view, "Refresh source");
    assert.equal(input(view, "Input 4 name").props.value, "Oz");
    assert.deepEqual(view.props.value.outputs, []);
    assert.equal(calls.change.length, 0);
    await click(view, "Use Oz only");
    api.get = async () => sourceConfig(source, "source-3");
    await click(view, "Refresh source");
    assert.deepEqual(view.props.value.outputs, ["Oz"]);
    assert.match(text(view.tree), /Source Oz is unavailable/);
  }, { value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});
