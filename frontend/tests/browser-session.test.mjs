/** Browser authentication never owns the shared Server lifecycle. */
import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import React from "react";
import { moduleConfiguration, revisionMap } from "./module-fixtures.mjs";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
const original = Object.fromEntries(["fetch", "window", "location", "WebSocket"].map((key) => [key, globalThis[key]]));
after(async () => { Object.assign(globalThis, original); await server.close(); });
const session = await server.ssrLoadModule("/src/api/browser-session.ts");
const { requestJson } = await server.ssrLoadModule("/src/api/transport.ts");
const { openResultsSocket } = await server.ssrLoadModule("/src/api/results-socket.ts");
const { default: BrowserApp } = await server.ssrLoadModule("/src/BrowserApp.tsx");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const { useCurrentConfiguration } = await server.ssrLoadModule("/src/hooks/useCurrentConfiguration.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const info = (instance_id = "server-A", auth_enabled = true) => ({ instance_id, auth_enabled, desktop_api_version: 1, app_version: "1.0.0" });
const response = (value) => Response.json({ success: true, data: value });
const english = { language: "en", setLanguage() {}, t: (key) => translate("en", key) };
const same = (a, b) => !!a && !!b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
const elements = (tree) => Array.isArray(tree) ? tree.flatMap(elements) : React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];

beforeEach(() => {
  globalThis.window = { location: { origin: "http://localhost:5173" }, confirm: () => true };
  globalThis.location = { protocol: "http:", host: "localhost:5173" };
  session.acceptBrowserSession(info());
});

function harness(Component = BrowserApp, replayMount = false) {
  const hooks = [];
  let cursor = 0, pending = true, tree, effects = [];
  const dispatcher = {
    useContext: () => english,
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
      if (!same(hooks[i]?.deps, deps)) hooks[i] = { value: callback, deps };
      return hooks[i].value;
    },
    useEffect(effect, deps) {
      const i = cursor++;
      if (!same(hooks[i]?.deps, deps)) {
        const previous = hooks[i]; hooks[i] = { deps, effect };
        effects.push(() => { previous?.cleanup?.(); hooks[i].cleanup = effect(); });
      }
    },
  };
  return {
    get nodes() { return elements(tree); },
    get app() { return elements(tree).find((node) => node.type.name === "App"); },
    async settle() {
      for (let round = 0; round < 60; round++) {
        if (pending) {
          const internal = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = internal.current;
          cursor = 0; pending = false; internal.current = dispatcher;
          try { tree = Component(); } finally { internal.current = previous; }
          const queued = effects; effects = []; queued.forEach((effect) => effect());
          if (replayMount) {
            replayMount = false;
            hooks.forEach((hook) => hook?.cleanup?.());
            hooks.forEach((hook) => { if (hook?.effect) hook.cleanup = hook.effect(); });
          }
        }
        await Promise.resolve();
      }
      assert.equal(pending, false, "Browser connection did not settle");
    },
    submit() { elements(tree).find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

test("browser login sends a token only to the session endpoint and then uses its cookie", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push([url, init]); return response(info()); };
  assert.deepEqual(await session.readBrowserSession("private-token"), info());
  await session.readBrowserSession();
  assert.equal(calls[0][0], "/api/v1/server/session");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers.Authorization, "Bearer private-token");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.equal(calls[1][0], "/api/v1/server/info");
  assert.equal(calls[1][1].headers, undefined);
  assert.equal(calls[1][1].cache, "no-store");
});

test("a rejected browser write requests authentication without claiming it may have executed", async () => {
  let lost = 0, calls = 0;
  const off = session.onBrowserSessionLost(() => lost++);
  globalThis.fetch = async () => { calls++; return new Response(null, { status: 401 }); };
  try {
    await assert.rejects(requestJson("/api/v1/engine/start", { method: "POST" }), session.AuthenticationRequired);
    assert.equal(lost, 1);
    await assert.rejects(requestJson("/api/v1/engine/start", { method: "POST" }), /Reconnect/);
    assert.equal(calls, 1);
  } finally { off(); }
});

test("unauthenticated service opens without a token form or starting the Engine", async () => {
  const calls = [];
  globalThis.fetch = async (url) => { calls.push(url); return response(info("server-A", false)); };
  const h = harness();
  try {
    await h.settle();
    assert.equal(h.app.key, "server-A");
    assert.equal(h.app.props.connected, true);
    assert.equal(h.nodes.some((node) => node.type === "input" && node.props.type === "password"), false);
    assert.deepEqual(calls, ["/api/v1/server/info"]);
  } finally { h.dispose(); }
});

test("expired authentication preserves the mounted workspace and same-instance drafts", async () => {
  let authorized = true;
  globalThis.fetch = async (url, init) => {
    if (url.endsWith("/session") && init.method === "POST") authorized = init.headers.Authorization === "Bearer good";
    return authorized ? response(info()) : new Response(null, { status: 401 });
  };
  const h = harness();
  try {
    await h.settle();
    h.app.props.onEditorStateChange({ dirty: true, busy: false });
    authorized = false; session.invalidateBrowserSession(); await h.settle();
    assert.equal(h.app.key, "server-A");
    assert.equal(h.app.props.connected, false);
    h.nodes.find((node) => node.type === "input").props.onChange({ target: { value: "good" } });
    await h.settle(); h.submit(); await h.settle();
    assert.equal(h.app.key, "server-A");
    assert.equal(h.app.props.connected, true);
    assert.equal(h.nodes.some((node) => node.type === "input"), false);
  } finally { h.dispose(); }
});

test("replacing the Server asks before discarding dirty drafts and cannot send commands while declined", async () => {
  let current = info();
  globalThis.fetch = async () => response(current);
  const h = harness();
  try {
    await h.settle();
    h.app.props.onEditorStateChange({ dirty: true, busy: false });
    current = info("server-B");
    assert.equal(await session.verifyBrowserSession(), false);
    await h.settle();
    let confirmations = 0;
    window.confirm = () => { confirmations++; return false; };
    h.submit(); await h.settle();
    assert.equal(confirmations, 1);
    assert.equal(h.app.key, "server-A");
    assert.equal(h.app.props.connected, false);
    await assert.rejects(requestJson("/api/v1/engine/start", { method: "POST" }), /Reconnect/);
    window.confirm = () => true;
    h.submit(); await h.settle();
    assert.equal(h.app.key, "server-B");
    assert.equal(h.app.props.connected, true);
  } finally { h.dispose(); }
});

test("browser disconnect checks drafts and only deletes its session", async () => {
  const calls = [];
  let authorized = true;
  globalThis.fetch = async (url, init) => {
    calls.push([url, init.method]);
    if (init.method === "DELETE") { authorized = false; return response(null); }
    return authorized ? response(info()) : new Response(null, { status: 401 });
  };
  const h = harness();
  try {
    await h.settle();
    h.app.props.onEditorStateChange({ dirty: true, busy: false });
    const disconnect = () => h.nodes.find((node) => node.type === "button" && node.props.children === "Disconnect").props.onClick();
    window.confirm = () => false;
    disconnect(); await h.settle();
    assert.equal(calls.some(([, method]) => method === "DELETE"), false);
    assert.equal(h.app.props.connected, true);
    window.confirm = () => true;
    disconnect(); await h.settle();
    assert.equal(h.app, undefined);
    assert.equal(calls.filter(([, method]) => method === "DELETE").length, 1);
    assert.equal(calls.every(([url]) => url.startsWith("/api/v1/server/")), true);
  } finally { h.dispose(); }
});

test("results reconnect checks identity and uses browser cookies without URL credentials", async () => {
  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; }
  };
  globalThis.fetch = async () => response(info());
  const events = [];
  const socket = openResultsSocket((event) => events.push(event));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sockets[0].url, "ws://localhost:5173/api/v1/ws/results");
  sockets[0].onopen();
  assert.deepEqual(events, [{ type: "open" }]);
  globalThis.fetch = async () => new Response(null, { status: 401 });
  sockets[0].onclose();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(session.browserSessionReady(), false);
  assert.equal(events.at(-1).type, "closed");
  socket.close();
});

test("a stale session check cannot invalidate a newly authenticated workspace", async () => {
  let release;
  globalThis.fetch = () => new Promise((resolve) => { release = resolve; });
  const checking = session.verifyBrowserSession();
  session.invalidateBrowserSession();
  session.acceptBrowserSession(info("server-B"));
  release(new Response(null, { status: 401 }));
  assert.equal(await checking, false);
  assert.equal(session.browserSessionReady(), true);
});

test("an aborted old response cannot invalidate a newly authenticated workspace", async () => {
  let release;
  globalThis.fetch = () => new Promise((resolve) => { release = resolve; });
  const request = requestJson("/api/v1/engine/status");
  session.invalidateBrowserSession();
  session.acceptBrowserSession(info("server-B"));
  release(new Response(null, { status: 401 }));
  await assert.rejects(request, { name: "AbortError" });
  assert.equal(session.browserSessionReady(), true);
});


test("Web StrictMode replay completes initial module loading when accepted revisions stay null", async () => {
  const originalModule = pluginConfigApi.module;
  const calls = [];
  pluginConfigApi.module = async (module) => { calls.push(module); return moduleConfiguration().modules[module]; };
  const h = harness(() => React.createElement("output", { current: useCurrentConfiguration(revisionMap(null), {}) }), true);
  try {
    await h.settle();
    const current = h.nodes.find((node) => node.type === "output").props.current;
    for (const module of ["acquisition", "processing", "stimulation", "actuation"]) {
      assert.equal(current.modules[module].ready, true, `${module} should finish loading after effect replay`);
      assert.equal(calls.filter((value) => value === module).length, 2);
    }
  } finally { h.dispose(); pluginConfigApi.module = originalModule; }
});
