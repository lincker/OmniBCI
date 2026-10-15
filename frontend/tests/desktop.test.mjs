/** Verify desktop boundaries with delayed responses and untrusted connection changes. */
import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { readFile } from "node:fs/promises";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
after(async () => { globalThis.fetch = originalFetch; globalThis.window = originalWindow; await server.close(); });
const desktop = await server.ssrLoadModule("/src/desktop.ts");
const { requestApi, requestJson } = await server.ssrLoadModule("/src/api/transport.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { pluginMarketApi } = await server.ssrLoadModule("/src/api/plugin-market.ts");
const { openResultsSocket } = await server.ssrLoadModule("/src/api/results-socket.ts");
const { saveSessionResults, exportPath } = await server.ssrLoadModule("/src/ResultExport.tsx");
const { SchemaForm, schemaErrors } = await server.ssrLoadModule("/src/SchemaForm.tsx");
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const { I18nProvider } = await server.ssrLoadModule("/src/i18n.tsx");
const { recordingPathMissing } = await server.ssrLoadModule("/src/RunConsole.tsx");
const ready = (id = "backend-A", kind = "local") => ({ id, kind, status: "ready", url: "http://127.0.0.1:49152" });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
beforeEach(() => {
  globalThis.window = { omnibciDesktop: {}, localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
  globalThis.fetch = originalFetch;
  desktop.setDesktopConnection(ready());
});

test("all REST entrypoints carry the current connection and preserve ZIP bytes", async () => {
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, ...init });
    return Response.json({ success: true, data: [] });
  };
  await api.engineStatus();
  await pluginConfigApi.profiles();
  const archive = new File([new Uint8Array([0, 255, 3, 7])], "plugin.zip", { type: "application/zip" });
  await pluginMarketApi.upload(archive);
  assert.equal(requests.length, 3);
  requests.forEach((request) => {
    assert.ok(request.path.startsWith("app://omnibci/api/v1/"), "Desktop development requests must not bypass the main-process proxy");
    assert.equal(request.headers.get("X-OmniBCI-Connection-Id"), "backend-A");
    assert.equal(request.redirect, "error");
  });
  assert.equal(requests[2].body, archive);
  assert.equal(requests[2].headers.get("Content-Type"), "application/zip");
});

test("switching backend aborts the request and discards a delayed consumed response", async () => {
  const body = deferred();
  const started = deferred();
  let signal;
  globalThis.fetch = async (_path, options) => {
    signal = options.signal;
    started.resolve();
    return { json: () => body.promise };
  };
  const request = requestJson("/api/v1/engine/status");
  await started.promise;
  desktop.setDesktopConnection(ready("backend-B", "remote"));
  assert.equal(signal.aborted, true);
  body.resolve({ success: true, data: { backend: "A" } });
  await assert.rejects(request, { name: "AbortError" });
});

test("a lost connection aborts pending work even when the same instance recovers", async () => {
  const body = deferred();
  const started = deferred();
  globalThis.fetch = async () => { started.resolve(); return { json: () => body.promise }; };
  const request = requestJson("/api/v1/engine/status");
  await started.promise;
  desktop.setDesktopConnection({ ...ready(), status: "failed" });
  desktop.setDesktopConnection(ready());
  body.resolve({ data: "stale" });
  await assert.rejects(request, { name: "AbortError" });
});

test("offline commands never reach fetch and interrupted writes are not retried", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new TypeError("Connection reset"); };
  desktop.setDesktopConnection({ ...ready(), status: "failed" });
  await assert.rejects(api.engineStart(), /Reconnect before sending commands/);
  assert.equal(calls, 0);
  desktop.setDesktopConnection(ready());
  await assert.rejects(api.engineStart(), /may have applied the change/);
  assert.equal(calls, 1);
});

test("browser transport keeps same-origin API behavior without desktop headers", async () => {
  delete window.omnibciDesktop;
  globalThis.fetch = async (path, options) => {
    assert.equal(path, "/api/v1/engine/status");
    assert.equal(options.headers.has("X-OmniBCI-Connection-Id"), false);
    return Response.json({ success: true, data: { running: false } });
  };
  assert.deepEqual(await api.engineStatus(), { running: false });
});

test("the transport rejects arbitrary URLs without issuing a request", async () => {
  globalThis.fetch = async () => assert.fail("Unexpected request");
  await assert.rejects(requestApi("https://other.example/api/v1/config", {}, async () => null), /Invalid API path/);
});

test("desktop results subscriptions suppress old connection events and close once", () => {
  let deliver;
  let unsubscribed = 0;
  window.omnibciDesktop.subscribeResults = (id, listener) => {
    assert.equal(id, "backend-A"); deliver = listener;
    return () => { unsubscribed += 1; };
  };
  const events = [];
  const socket = openResultsSocket((event) => events.push(event));
  deliver({ type: "open" });
  deliver({ type: "message", data: "first" });
  desktop.setDesktopConnection(ready("backend-B"));
  deliver({ type: "message", data: "old backend" });
  socket.close(); socket.close();
  deliver({ type: "message", data: "after close" });
  assert.deepEqual(events, [{ type: "open" }, { type: "message", data: "first" }, { type: "closed" }]);
  assert.equal(unsubscribed, 1);
});

test("export passes exact selected sessions to the native saver and preserves cancellation", async () => {
  window.omnibciDesktop.saveResults = async (input) => {
    assert.deepEqual(input, { connectionId: "backend-A", format: "csv", sessionIds: ["a/b", "session 2"] });
    return { canceled: true };
  };
  assert.deepEqual(await saveSessionResults("csv", ["a/b", "session 2"]), { canceled: true });
  window.omnibciDesktop.saveResults = async () => { throw new Error("Disk full"); };
  await assert.rejects(saveSessionResults("json", []), /Disk full/);
  assert.equal(exportPath("csv", ["a/b", "session 2"]), "/api/v1/sessions/export?format=csv&session_id=a%2Fb&session_id=session+2");
});

test("file fields allow blank setup and offer native selection only on the local backend", () => {
  const schema = { type: "object", required: ["path"], properties: {
    path: { type: "string", default: "", "x-widget": "file-path", "x-scope": "machine" },
  } };
  assert.deepEqual(schemaErrors(schema, { path: "" }), []);
  const render = () => renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(SchemaForm, { schema, value: { path: "" }, onChange: () => {} })));
  assert.match(render(), /Browse…/);
  desktop.setDesktopConnection(ready("remote", "remote"));
  assert.doesNotMatch(render(), /Browse…/);
  assert.match(render(), /Path on the backend computer/);
  delete window.omnibciDesktop;
  assert.doesNotMatch(render(), /Browse…/);
});

test("an offline desktop workspace keeps the current page visible with its controls disabled", () => {
  Object.assign(window, { location: { hash: "#run" }, localStorage: { getItem: () => "en" }, navigator: { language: "en" } });
  const render = (status) => renderToStaticMarkup(React.createElement(I18nProvider, null,
    React.createElement(App, { desktopConnection: { ...ready(), status } })));
  const offline = render("failed");
  assert.match(offline, /Run console/);
  assert.match(offline, /<fieldset class="desktop-control-guard" disabled=""/);
  assert.doesNotMatch(render("ready"), /<fieldset class="desktop-control-guard" disabled/);
});

test("the built-in manifest's qualified File Reader identity requires a recording before starting", async () => {
  const manifest = await readFile(new URL("../../omnibci/infrastructure/plugins/acquisition/file/manifest.toml", import.meta.url), "utf8");
  const identity = `${/^publisher = "([^"]+)"/m.exec(manifest)[1]}.${/^id = "([^"]+)"/m.exec(manifest)[1]}`;
  const selected = (path, packageId = identity) => ({ bindings: [{ slot: "acquisition.source", selection: { package_id: packageId }, parameters: { data_file_path: path } }] });
  assert.equal(identity, "OmniBCI.file_reader");
  for (const value of [undefined, "", "   "]) assert.equal(recordingPathMissing(selected(value)), true);
  assert.equal(recordingPathMissing(selected("/recordings/subject.cnt")), false);
  assert.equal(recordingPathMissing(selected("", "example.device")), false);
  assert.equal(recordingPathMissing(null), false);
});
