/** Channel editing keeps drafts, saved settings, and runtime selection separate. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { SignalSetup } = await server.ssrLoadModule("/src/SignalSetup.tsx");
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
          try { tree = SignalSetup(props); } finally { internals.current = previous; }
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
async function withSetup(action, { offline = false, saved = true } = {}) {
  const originalApi = { ...api }, originalPlugins = { ...pluginConfigApi };
  const data = { setup: saved ? clone(setup) : null, revision: "saved-1", selected_source_id: "file_reader", accepted_setup: clone(setup), active_setup: clone(setup),
    readiness: { ready: false, receipt: null } };
  const calls = { save: [], preview: [], review: [], metadata: [] };
  api.get = async () => clone(data);
  api.metadata = async (refresh) => { calls.metadata.push(refresh); if (offline) throw new Error("Source disconnected"); return clone(source); };
  api.templates = async () => ({ templates: ["standard_1020"] });
  api.template = async () => ({ name: "standard_1020", positions: { C3: [0, 0, 0], C4: [0, 0, 0] }, unit: "m", coordinate_frame: "head", provider: "MNE" });
  api.save = async (next, revision) => {
    calls.save.push(clone({ setup: next, expected_revision: revision }));
    data.setup = clone(next); data.revision = "saved-2"; return clone(data);
  };
  api.preview = async (next, metadata) => {
    calls.preview.push(clone({ setup: next, metadata }));
    return { plan: { source: clone(metadata ?? next.manual_metadata), output_channels: [{ index: 0, name: "C4", signal_type: "eeg", unit: "V", reference: "M1" }], coefficients: [], warnings: [], fingerprint: "plan" }, warnings: ["Reference M1 is not an output channel."] };
  };
  pluginConfigApi.activation = async () => ({ revision: "accepted-1", bindings: [{ slot: "acquisition.source", profile_id: "source-profile" }] });
  const props = { runtime: { blocked: false, status: { running: true, accepted_revision: "accepted-1", applied_revision: "accepted-1" },
    review: async (body) => { calls.review.push(clone(body)); } }, onDirtyChange: () => {}, onBusyChange: () => {} };
  const view = harness(props);
  try { await view.settle(); await action(view, calls, data); }
  finally { view.dispose(); Object.assign(api, originalApi); Object.assign(pluginConfigApi, originalPlugins); }
}

test("saving reordered channels leaves the active setup unchanged until scoped review", async () => {
  await withSetup(async (view, calls, data) => {
    await change(view, "Ordered output electrodes", "C4, C3");
    assert.equal(!!button(view, "Review and apply channel setup").props.disabled, true);
    await click(view, "Save channel setup");
    assert.deepEqual(calls.save[0].setup.outputs, ["C4", "C3"]);
    assert.equal(calls.save[0].expected_revision, "saved-1");
    assert.deepEqual(data.active_setup.outputs, ["C3", "C4"]);
    assert.match(text(view.tree), /Saved channel setup has changes that are not selected/);
    assert.equal(calls.review.length, 0);
    await click(view, "Review and apply channel setup");
    assert.deepEqual(calls.review, [{ bindings: [{ slot: "acquisition.source", profile_id: "source-profile" }],
      expected_revision: "accepted-1", refresh_slots: [], include_host_settings: false, include_signal_setup: true }]);
  });
});

test("reference-only dependencies and input order reach the side-effect-free preview", async () => {
  await withSetup(async (view, calls) => {
    await change(view, "Reference mode", "single", "select");
    await change(view, "Reference channels", "M1");
    await change(view, "Ordered output electrodes", "C4");
    await click(view, "Preview mapping");
    assert.deepEqual(calls.preview[0].setup.reference, { mode: "single", channels: ["M1"] });
    assert.equal(calls.preview[0].metadata.connection_id, "connection-1");
    assert.match(text(view.tree), /Reference M1 is not an output channel/);
    assert.equal(calls.save.length, 0); assert.equal(calls.review.length, 0);
    await change(view, "Ordered output electrodes", "C3");
    assert.doesNotMatch(text(view.tree), /Reference M1 is not an output channel/);
  });
});

test("comma-separated fields retain a typed delimiter while parsing channel names", async () => {
  await withSetup(async (view, calls) => {
    await change(view, "Ordered output electrodes", "C4,");
    assert.equal(input(view, "Ordered output electrodes").props.value, "C4,");
    await change(view, "Ordered output electrodes", "C4, C3");
    await click(view, "Save channel setup");
    assert.deepEqual(calls.save[0].setup.outputs, ["C4", "C3"]);
  });
});

test("a save conflict preserves edits and the old expected revision", async () => {
  await withSetup(async (view, calls, data) => {
    await change(view, "Row 1 electrode", "P3");
    data.revision = "changed-elsewhere";
    view.props.runtime.status = { ...view.props.runtime.status, accepted_revision: "accepted-2" };
    view.rerender(); await view.settle();
    api.save = async (_, revision) => { assert.equal(revision, "saved-1"); throw new ApiError(409, "REVISION_CONFLICT", "Settings changed."); };
    await click(view, "Save channel setup");
    assert.equal(input(view, "Row 1 electrode").props.value, "P3");
    assert.match(text(view.tree), /Your edits are preserved/);
    assert.equal(calls.review.length, 0);
  });
});

test("offline source declarations are managed in the acquisition profile", async () => {
  await withSetup(async (view) => {
    assert.match(text(view.tree), /Acquisition profile/);
    assert.match(text(view.tree), /Offline preview does not validate a connected device/);
    assert.equal(elements(view.tree).some((element) => element.type === "button"
      && text(element) === "Add manual source declaration"), false);
  }, { offline: true, saved: false });
});

test("bipolar pairs preserve their positive and negative inputs when saved", async () => {
  await withSetup(async (view, calls) => {
    await change(view, "Reference mode", "bipolar", "select");
    await click(view, "Add bipolar pair");
    await change(view, "Pair 1 name", "C3-C4");
    await change(view, "Pair 1 positive", "C3");
    await change(view, "Pair 1 negative", "C4");
    await click(view, "Save channel setup");
    assert.deepEqual(calls.save[0].setup.outputs, [{ name: "C3-C4", positive: "C3", negative: "C4" }]);
  });
});

test("preview failures show the source conflict and preserve the draft", async () => {
  await withSetup(async (view) => {
    await change(view, "Row 1 electrode", "P3");
    api.preview = async () => { throw new ApiError(400, "INVALID_MAPPING", "Source row 1 no longer matches C3."); };
    await click(view, "Preview mapping");
    assert.match(text(view.tree), /Source row 1 no longer matches C3/);
    assert.equal(input(view, "Row 1 electrode").props.value, "P3");
  });
});

test("refreshing reordered source rows exposes the mismatch without rebinding the draft", async () => {
  await withSetup(async (view, calls) => {
    const changed = clone(source);
    changed.channels[0].name = "C4"; changed.channels[1].name = "C3";
    api.metadata = async () => changed;
    await click(view, "Refresh source metadata");
    assert.equal(input(view, "Row 1 electrode").props.value, "C3");
    assert.match(text(view.tree), /Expected C3/);
    await click(view, "Preview mapping");
    assert.equal(calls.preview[0].setup.assignments[0].expected_name, "C3");
    assert.equal(calls.preview[0].metadata.channels[0].name, "C4");
  });
});
