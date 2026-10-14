/** Host settings save independently of plugin profiles and runtime application. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { ConfigCard } = await server.ssrLoadModule("/src/ConfigCard.tsx");
const sameDependencies = (a, b) => !!a && !!b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));

function harness(props) {
  const hooks = [];
  let cursor = 0, pending = true, tree;
  let effects = [];
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = { value: typeof initial === "function" ? initial() : initial };
      return [hooks[i].value, (next) => {
        const value = typeof next === "function" ? next(hooks[i].value) : next;
        if (!Object.is(value, hooks[i].value)) { hooks[i].value = value; pending = true; }
      }];
    },
    useRef(initial) { return hooks[cursor++] ??= { current: initial }; },
    useCallback(callback, deps) {
      const i = cursor++;
      if (!sameDependencies(hooks[i]?.deps, deps)) hooks[i] = { value: callback, deps };
      return hooks[i].value;
    },
    useEffect(effect, deps) {
      const i = cursor++;
      if (!sameDependencies(hooks[i]?.deps, deps)) {
        const previous = hooks[i];
        hooks[i] = { deps };
        effects.push(() => { previous?.cleanup?.(); hooks[i].cleanup = effect(); });
      }
    },
  };
  return {
    get tree() { return tree; },
    async settle() {
      for (let round = 0; round < 40; round += 1) {
        if (pending) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          cursor = 0; pending = false; current.current = dispatcher;
          try { tree = ConfigCard(props); } finally { current.current = previous; }
          const queued = effects; effects = []; queued.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(pending, false, "Settings component did not settle");
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
function button(tree, label) {
  const found = elements(tree).find((item) => item.type === "button" && text(item) === label);
  assert.ok(found, `Missing button: ${label}`);
  return found;
}

function input(component, key) {
  const found = elements(component.tree).find((item) => item.type === "input" && item.props.id === `setting-${key}`);
  assert.ok(found, `Missing setting: ${key}`);
  return found;
}
async function change(component, key, value) {
  const control = input(component, key);
  assert.equal(control.props.disabled, false);
  control.props.onChange({ target: { value } });
  await component.settle();
}
async function click(component, label) {
  const target = button(component.tree, label);
  assert.equal(target.props.disabled, false);
  target.props.onClick();
  await component.settle();
}
const dialog = (component) => elements(component.tree).find((item) => item.type.name === "ConfigDialog");

async function withSettings(action, overrides = {}) {
  const cfg = { revision: "a".repeat(64), entries: [
    { key: "ALGORITHM.baseline_duration", type: "float", value: 0.5, requires: "engine_restart", description: "Baseline duration in seconds" },
    { key: "NEUROENGINE.queue_size", type: "int", value: 32, requires: "engine_restart", description: "Queue capacity" },
  ] };
  const activation = { revision: "accepted", bindings: [
    { slot: "acquisition.source", profile_id: "source", profile_revision: 3 },
    { slot: "processing.filter.first", profile_id: "filter", profile_revision: 2 },
    { slot: "processing.filter.second", profile_id: "filter", profile_revision: 2 },
  ] };
  const calls = [], reviews = [], applied = [], dirty = [], busy = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ path, method, body });
    let data;
    if (path === "/api/v1/config" && method === "GET") {
      if (overrides.reloadError && calls.some((call) => call.method === "PATCH")) throw new Error("Settings refresh failed");
      data = cfg;
    }
    else if (path === "/api/v1/config/activation" && method === "GET") data = activation;
    else if (path === "/api/v1/config" && method === "PATCH") {
      if (body.expected_revision !== cfg.revision) return { ok: false, status: 409, json: async () => ({ code: "CONFLICT", message: "Host settings changed" }) };
      if (overrides.patchError) return { ok: false, status: 422, json: async () => ({ code: "VALIDATION_ERROR", message: "Value is invalid" }) };
      for (const entry of cfg.entries) if (entry.key in body.updates) entry.value = body.updates[entry.key];
      cfg.revision = "b".repeat(64);
      data = { saved: Object.keys(body.updates), engine_restart_required: true, revision: cfg.revision };
    } else throw new Error(`Unexpected request: ${method} ${path}`);
    return { ok: true, json: async () => ({ success: true, data: structuredClone(data) }) };
  };
  const runtime = { blocked: false, status: { running: overrides.running ?? false },
    review: async (request) => { reviews.push(request); }, apply: async () => { applied.push(true); },
    ...overrides.runtime };
  const component = harness({ navigation: null, preferences: React.createElement("p", null, "Console preferences"),
    onDirtyChange: (value) => dirty.push(value), onBusyChange: (value) => busy.push(value), runtime });
  try { await component.settle(); await action({ component, calls, reviews, applied, dirty, busy, cfg, activation }); }
  finally { component.dispose(); globalThis.fetch = originalFetch; }
}

for (const running of [false, true]) {
  test(`General exposes both host settings while the engine running=${running}`, async () => {
    await withSettings(async ({ component, calls, dirty }) => {
      assert.deepEqual(calls, [{ path: "/api/v1/config", method: "GET", body: undefined }]);
      const fields = elements(component.tree).filter((item) => item.type === "input");
      assert.deepEqual(fields.map((item) => item.props.id), ["setting-ALGORITHM.baseline_duration", "setting-NEUROENGINE.queue_size"]);
      assert.equal(input(component, "ALGORITHM.baseline_duration").props.value, "0.5");
      assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
      assert.match(text(component.tree), /Console preferences/);
      assert.doesNotMatch(text(component.tree), /Advanced|Default configuration|Edit configuration|Active configuration|Save as|Delete|Category|Saved resolved/);
      assert.equal(elements(component.tree).some((item) => item.type === "select" || item.type === "datalist"), false);
      await change(component, "ALGORITHM.baseline_duration", "0.75");
      await change(component, "NEUROENGINE.queue_size", "64");
      assert.equal(dirty.at(-1), true);
      assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
    }, { running });
  });
}

test("Save sends host updates only and Review retains every selected plugin binding", async () => {
  await withSettings(async ({ component, calls, reviews, applied, dirty, busy }) => {
    await change(component, "ALGORITHM.baseline_duration", "0.75");
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Save settings");
    assert.deepEqual(calls.filter((call) => call.method === "PATCH"), [{ path: "/api/v1/config", method: "PATCH", body: {
      updates: { "ALGORITHM.baseline_duration": 0.75, "NEUROENGINE.queue_size": 64 }, expected_revision: "a".repeat(64),
    } }]);
    assert.deepEqual(reviews, []);
    assert.deepEqual(applied, []);
    assert.equal(dirty.at(-1), false);
    assert.deepEqual(busy, [true, false, true, false]);
    assert.equal(input(component, "ALGORITHM.baseline_duration").props.value, "0.75");
    await click(component, "Review and apply host settings");
    assert.deepEqual(reviews, [{ bindings: [
      { slot: "acquisition.source", profile_id: "source" },
      { slot: "processing.filter.first", profile_id: "filter" },
      { slot: "processing.filter.second", profile_id: "filter" },
    ], expected_revision: "accepted", refresh_slots: [], include_host_settings: true }]);
    assert.deepEqual(applied, [], "Only the runtime review dialog may apply the saved settings");
    assert.deepEqual([...new Set(calls.map((call) => call.path))], ["/api/v1/config", "/api/v1/config/activation"]);
  });
});

test("host settings review retains an inline acquisition source without creating a profile", async () => {
  await withSettings(async ({ component, calls, reviews, applied, activation }) => {
    const source = { slot: "acquisition.source", kind: "acquisition", profile_id: null, profile_revision: null,
      selection: { package_id: "omnibci.curry8", contribution_id: "reader", version: "1.0.0" },
      parameters: { host: "127.0.0.1", port: 4455 } };
    activation.bindings[0] = structuredClone(source);
    const accepted = structuredClone(activation);
    await click(component, "Review and apply host settings");
    assert.deepEqual(reviews, [{ bindings: [
      { slot: source.slot, selection: source.selection, parameters: source.parameters },
      { slot: "processing.filter.first", profile_id: "filter" },
      { slot: "processing.filter.second", profile_id: "filter" },
    ], expected_revision: "accepted", refresh_slots: [], include_host_settings: true }]);
    assert.deepEqual(activation, accepted);
    assert.deepEqual(applied, []);
    assert.equal(calls.every((call) => call.method === "GET"), true);
  });
});

test("host settings review still requires saved profiles for other modules", async () => {
  await withSettings(async ({ component, reviews, activation }) => {
    activation.bindings[1].profile_id = null;
    await click(component, "Review and apply host settings");
    assert.deepEqual(reviews, []);
    assert.equal(elements(component.tree).some((item) => item.props.role === "alert"), true);
  });
});

test("invalid numeric input stays editable without sending updates", async () => {
  await withSettings(async ({ component, calls, dirty }) => {
    for (const value of ["", "invalid", "1.5"]) {
      await change(component, "NEUROENGINE.queue_size", value);
      await click(component, "Save settings");
      assert.match(text(component.tree), /Queue capacity: Enter a valid integer/);
      assert.equal(input(component, "NEUROENGINE.queue_size").props.value, value);
      assert.equal(dirty.at(-1), true);
    }
    assert.equal(calls.some((call) => call.method === "PATCH"), false);
  });
});

test("a rejected save preserves the draft and dirty navigation protection", async () => {
  await withSettings(async ({ component, dirty, busy, reviews }) => {
    await change(component, "NEUROENGINE.queue_size", "0");
    await click(component, "Save settings");
    assert.match(text(component.tree), /VALIDATION_ERROR.*Value is invalid/);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "0");
    assert.equal(dirty.at(-1), true);
    assert.equal(busy.at(-1), false);
    assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
    assert.deepEqual(reviews, []);
  }, { patchError: true });
});

test("a failed refresh after saving keeps the visible draft until saved settings are confirmed", async () => {
  await withSettings(async ({ component, cfg, dirty, busy }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Save settings");
    assert.equal(cfg.entries.find((entry) => entry.key === "NEUROENGINE.queue_size").value, 64);
    assert.match(text(component.tree), /Settings refresh failed/);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), true);
    assert.equal(busy.at(-1), false);
    assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
  }, { reloadError: true });
});

test("Discard confirms unsaved host changes and restores the saved values", async () => {
  await withSettings(async ({ component, calls, dirty }) => {
    await change(component, "ALGORITHM.baseline_duration", "2");
    await click(component, "Discard");
    assert.equal(dialog(component).props.message, "Discard unsaved host settings?");
    dialog(component).props.onCancel(); await component.settle();
    assert.equal(input(component, "ALGORITHM.baseline_duration").props.value, "2");
    assert.equal(dirty.at(-1), true);
    await click(component, "Discard");
    dialog(component).props.onConfirm(); await component.settle();
    assert.equal(input(component, "ALGORITHM.baseline_duration").props.value, "0.5");
    assert.equal(dirty.at(-1), false);
    assert.equal(calls.length, 1);
  });
});

test("runtime recovery blocks Review while host settings remain editable", async () => {
  await withSettings(async ({ component, reviews }) => {
    assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Save settings");
    assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
    assert.deepEqual(reviews, []);
  }, { runtime: { blocked: true } });
});


test("a stale save preserves the draft and requires an explicit refresh", async () => {
  await withSettings(async ({ component, cfg, calls, dirty, busy }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    cfg.entries.find((entry) => entry.key === "NEUROENGINE.queue_size").value = 128;
    cfg.revision = "c".repeat(64);
    await click(component, "Save settings");
    assert.match(text(component.tree), /CONFLICT.*Refresh settings before saving again/);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), true);
    assert.equal(busy.at(-1), false);
    assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
    assert.equal(calls.filter((call) => call.method === "PATCH").length, 1);
    assert.equal(calls.filter((call) => call.method === "GET").length, 1);
    assert.equal(cfg.entries.find((entry) => entry.key === "NEUROENGINE.queue_size").value, 128);
    const refresh = elements(component.tree).find((item) => item.props["aria-label"] === "Refresh settings");
    refresh.props.onClick(); await component.settle();
    assert.equal(dialog(component).props.message, "Discard unsaved host settings?");
    dialog(component).props.onConfirm(); await component.settle();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "128");
    assert.equal(dirty.at(-1), false);
    await change(component, "NEUROENGINE.queue_size", "256");
    await click(component, "Save settings");
    assert.equal(calls.filter((call) => call.method === "PATCH").at(-1).body.expected_revision, "c".repeat(64));
    assert.equal(cfg.entries.find((entry) => entry.key === "NEUROENGINE.queue_size").value, 256);
  });
});
