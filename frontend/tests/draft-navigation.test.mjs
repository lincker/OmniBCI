/** Unsaved editors survive navigation attempts until the user explicitly discards them. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const { ConfigDialog } = await server.ssrLoadModule("/src/ConfigDialog.tsx");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const desktop = await server.ssrLoadModule("/src/desktop.ts");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const sameDependencies = (a, b) => !!a && !!b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));

function harness(Component, props = {}, attachRefs = () => {}) {
  const hooks = [];
  let cursor = 0, pending = true, tree, effects = [];
  const dispatcher = {
    useContext: () => english,
    useId() { return `test-${cursor++}`; },
    useState(initial) {
      const i = cursor++;
      hooks[i] ??= { value: typeof initial === "function" ? initial() : initial };
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
        const previous = hooks[i]; hooks[i] = { deps };
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
          try { tree = Component(props); } finally { current.current = previous; }
          attachRefs(tree);
          const queued = effects; effects = []; queued.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(pending, false, "Navigation component did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
const component = (h, name) => elements(h.tree).find((item) => item.type.name === name);
const navigation = (h, label) => elements(h.tree).find((item) => item.type === "button" && item.props["aria-label"] === label);

async function withApp(action, { desktopMode = false, applying = false } = {}) {
  const original = Object.fromEntries(["window", "document", "location", "localStorage", "WebSocket", "fetch"].map((key) => [key, globalThis[key]]));
  const originalStatus = runtimeConfigApi.status;
  const listeners = new Map(), editorStates = [];
  const location = { hash: "#acquisition", protocol: "http:", host: "localhost" };
  const localStorage = { getItem: () => null, setItem: () => {} };
  const state = { running: false, accepted_revision: null, applied_revision: null, generation: null, recovery_required: false,
    operation: applying ? { operation_id: "operation-1", status: "applying", stage: "rebuilding" } : null };
  globalThis.window = { location, localStorage, history: { replaceState: (_state, _title, hash) => { location.hash = hash; } },
    addEventListener: (event, listener) => listeners.set(event, listener),
    removeEventListener: (event, listener) => { if (listeners.get(event) === listener) listeners.delete(event); } };
  globalThis.location = location; globalThis.localStorage = localStorage;
  globalThis.document = { hidden: true, documentElement: { style: {} } };
  globalThis.WebSocket = class { close() {} };
  globalThis.fetch = () => new Promise(() => {});
  runtimeConfigApi.status = async () => state;
  if (desktopMode) {
    desktop.setDesktopConnection({ id: "backend", kind: "local", status: "ready" });
    window.omnibciDesktop = { subscribeResults: () => () => {}, setEditorState: (value) => editorStates.push(value) };
  }
  const focusEvents = [];
  const heading = { setAttribute: (name, value) => { heading[name] = value; }, focus: () => focusEvents.push(location.hash) };
  const content = { querySelector: () => heading, focus: () => focusEvents.push("main") };
  const h = harness(App, {}, (tree) => { elements(tree).find((item) => item.type === "main").ref.current = content; });
  try { await h.settle(); await action({ h, listeners, editorStates, state, focusEvents, heading }); }
  finally { h.dispose(); runtimeConfigApi.status = originalStatus; Object.assign(globalThis, original); }
}

for (const trigger of ["workspace", "hash", "shortcut"]) {
  test(`${trigger} navigation preserves the editor until an explicit discard`, async () => {
    await withApp(async ({ h, listeners }) => {
      component(h, "PluginProfiles").props.onDirtyChange(true); await h.settle();
      const navigate = () => {
        if (trigger === "workspace") navigation(h, "Run").props.onClick();
        else if (trigger === "hash") { window.location.hash = "#run"; listeners.get("hashchange")(); }
        else h.tree.props.onKeyDown({ ctrlKey: true, key: ",", target: {}, preventDefault() {} });
      };
      navigate(); await h.settle();
      assert.equal(component(h, "PluginProfiles").props.module, "acquisition");
      assert.equal(window.location.hash, "#acquisition");
      assert.equal(component(h, "ConfigDialog").props.cancelLabel, "Keep editing");
      component(h, "ConfigDialog").props.onCancel(); await h.settle();
      assert.equal(component(h, "ConfigDialog"), undefined);
      assert.ok(listeners.has("beforeunload"));
      navigate(); await h.settle();
      component(h, "ConfigDialog").props.onConfirm(); await h.settle();
      assert.equal(component(h, "PluginProfiles"), undefined);
      assert.equal(window.location.hash, trigger === "shortcut" ? "#general" : "#run");
      assert.equal(listeners.has("beforeunload"), false);
    });
  });
}

test("a busy editor rejects hash navigation without discarding or opening another dialog", async () => {
  await withApp(async ({ h, listeners }) => {
    component(h, "PluginProfiles").props.onDirtyChange(true);
    component(h, "PluginProfiles").props.onBusyChange(true); await h.settle();
    window.location.hash = "#run"; listeners.get("hashchange")(); await h.settle();
    assert.equal(window.location.hash, "#acquisition");
    assert.equal(component(h, "PluginProfiles").props.module, "acquisition");
    assert.equal(component(h, "ConfigDialog"), undefined);
  });
});

test("Skip to editor focuses the page heading without changing the hash route or draft", async () => {
  await withApp(async ({ h, focusEvents, heading }) => {
    assert.deepEqual(focusEvents, [], "Initial loading does not steal focus");
    component(h, "PluginProfiles").props.onDirtyChange(true); await h.settle();
    const skip = elements(h.tree).find((item) => item.type === "a" && item.props.className === "skip-link");
    let prevented = false;
    skip.props.onClick({ preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(heading.tabindex, "-1");
    assert.deepEqual(focusEvents, ["#acquisition"]);
    assert.equal(window.location.hash, "#acquisition");
    assert.equal(component(h, "ConfigDialog"), undefined);
  });
});

test("navigation moves focus to new content only after a discard is confirmed", async () => {
  await withApp(async ({ h, focusEvents }) => {
    component(h, "PluginProfiles").props.onDirtyChange(true); await h.settle();
    navigation(h, "Run").props.onClick(); await h.settle();
    component(h, "ConfigDialog").props.onCancel(); await h.settle();
    assert.deepEqual(focusEvents, []);
    navigation(h, "Run").props.onClick(); await h.settle();
    component(h, "ConfigDialog").props.onConfirm(); await h.settle();
    assert.deepEqual(focusEvents, ["#run"]);
    await component(h, "RuntimeConfigurationReview").props.runtime.refresh(); await h.settle();
    assert.deepEqual(focusEvents, ["#run"], "Background status polling does not move focus");
  });
});

test("settings shortcuts ignore handled events and keys inside dialogs", async () => {
  await withApp(async ({ h, focusEvents }) => {
    for (const event of [{ defaultPrevented: true, target: {} }, { target: { closest: () => ({}) } }]) {
      h.tree.props.onKeyDown({ ctrlKey: true, key: ",", preventDefault() { assert.fail("Dialog shortcut was intercepted"); }, ...event });
      await h.settle();
      assert.equal(window.location.hash, "#acquisition");
    }
    assert.deepEqual(focusEvents, []);
  });
});

test("Open Run preserves draft protection and Change setup returns to Acquisition", async () => {
  await withApp(async ({ h, listeners }) => {
    component(h, "PluginProfiles").props.onDirtyChange(true); await h.settle();
    component(h, "PluginProfiles").props.onRun(); await h.settle();
    assert.equal(window.location.hash, "#acquisition");
    component(h, "ConfigDialog").props.onCancel(); await h.settle();
    component(h, "PluginProfiles").props.onDirtyChange(false); await h.settle();
    window.location.hash = "#processing"; listeners.get("hashchange")(); await h.settle();
    navigation(h, "Run").props.onClick(); await h.settle();
    component(h, "RunConsole").props.onEditSetup(); await h.settle();
    assert.equal(window.location.hash, "#acquisition", "Source setup must not depend on the last visited setup tab");
  });
});

test("Run processing shortcut opens the relevant editor and respects busy navigation", async () => {
  await withApp(async ({ h }) => {
    navigation(h, "Run").props.onClick(); await h.settle();
    component(h, "RunConsole").props.onEditProcessing(); await h.settle();
    assert.equal(window.location.hash, "#processing");
    assert.equal(component(h, "PluginProfiles").props.module, "processing");
    navigation(h, "Run").props.onClick(); await h.settle();
    component(h, "RunConsole").props.onBusyChange(true); await h.settle();
    component(h, "RunConsole").props.onEditProcessing(); await h.settle();
    assert.equal(window.location.hash, "#run", "A running operation keeps navigation blocked");
    component(h, "RunConsole").props.onBusyChange(false); await h.settle();
    component(h, "RunConsole").props.onEditProcessing(); await h.settle();
    assert.equal(window.location.hash, "#processing");
    assert.equal(component(h, "PluginProfiles").props.module, "processing");
  });
});

test("browser refresh stays protected until an in-flight runtime application settles", async () => {
  await withApp(async ({ h, listeners, state }) => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    assert.ok(listeners.has("beforeunload"));
    listeners.get("beforeunload")(event);
    assert.equal(event.prevented, true); assert.equal(event.returnValue, "");
    state.operation = { operation_id: "operation-1", status: "succeeded", stage: "complete" };
    await component(h, "RuntimeConfigurationReview").props.runtime.refresh(); await h.settle();
    assert.equal(listeners.has("beforeunload"), false);
  }, { applying: true });
});

test("desktop delegates dirty and in-flight protection to its existing native connection and close guard", async () => {
  await withApp(async ({ h, listeners, editorStates }) => {
    component(h, "PluginProfiles").props.onDirtyChange(true); await h.settle();
    assert.deepEqual(editorStates.at(-1), { dirty: true, busy: true });
    assert.equal(listeners.has("beforeunload"), false);
  }, { desktopMode: true, applying: true });
});

test("discard dialogs describe the consequence and focus Keep editing; Escape preserves edits", async () => {
  let cancelled = 0, confirmed = 0;
  const h = harness(ConfigDialog, { title: "Unsaved changes", message: "Discard the draft?", confirmLabel: "Discard changes",
    cancelLabel: "Keep editing", onCancel: () => { cancelled += 1; }, onConfirm: () => { confirmed += 1; } });
  await h.settle();
  const dialog = h.tree;
  const description = elements(h.tree).find((item) => item.type === "p");
  assert.equal(dialog.props["aria-describedby"], description.props.id);
  const cancel = elements(h.tree).find((item) => item.type === "button" && item.props.type === "button");
  assert.equal(cancel.props.children, "Keep editing"); assert.equal(cancel.props.autoFocus, true);
  let prevented = false;
  dialog.props.onCancel({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(cancelled, 1); assert.equal(confirmed, 0);
  h.dispose();
});

test("closing a dialog restores focus only while the original control still exists", async () => {
  const originalDocument = globalThis.document;
  try {
    for (const connected of [true, false]) {
      let focused = 0, closed = 0;
      const opener = { isConnected: connected, focus: () => { focused += 1; } };
      globalThis.document = { activeElement: opener };
      const h = harness(ConfigDialog, { title: "Discard draft", message: "Pending edits will be lost.", confirmLabel: "Discard",
        onConfirm() {}, onCancel() {} }, (tree) => {
        tree.ref.current = { showModal() { document.activeElement = {}; }, close() { closed += 1; } };
      });
      await h.settle(); h.dispose();
      assert.equal(closed, 1);
      assert.equal(focused, connected ? 1 : 0);
    }
  } finally { globalThis.document = originalDocument; }
});

async function withSharedAcquisition(action) {
  const original = { ...api }, originalActivation = pluginConfigApi.activation;
  const source = { connected: false, streaming: false, connection_id: null }, calls = [];
  const backend = { status: async () => ({ ...source }) };
  let engineState;
  api.engineStatus = async () => ({ running: engineState?.running ?? false });
  api.acquisitionStatus = () => backend.status();
  try {
    await withApp(async ({ h, state }) => {
      engineState = state;
      Object.assign(state, { running: true, generation: "engine-1", accepted_revision: "rev-1", applied_revision: "rev-1" });
      pluginConfigApi.activation = async () => ({ revision: "rev-1", bindings: [{ slot: "acquisition.source" }] });
      api.acqConnect = async () => { calls.push("connect"); Object.assign(source, { connected: true, connection_id: "source-1" }); };
      api.acqDisconnect = async () => { calls.push("disconnect"); Object.assign(source, { connected: false, connection_id: null }); };
      api.acqMetadata = async () => assert.fail("Connect must not inspect metadata");
      api.acqStreamStart = async () => { calls.push("read"); source.streaming = true; };
      api.acqStreamStop = async () => { calls.push("stop"); source.streaming = false; };
      const controller = () => (component(h, "PluginProfiles") ?? component(h, "RunConsole")).props.acquisition;
      await controller().refresh(); await h.settle();
      await action({ h, source, calls, controller, backend });
    });
  } finally { Object.assign(api, original); pluginConfigApi.activation = originalActivation; }
}

test("Setup and Run share connection state through connect, reading, stop and disconnect", async () => {
  await withSharedAcquisition(async ({ h, calls, controller }) => {
    await controller().connect.run("rev-1"); await h.settle();
    assert.equal(controller().data.connected, true);
    assert.deepEqual(calls, ["connect"]);
    component(h, "PluginProfiles").props.onRun(); await h.settle();
    assert.ok(component(h, "RunConsole"));
    assert.equal(controller().data.connected, true);
    await controller().read.run("rev-1"); await h.settle();
    assert.equal(controller().data.streaming, true);
    await controller().stopRead.run(); await h.settle();
    assert.equal(controller().data.connected, true);
    assert.equal(controller().data.streaming, false);
    await controller().disconnect.run(); await h.settle();
    component(h, "RunConsole").props.onEditSetup(); await h.settle();
    assert.ok(component(h, "PluginProfiles"));
    assert.equal(controller().data.connected, false);
    assert.deepEqual(calls, ["connect", "read", "stop", "disconnect"]);
  });
});

test("a pending shared connection blocks navigation and duplicate commands", async () => {
  await withSharedAcquisition(async ({ h, calls, controller }) => {
    let finish;
    const connect = api.acqConnect;
    api.acqConnect = async () => { await new Promise((resolve) => { finish = resolve; }); await connect(); };
    const connecting = controller().connect.run("rev-1");
    await h.settle();
    assert.equal(controller().pending, true);
    component(h, "PluginProfiles").props.onRun(); await h.settle();
    assert.ok(component(h, "PluginProfiles"));
    await controller().connect.run("rev-1");
    finish(); await connecting; await h.settle();
    assert.equal(controller().pending, false);
    assert.deepEqual(calls, ["connect"]);
  });
});

test("a failed connection refreshes actual status and keeps its error when switching pages", async () => {
  await withSharedAcquisition(async ({ h, source, controller, backend }) => {
    api.acqConnect = async () => { Object.assign(source, { connected: true, connection_id: "source-1" }); throw new Error("Reply timed out"); };
    await controller().connect.run("rev-1"); await h.settle();
    assert.equal(controller().data.connected, true);
    assert.match(controller().connect.error, /Reply timed out/);
    component(h, "PluginProfiles").props.onRun(); await h.settle();
    assert.equal(controller().data.connected, true);
    assert.match(controller().connect.error, /Reply timed out/);
    backend.status = async () => { throw new Error("Status unavailable"); };
    await controller().refresh(); await h.settle();
    assert.match(controller().error.message, /Status unavailable/);
    component(h, "RunConsole").props.onEditSetup(); await h.settle();
    assert.match(controller().error.message, /Status unavailable/);
  });
});

for (const staleFailure of [false, true]) test(`older acquisition status cannot overwrite a newer refresh (failure=${staleFailure})`, async () => {
  await withSharedAcquisition(async ({ h, controller, backend }) => {
    let resolveOld, rejectOld;
    backend.status = () => new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
    const older = controller().refresh();
    backend.status = async () => ({ connected: true, streaming: false, connection_id: "current" });
    await controller().refresh(); await h.settle();
    if (staleFailure) rejectOld(new Error("Old status failure"));
    else resolveOld({ connected: false, streaming: false, connection_id: null });
    await older; await h.settle();
    assert.equal(controller().data.connection_id, "current");
    assert.equal(controller().error, null);
  });
});

for (const unavailable of [false, true]) test(`Disconnect rejects a changed or unavailable source (unavailable=${unavailable})`, async () => {
  await withSharedAcquisition(async ({ h, source, calls, controller, backend }) => {
    await controller().connect.run("rev-1"); await h.settle();
    if (unavailable) backend.status = async () => null;
    else source.connection_id = "replacement";
    await controller().disconnect.run(); await h.settle();
    assert.deepEqual(calls, ["connect"]);
    assert.match(controller().disconnect.error, unavailable ? /status is unavailable/ : /connection changed/);
  });
});
