import { runtimeController } from "./module-fixtures.mjs";
/** Engine review and server startup saves share one settings page. */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { I18nProvider, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { ConfigCard } = await server.ssrLoadModule("/src/ConfigCard.tsx");
const { ServerLogs } = await server.ssrLoadModule("/src/ServerLogs.tsx");
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const originalWindow = globalThis.window;
afterEach(() => { globalThis.window = originalWindow; });
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
    rerender() { pending = true; },
    async settle() {
      for (let round = 0; round < 40; round += 1) {
        if (pending) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          cursor = 0; pending = false; current.current = dispatcher;
          try { tree = ConfigCard({ ...props, runtime: runtimeController(props.runtime) }); } finally { current.current = previous; }
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
async function saveShortcut(component, modifiers = {}) {
  let prevented = false;
  component.tree.props.onKeyDown({ ctrlKey: true, key: "s", preventDefault() { prevented = true; }, ...modifiers });
  await component.settle();
  return prevented;
}

async function withSettings(action, overrides = {}) {
  const cfg = { revision: "a".repeat(64), entries: [
    { key: "LOGGING.level", type: "string", value: "INFO", active_value: "INFO", restart_required: false, requires: "server-restart", description: "Log level" },
    { key: "LOGGING.file_path", type: "string", value: "omnibci.log", active_value: "omnibci.log", restart_required: false, requires: "server-restart", description: "Log path" },
    { key: "NEUROENGINE.command_timeout_s", type: "float", value: 10, active_value: 10, restart_required: false, requires: "server-restart", description: "Command timeout" },
    { key: "PLUGINS.user_dir", type: "string", value: "", active_value: "", restart_required: false, requires: "server-restart", description: "Plugin directory" },
  ] };
  const engine = { revision: overrides.engineRevision === undefined ? "engine-accepted" : overrides.engineRevision,
    settings: { "NEUROENGINE.queue_size": 32, "NEUROENGINE.startup_timeout_s": 5, debug: false } };
  const calls = [], reviews = [], dirty = [], busy = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ path, method, body });
    let data;
    if (path === "/api/v1/config" && method === "GET") {
      if (overrides.reloadError && calls.some((call) => call.method === "PATCH")) throw new Error("Settings refresh failed");
      if (overrides.serverReloadError && calls.filter((call) => call.path === path).length > 1) throw new Error("Server settings refresh failed");
      data = cfg;
    } else if (path === "/api/v1/config/engine" && method === "GET") {
      if (overrides.engineReloadError && calls.filter((call) => call.path === path).length > 1) throw new Error("Engine settings refresh failed");
      if (calls.filter((call) => call.path === path).length > 1) await overrides.engineReloadWait;
      data = engine;
    }
    else if (path === "/api/v1/config" && method === "PATCH") {
      if (body.expected_revision !== cfg.revision) return { ok: false, status: 409, json: async () => ({ code: "CONFLICT", message: "Settings changed" }) };
      if (overrides.patchError) return { ok: false, status: 422, json: async () => ({ code: "VALIDATION_ERROR", message: "Value is invalid" }) };
      for (const entry of cfg.entries) if (entry.key in body.updates) {
        entry.value = body.updates[entry.key]; entry.restart_required = entry.value !== entry.active_value;
      }
      cfg.revision = "b".repeat(64);
      data = { saved: Object.keys(body.updates), server_restart_required: cfg.entries.some((entry) => entry.restart_required), revision: cfg.revision };
    } else throw new Error(`Unexpected request: ${method} ${path}`);
    return { ok: true, json: async () => ({ success: true, data: structuredClone(data) }) };
  };
  const runtime = { blocked: false, status: { running: overrides.running ?? false, accepted: { engine: engine.revision } },
    review: async (request) => { reviews.push(request); return { plan_id: `plan-${reviews.length}` }; }, ...overrides.runtime };
  const component = harness({ navigation: null, preferences: React.createElement("p", null, "Console preferences"),
    onDirtyChange: (value) => dirty.push(value), onBusyChange: (value) => busy.push(value), runtime });
  const complete = async (settings = reviews.at(-1).settings) => {
    engine.revision = "engine-applied"; engine.settings = structuredClone(settings);
    runtime.status.accepted = { engine: engine.revision };
    runtime.completedOperation = { operation_id: "applied-operation", plan_id: `plan-${reviews.length}`, module: "engine",
      status: "succeeded", target_revisions: { engine: engine.revision } };
    component.rerender(); await component.settle();
  };
  try { await component.settle(); await action({ component, calls, reviews, dirty, busy, cfg, engine, runtime, complete }); }
  finally { component.dispose(); globalThis.fetch = originalFetch; }
}

for (const running of [false, true]) {
  test(`Engine settings show independent Engine and Server groups while running=${running}`, async () => {
    await withSettings(async ({ component, calls }) => {
      assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
      assert.equal(input(component, "NEUROENGINE.startup_timeout_s").props.value, "5");
      assert.equal(input(component, "debug").props.checked, false);
      assert.equal(input(component, "NEUROENGINE.command_timeout_s").props.value, "10");
      assert.equal(input(component, "LOGGING.file_path").props.value, "omnibci.log");
      assert.match(text(component.tree), /Console preferences/);
      assert.match(text(component.tree), /Server/);
      assert.deepEqual([...new Set(calls.map((call) => call.path))].sort(), ["/api/v1/config", "/api/v1/config/engine"]);
    }, { running });
  });
}

test("Engine Review submits current values without saving host settings first", async () => {
  await withSettings(async ({ component, calls, reviews, dirty }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "NEUROENGINE.startup_timeout_s", "8.5");
    input(component, "debug").props.onChange({ target: { checked: true } }); await component.settle();
    await click(component, "Review Engine settings");
    assert.deepEqual(reviews, [{ module: "engine", bindings: [], expected_revision: "engine-accepted",
      settings: { "NEUROENGINE.queue_size": 64, "NEUROENGINE.startup_timeout_s": 8.5, debug: true } }]);
    assert.equal(calls.some((call) => call.method === "PATCH"), false);
    assert.equal(dirty.at(-1), true, "Review does not acknowledge application");
  });
});

test("an absent Engine configuration can review declared defaults without a saved host draft", async () => {
  await withSettings(async ({ component, reviews }) => {
    await click(component, "Review Engine settings");
    assert.equal(reviews[0].expected_revision, null);
    assert.equal(reviews[0].settings["NEUROENGINE.queue_size"], 32);
  }, { engineRevision: null });
});

test("Server saves only its own values and leaves the Engine draft pending", async () => {
  await withSettings(async ({ component, calls, reviews, dirty }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "NEUROENGINE.command_timeout_s", "20");
    await click(component, "Save Server settings");
    assert.deepEqual(calls.filter((call) => call.method === "PATCH").map((call) => call.body), [{
      updates: { "NEUROENGINE.command_timeout_s": 20 }, expected_revision: "a".repeat(64),
    }]);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), true); assert.equal(reviews.length, 0);
    assert.match(text(component.tree), /Server restart required/);
    assert.match(text(component.tree), /Current: 10/);
    await click(component, "Review Engine settings");
    assert.equal(reviews[0].expected_revision, "engine-accepted");
  });
});

test("unsaved Server fields do not block Engine review", async () => {
  await withSettings(async ({ component, reviews, calls }) => {
    await change(component, "LOGGING.file_path", "new.log");
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Review Engine settings");
    assert.equal(reviews.length, 1);
    assert.equal(input(component, "LOGGING.file_path").props.value, "new.log");
    assert.equal(calls.some((call) => call.method === "PATCH"), false);
  });
});

test("runtime recovery blocks Engine review while Server saves remain available", async () => {
  await withSettings(async ({ component, calls, reviews }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "NEUROENGINE.command_timeout_s", "20");
    assert.equal(button(component.tree, "Review Engine settings").props.disabled, true);
    await click(component, "Save Server settings");
    assert.equal(reviews.length, 0);
    assert.equal(calls.filter((call) => call.method === "PATCH").length, 1);
  }, { runtime: { blocked: true, recoveryRequired: true } });
});

test("confirmed Engine application advances its baseline and preserves later edits", async () => {
  await withSettings(async ({ component, reviews, dirty, complete }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Review Engine settings");
    await change(component, "NEUROENGINE.queue_size", "128");
    await complete();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "128");
    assert.equal(dirty.at(-1), true);
    await click(component, "Review Engine settings");
    assert.equal(reviews.at(-1).expected_revision, "engine-applied");
    assert.equal(reviews.at(-1).settings["NEUROENGINE.queue_size"], 128);
  });
});

test("reverting to the old Engine value while applying remains a new edit", async () => {
  await withSettings(async ({ component, dirty, complete }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Review Engine settings");
    await change(component, "NEUROENGINE.queue_size", "32");
    await complete();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
    assert.equal(dirty.at(-1), true);
  });
});

test("an accepted revision arriving before operation completion preserves a reverted edit", async () => {
  await withSettings(async ({ component, engine, runtime, dirty, complete }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Review Engine settings");
    runtime.inProgress = true; component.rerender(); await component.settle();
    await change(component, "NEUROENGINE.queue_size", "32");
    engine.revision = "engine-applied"; engine.settings["NEUROENGINE.queue_size"] = 64;
    runtime.status.accepted = { engine: engine.revision }; component.rerender(); await component.settle();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
    runtime.inProgress = false;
    await complete();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
    assert.equal(dirty.at(-1), true);
  });
});

test("confirmed Engine application clears only unchanged submitted edits", async () => {
  await withSettings(async ({ component, dirty, complete }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await click(component, "Review Engine settings");
    await complete();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), false);
    assert.equal(button(component.tree, "Review Engine settings").props.disabled, true);
  });
});

for (const [key, value, accepted] of [["NEUROENGINE.queue_size", "64.0", "64"], ["NEUROENGINE.startup_timeout_s", "8.50", "8.5"]]) {
  test(`confirmed Engine application clears the equivalent numeric edit ${key}=${value}`, async () => {
    await withSettings(async ({ component, dirty, complete }) => {
      await change(component, key, value); await click(component, "Review Engine settings");
      await complete();
      assert.equal(input(component, key).props.value, accepted);
      assert.equal(dirty.at(-1), false);
      assert.equal(button(component.tree, "Review Engine settings").props.disabled, true);
    });
  });
}

test("confirmed Engine application preserves an invalid edit made during application", async () => {
  await withSettings(async ({ component, dirty, complete }) => {
    await change(component, "NEUROENGINE.queue_size", "64"); await click(component, "Review Engine settings");
    await change(component, "NEUROENGINE.queue_size", "");
    await complete();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "");
    assert.equal(dirty.at(-1), true);
  });
});

test("an external Engine update never silently rebases a dirty editor", async () => {
  await withSettings(async ({ component, engine, runtime, reviews }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    engine.revision = "another-client"; engine.settings["NEUROENGINE.queue_size"] = 256;
    runtime.status.accepted = { engine: "another-client" }; component.rerender(); await component.settle();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    await click(component, "Review Engine settings");
    assert.equal(reviews[0].expected_revision, "engine-accepted");
  });
});

for (const [key, value] of [["NEUROENGINE.queue_size", ""], ["NEUROENGINE.queue_size", "0"],
  ["NEUROENGINE.queue_size", "1.5"], ["NEUROENGINE.startup_timeout_s", "0"], ["NEUROENGINE.startup_timeout_s", "Infinity"]]) {
  test(`invalid Engine ${key}=${value} stays editable without any request`, async () => {
    await withSettings(async ({ component, reviews, calls, dirty }) => {
      await change(component, key, value); await click(component, "Review Engine settings");
      assert.equal(reviews.length, 0); assert.equal(calls.some((call) => call.method === "PATCH"), false);
      assert.equal(input(component, key).props.value, value); assert.equal(dirty.at(-1), true);
      assert.match(text(component.tree), /positive/);
    });
  });
}

for (const [key, value] of [["NEUROENGINE.command_timeout_s", "0"], ["NEUROENGINE.command_timeout_s", "Infinity"], ["LOGGING.file_path", " "]]) {
  test(`invalid Server ${key}=${value} stays editable without being saved`, async () => {
    await withSettings(async ({ component, calls, dirty }) => {
      await change(component, key, value); await click(component, "Save Server settings");
      assert.equal(calls.some((call) => call.method === "PATCH"), false);
      assert.equal(input(component, key).props.value, value); assert.equal(dirty.at(-1), true);
    });
  });
}

test("clearing the plugin directory restores the default and clears restart pending", async () => {
  await withSettings(async ({ component, calls }) => {
    await change(component, "PLUGINS.user_dir", "/tmp/omnibci-plugins"); await click(component, "Save Server settings");
    assert.match(text(component.tree), /Server restart required/);
    await change(component, "PLUGINS.user_dir", ""); await click(component, "Save Server settings");
    assert.equal(calls.filter((call) => call.method === "PATCH").at(-1).body.updates["PLUGINS.user_dir"], "");
    assert.doesNotMatch(text(component.tree), /Server restart required|Restart the server/);
  });
});

test("a rejected Server save preserves both drafts and navigation protection", async () => {
  await withSettings(async ({ component, dirty, busy }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "LOGGING.file_path", "new.log");
    await click(component, "Save Server settings");
    assert.match(text(component.tree), /VALIDATION_ERROR.*Value is invalid/);
    assert.equal(input(component, "LOGGING.file_path").props.value, "new.log");
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), true); assert.equal(busy.at(-1), false);
  }, { patchError: true });
});

test("a failed Server reload keeps the visible draft until saved values are confirmed", async () => {
  await withSettings(async ({ component, dirty, cfg }) => {
    await change(component, "LOGGING.file_path", "new.log"); await click(component, "Save Server settings");
    assert.equal(cfg.entries.find((entry) => entry.key === "LOGGING.file_path").value, "new.log");
    assert.match(text(component.tree), /Settings refresh failed/);
    assert.equal(dirty.at(-1), true);
  }, { reloadError: true });
});

test("a stale Server save preserves edits until an explicit refresh", async () => {
  await withSettings(async ({ component, cfg, dirty }) => {
    await change(component, "LOGGING.file_path", "new.log"); cfg.revision = "c".repeat(64);
    await click(component, "Save Server settings");
    assert.match(text(component.tree), /CONFLICT.*Refresh/); assert.equal(dirty.at(-1), true);
    elements(component.tree).find((item) => item.props["aria-label"] === "Refresh settings").props.onClick();
    await component.settle(); dialog(component).props.onConfirm(); await component.settle();
    assert.equal(dirty.at(-1), false);
    await change(component, "LOGGING.file_path", "new.log"); await click(component, "Save Server settings");
    assert.equal(cfg.entries.find((entry) => entry.key === "LOGGING.file_path").value, "new.log");
  });
});

test("partial refresh success never combines a new Server revision with a discarded draft", async () => {
  await withSettings(async ({ component, cfg, dirty }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "LOGGING.file_path", "draft.log");
    cfg.revision = "another-client";
    cfg.entries.find((entry) => entry.key === "LOGGING.file_path").value = "another-client.log";
    elements(component.tree).find((item) => item.props["aria-label"] === "Refresh settings").props.onClick();
    await component.settle(); dialog(component).props.onConfirm(); await component.settle();
    assert.match(text(component.tree), /Engine settings refresh failed/);
    assert.equal(input(component, "LOGGING.file_path").props.value, "another-client.log");
    assert.equal(button(component.tree, "Save Server settings").props.disabled, true);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "64");
    assert.equal(dirty.at(-1), true);
  }, { engineReloadError: true });
});

test("partial refresh failure keeps editing blocked until the other request settles", async () => {
  let finishEngineRead;
  const engineReloadWait = new Promise((resolve) => { finishEngineRead = resolve; });
  await withSettings(async ({ component, busy, dirty }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "LOGGING.file_path", "draft.log");
    elements(component.tree).find((item) => item.props["aria-label"] === "Refresh settings").props.onClick();
    await component.settle(); dialog(component).props.onConfirm(); await component.settle();
    assert.equal(busy.at(-1), true);
    assert.equal(input(component, "NEUROENGINE.queue_size").props.disabled, true);
    finishEngineRead(); await component.settle();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
    assert.equal(input(component, "LOGGING.file_path").props.value, "draft.log");
    assert.equal(busy.at(-1), false); assert.equal(dirty.at(-1), true);
    assert.match(text(component.tree), /Server settings refresh failed/);
  }, { serverReloadError: true, engineReloadWait });
});

test("Discard restores both groups without saving or applying", async () => {
  await withSettings(async ({ component, dirty, calls, reviews }) => {
    await change(component, "NEUROENGINE.queue_size", "64");
    await change(component, "LOGGING.file_path", "new.log");
    await click(component, "Discard"); dialog(component).props.onCancel(); await component.settle();
    assert.equal(dirty.at(-1), true);
    await click(component, "Discard"); dialog(component).props.onConfirm(); await component.settle();
    assert.equal(input(component, "NEUROENGINE.queue_size").props.value, "32");
    assert.equal(input(component, "LOGGING.file_path").props.value, "omnibci.log");
    assert.equal(dirty.at(-1), false); assert.equal(reviews.length, 0);
    assert.equal(calls.every((call) => call.method === "GET"), true);
  });
});

for (const modifiers of [{ ctrlKey: true }, { ctrlKey: false, metaKey: true, key: "S" }]) {
  test(`Server save shortcut stays independent of runtime recovery (${JSON.stringify(modifiers)})`, async () => {
    await withSettings(async ({ component, calls, reviews }) => {
      await change(component, "NEUROENGINE.command_timeout_s", "20");
      assert.equal(await saveShortcut(component, modifiers), true);
      assert.equal(calls.filter((call) => call.method === "PATCH").length, 1);
      assert.equal(reviews.length, 0);
    }, { runtime: { blocked: true, recoveryRequired: true } });
  });
}

test("Engine save shortcut reviews the current draft without persisting it", async () => {
  await withSettings(async ({ component, calls, reviews }) => {
    await change(component, "NEUROENGINE.queue_size", "64"); await saveShortcut(component);
    assert.equal(reviews.length, 1); assert.equal(calls.some((call) => call.method === "PATCH"), false);
  });
});

test("shortcuts ignore extra modifiers, handled keys, repeats, composition and open dialogs", async () => {
  await withSettings(async ({ component, calls }) => {
    await change(component, "LOGGING.file_path", "new.log");
    for (const modifiers of [{ altKey: true }, { shiftKey: true }, { defaultPrevented: true }]) assert.equal(await saveShortcut(component, modifiers), false);
    await saveShortcut(component, { repeat: true }); await saveShortcut(component, { nativeEvent: { isComposing: true } });
    await click(component, "Discard"); await saveShortcut(component);
    assert.equal(calls.filter((call) => call.method === "PATCH").length, 0);
  });
});

for (const available of [true, false]) {
  test(`Server keyboard save restores field or heading focus (available=${available})`, async () => {
    const originalDocument = globalThis.document;
    let fieldFocus = 0, headingFocus = 0;
    globalThis.document = { activeElement: { isConnected: available, getClientRects: () => available ? [{}] : [], focus() { fieldFocus += 1; } } };
    try {
      await withSettings(async ({ component }) => {
        elements(component.tree).find((item) => item.type === "h1").ref.current = { focus() { headingFocus += 1; } };
        await change(component, "LOGGING.file_path", "new.log"); await saveShortcut(component);
        assert.equal(fieldFocus, available ? 1 : 0); assert.equal(headingFocus, available ? 0 : 1);
      });
    } finally { if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; }
  });
}

function renderLogs(data, error = null) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  const values = [data, error];
  dispatcher.current = {
    useContext: () => ({ t: (key) => translate("en", key) }),
    useState: () => [values.shift(), () => {}],
    useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback,
    useEffect: () => {},
  };
  let tree;
  try { tree = ServerLogs({ navigation: null }); }
  finally { dispatcher.current = previous; }
  return renderToStaticMarkup(tree);
}

test("a direct Logs URL selects Settings and exposes navigation back to General", () => {
  globalThis.window = {
    location: { hash: "#logs" }, navigator: { language: "en" },
    localStorage: { getItem: () => "en" },
  };
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(App)));
  assert.match(html, /aria-label="Settings"[^>]*aria-current="page"/);
  assert.match(html, /aria-label="Settings pages"/);
  assert.match(html, />General<\/span>/);
  assert.match(html, /<h1>Logs<\/h1>/);
  assert.match(html, /Refresh logs/);
});

test("log output escapes markup, preserves multiline text, and identifies truncated output", () => {
  const html = renderLogs({ filename: "server.log", lines: ["<script>alert(1)</script>", "ERROR second line"], truncated: true });
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;\nERROR second line/);
  assert.match(html, /Earlier content is omitted/);
  assert.match(html, /server.log/);
  assert.match(html, /tabindex="0" role="region" aria-label="Server log output"/);
});

test("empty logs, loading, and a failed refresh have distinct states", () => {
  const empty = renderLogs({ filename: "server.log", lines: [], truncated: false });
  assert.match(empty, /No log entries yet/);
  assert.doesNotMatch(empty, /Earlier content is omitted|Loading logs/);
  assert.match(renderLogs(null), /Loading logs/);
  const failed = renderLogs(null, new Error("unavailable"));
  assert.match(failed, /role="alert"/);
  assert.match(failed, /Logs could not be refreshed/);
  assert.match(failed, /Refresh logs/);
  assert.doesNotMatch(failed, /Loading logs|No log entries yet/);
});
