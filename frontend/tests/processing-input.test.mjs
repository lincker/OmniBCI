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
  const found = elements(view.tree).find((element) => element.type === "button" && text(element) === label);
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
async function withInput(action, { legacy = null, value = undefined, sourceMetadata = source } = {}) {
  const originalApi = { ...api };
  const calls = { change: [], preview: [] };
  api.get = async () => ({ source_metadata: clone(sourceMetadata), legacy_setup: clone(legacy) });
  api.preview = async (processing_input, metadata) => {
    calls.preview.push(clone({ processing_input, metadata }));
    return { plan: { source: clone(sourceMetadata), output_channels: [{ index: 0, name: "C4", signal_type: "eeg", unit: "V", reference: "M1" }],
      coefficients: [[0, 1, -1]], warnings: [], fingerprint: "plan" }, warnings: ["Reference M1 is not an output channel."] };
  };
  const props = { value: value === undefined ? { schema_version: 1, outputs: ["C3", "C4"], reference: { mode: "original", channels: [] }, excluded_channels: [] } : clone(value),
    busy: false, configurationRevision: "accepted-1", onChange: (next) => {
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
    await change(view, "Ordered output channels", "C4");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].processing_input.reference, { mode: "single", channels: ["M1"] });
    assert.deepEqual(calls.preview[0].processing_input.outputs, ["C4"]);
    assert.equal(calls.preview[0].metadata, undefined);
    assert.match(text(view.tree), /Reference M1 is not an output channel/);
    await change(view, "Ordered output channels", "C3");
    assert.doesNotMatch(text(view.tree), /Reference M1 is not an output channel/);
    assert.equal(elements(view.tree).some((element) => element.type === "button" && /Save|apply/.test(text(element))), false);
  });
});

test("comma-separated fields retain a typed delimiter while parsing channel names", async () => {
  await withInput(async (view) => {
    await change(view, "Ordered output channels", "C4,");
    assert.equal(input(view, "Ordered output channels").props.value, "C4,");
    await change(view, "Ordered output channels", "C4, C3");
    assert.deepEqual(view.props.value.outputs, ["C4", "C3"]);
    view.props.value = { ...view.props.value, outputs: ["C3", "C4"] }; view.rerender(); await view.settle();
    assert.equal(input(view, "Ordered output channels").props.value, "C3, C4", "Discard restores saved values");
  });
});

test("source facts are visible only as read-only context", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Available channels: C3, C4, M1/);
    assert.match(text(view.tree), /Original reference: Cz/);
    assert.match(text(view.tree), /managed in Acquisition/);
    assert.equal(elements(view.tree).filter((element) => element.type === "input").length, 2);
    assert.doesNotMatch(text(view.tree), /Electrode template|Source identifier/);
  });
});

test("offline input remains editable and directs source declarations to Acquisition", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Source information is unavailable/);
    assert.match(text(view.tree), /Offline preview does not validate a connected device/);
    await change(view, "Ordered output channels", "C4");
    assert.deepEqual(view.props.value.outputs, ["C4"]);
  }, { sourceMetadata: null });
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

test("preview failures preserve channel input and show the missing source channel", async () => {
  await withInput(async (view) => {
    await change(view, "Ordered output channels", "P3");
    api.preview = async () => { throw new ApiError(400, "INVALID_MAPPING", "Channel P3 is unavailable in the source montage."); };
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Channel P3 is unavailable/);
    assert.equal(input(view, "Ordered output channels").props.value, "P3");
  });
});

test("changing the acquisition selection invalidates a previous preview without changing the decoder draft", async () => {
  await withInput(async (view) => {
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /trigger preserved/);
    api.get = async () => ({ source_metadata: { ...source, channels: [{ index: 0, name: "P3", signal_type: "eeg" }] } });
    view.props.configurationRevision = "accepted-2"; view.rerender(); await view.settle();
    assert.doesNotMatch(text(view.tree), /trigger preserved/);
    assert.match(text(view.tree), /Available channels: P3/);
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
    await api.preview(processing_input, source);
    assert.deepEqual(requests[0].body.processing_input, processing_input);
    assert.equal(requests[1].body.expected_revision, 3);
    assert.deepEqual(requests[1].body.processing_input, processing_input);
    assert.equal(requests[2].url, "/api/v1/config/signal/preview");
    assert.deepEqual(requests[2].body, { processing_input, metadata: source });
    assert.equal(api.save, undefined);
  } finally { globalThis.fetch = originalFetch; }
});

test("preview resolves a refreshed acquisition montage instead of posting the mount-time snapshot", async () => {
  await withInput(async (view) => {
    assert.match(text(view.tree), /Available channels: C3, C4, M1/);
    const refreshed = { ...clone(source), connection_id: "connection-2", original_reference: "M2",
      channels: ["P3", "P4", "M1"].map((name, index) => ({ index, name, signal_type: "eeg", unit: "V" })) };
    api.preview = async (value, suppliedMetadata) => {
      assert.equal(suppliedMetadata, undefined, "The backend must resolve the current acquisition montage");
      return { plan: { source: refreshed, output_channels: refreshed.channels, coefficients: [], fingerprint: "new-plan", warnings: [] }, warnings: [] };
    };
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Available channels: P3, P4, M1/);
    assert.match(text(view.tree), /Original reference: M2/);
    assert.match(text(view.tree), /trigger preserved/);
    await change(view, "Ordered output channels", "P4");
    assert.match(text(view.tree), /Available channels: P3, P4, M1/, "The current source remains visible after editing the input");
  }, { value: { schema_version: 1, outputs: [], reference: { mode: "original", channels: [] }, excluded_channels: [] } });
});
