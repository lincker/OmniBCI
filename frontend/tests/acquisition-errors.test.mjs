/** Command error details reach shared acquisition controls without changing other clients. */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { api, ApiError } = await server.ssrLoadModule("/src/api/client.ts");
const { useAcquisition } = await server.ssrLoadModule("/src/hooks/useAcquisition.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const failure = (error_code) => ({ success: false, code: "COMMAND_EXECUTION_FAILED", message: "Command execution failed",
  detail: { command_id: "connect-failed", command: "ConnectDataProvider", error_code } });

function sourceFixture(error) {
  const state = { error, connected: false, revision: "rev-1", calls: [] };
  globalThis.fetch = async (path, options = {}) => {
    state.calls.push([path, options.method ?? "GET"]);
    if (path === "/api/v1/acquisition/disconnect") {
      state.connected = false;
      return Response.json({ success: true, message: "Data provider disconnected", data: null });
    }
    if (path === "/api/v1/acquisition/connect") {
      if (state.error) return Response.json(state.error, { status: 500 });
      state.connected = true;
      const request = JSON.parse(options.body);
      return Response.json({ success: true, data: { operation_id: request.operation_id, module: "acquisition", status: "succeeded",
        target_revisions: { acquisition: state.revision }, result: { configuration_applied: true, action: "connected" } } });
    }
    const data = path === "/api/v1/acquisition/status"
      ? { connected: state.connected, streaming: false, connection_id: state.connected ? "source-1" : null }
      : assert.fail(`Unexpected request: ${path}`);
    return Response.json({ success: true, message: "ok", data });
  };
  return state;
}

function controllerHarness(runtimeActions = {}) {
  const values = [], refs = [];
  let language = "en";
  const runtime = { refresh: async () => {}, execute: async (module, configuration, send, onApplied) => {
    const result = await send({ operation_id: crypto.randomUUID(), configuration });
    if (result.result?.configuration_applied) onApplied?.(result);
    return result;
  }, ...runtimeActions };
  return {
    setLanguage(next) { language = next; },
    render() {
      let stateIndex = 0, refIndex = 0;
      const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
      const previous = internals.current;
      internals.current = {
        useContext: () => ({ language, t: (key, params) => translate(language, key, params) }),
        useState(initial) {
          const index = stateIndex++;
          if (index >= values.length) values[index] = typeof initial === "function" ? initial() : initial;
          return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
        },
        useRef: (value) => refs[refIndex++] ??= { current: value },
        useCallback: (callback) => callback,
        useEffect: () => {},
      };
      try { return useAcquisition(runtime, async () => {}, { acknowledge() {} }); }
      finally { internals.current = previous; }
    },
  };
}

for (const method of ["connect", "status"]) test(`${method} errors retain response details without rewriting the original ApiError message`, async () => {
  const response = failure("ConnectionRefusedError");
  globalThis.fetch = async () => Response.json(response, { status: 500 });
  await assert.rejects(method === "connect" ? api.acqConnect() : api.acquisitionStatus(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 500);
    assert.equal(error.code, response.code);
    assert.equal(error.message, response.message);
    assert.deepEqual(error.detail, response.detail);
    return true;
  });
});

for (const [code, key] of [["ConnectionRefusedError", "acquisition.error.connectionRefused"], ["TimeoutError", "acquisition.error.deviceTimeout"]]) {
  test(`${code} displays a specific acquisition hint and still refreshes failed connection state`, async () => {
    const state = sourceFixture(failure(code));
    const harness = controllerHarness();
    await harness.render().connect.run(currentSource());
    const result = harness.render();
    assert.equal(result.connect.error, `[COMMAND_EXECUTION_FAILED] ${translate("en", key)}`);
    assert.equal(result.data.connected, false);
    assert.equal(result.pending, false);
    assert.equal(state.calls.filter(([path]) => path.endsWith("/connect")).length, 1);
    assert.match(result.connect.error, code === "ConnectionRefusedError" ? /refused the connection/ : /did not respond in time/);
    harness.setLanguage("zh");
    assert.match(harness.render().connect.error, code === "ConnectionRefusedError" ? /拒绝连接/ : /未及时响应/);
    assert.equal(harness.render().connect.error, `[COMMAND_EXECUTION_FAILED] ${translate("zh", key)}`);
    state.error = null;
    await harness.render().connect.run(currentSource());
    assert.equal(harness.render().connect.error, null);
    assert.equal(harness.render().data.connected, true);
  });
}

test("unknown, missing or malformed details preserve the original acquisition error message", async () => {
  for (const detail of [undefined, null, "ConnectionRefusedError", ["ConnectionRefusedError"], {},
    { error_code: "OSError" }, { error_code: { name: "TimeoutError" } }]) {
    sourceFixture({ success: false, code: "COMMAND_EXECUTION_FAILED", message: "Original device failure", detail });
    const harness = controllerHarness();
    await harness.render().connect.run(currentSource());
    assert.equal(harness.render().connect.error, "[COMMAND_EXECUTION_FAILED] Original device failure");
  }
});

test("other API codes do not reinterpret detail as an acquisition execution failure", async () => {
  sourceFixture({ success: false, code: "COMMAND_REJECTED", message: "Source is already reading", detail: { error_code: "TimeoutError" } });
  const harness = controllerHarness();
  await harness.render().connect.run(currentSource());
  assert.equal(harness.render().connect.error, "[COMMAND_REJECTED] Source is already reading");
});

test("other control clients and existing three-argument ApiErrors keep their message", async () => {
  const legacy = new ApiError(409, "CONFLICT", "Selection changed");
  assert.equal(legacy.message, "Selection changed");
  assert.equal(legacy.detail, undefined);
  globalThis.fetch = async () => Response.json(failure("TimeoutError"), { status: 500 });
  await assert.rejects(api.engineStart(), (error) => {
    assert.equal(error.message, "Command execution failed");
    assert.equal(error.detail.error_code, "TimeoutError");
    return true;
  });
});

const currentSource = () => ({ settings: {}, expected_revision: "rev-1", bindings: [{
  slot: "acquisition.source", selection: { package_id: "example.source", contribution_id: "main", version: "1.0.0" },
  parameters: { host: "192.168.0.20", port: 4000 },
}] });

test("Disconnect sends scoped recovery even when the acquisition worker is already disconnected", async () => {
  const state = sourceFixture(null);
  const harness = controllerHarness({ status: { recovery: { acquisition: "Worker state unknown" } } });
  await harness.render().disconnect.run();
  assert.equal(harness.render().disconnect.error, null);
  assert.equal(state.calls.filter(([path]) => path.endsWith("/disconnect")).length, 1);
});

test("acqMetadata uses read-only GET while inspect.run uses POST /metadata/refresh", async () => {
  const calls = [];
  let connectionId = "source-1";
  globalThis.fetch = async (path, options = {}) => {
    calls.push([path, options.method ?? "GET"]);
    if (path === "/api/v1/acquisition/status") {
      return Response.json({ success: true, message: "ok", data: { connected: Boolean(connectionId), streaming: false, connection_id: connectionId, metadata_available: true } });
    }
    if (path === "/api/v1/acquisition/metadata" || path === "/api/v1/acquisition/metadata/refresh") {
      return Response.json({ success: true, message: "ok", data: { source_id: "curry8", connection_id: connectionId, metadata: options.method ?? "GET", descriptor: null } });
    }
    return assert.fail(`Unexpected request: ${path}`);
  };
  const readOnly = await api.acqMetadata();
  assert.equal(readOnly.metadata, "GET");
  assert.deepEqual(calls, [["/api/v1/acquisition/metadata", "GET"]]);

  const harness = controllerHarness();
  await harness.render().refresh();
  await harness.render().inspect.run();
  assert.equal(harness.render().metadata.metadata, "POST");
  assert.ok(calls.some(([path, method]) => path === "/api/v1/acquisition/metadata/refresh" && method === "POST"));

  connectionId = "source-2";
  await harness.render().refresh();
  assert.equal(harness.render().metadata, null);
});
