/** Acquisition source error details, connection state, and read-only source metadata tests. */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { elements, text } from "./module-fixtures.mjs";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { api, ApiError } = await server.ssrLoadModule("/src/api/client.ts");
const { useAcquisition } = await server.ssrLoadModule("/src/hooks/useAcquisition.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { AcquisitionMetadata } = await server.ssrLoadModule("/src/AcquisitionMetadata.tsx");
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

function render(overrides = {}) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  dispatcher.current = { useContext: () => english };
  try {
    return AcquisitionMetadata({ value: null, busy: false, canRead: true,
      readHint: "Connect the source before reading metadata.", onRead: () => {}, ...overrides });
  } finally { dispatcher.current = previous; }
}
const descriptor = {
  source_id: "curry8", connection_id: "connection-a", sample_rate: 500,
  signal_channel_count: 2, output_row_count: 3, trigger_row_index: 2,
  native_event: "final-row", provenance: "device-reported",
  channels: ["C3", "C4"].map((name, index) => ({ index, channel_id: `channel-${index}`, name, signal_type: "unknown" })),
};
const source = (metadata, output = descriptor) => ({ source_id: "curry8", connection_id: "connection-a", metadata, descriptor: output });
function summary(tree, label) {
  const group = elements(tree).find((element) => element.type === "dl");
  return elements(group).find((element) => element.type === "div" && text(element.props.children[0]) === label)?.props.children[1];
}

test("metadata is requested explicitly and an empty page never invents source fields", () => {
  let reads = 0;
  const tree = render({ onRead: () => { reads += 1; } });
  assert.equal(reads, 0);
  const button = elements(tree).find((element) => element.type === "button");
  assert.equal(text(button), "Get Metadata");
  button.props.onClick();
  assert.equal(reads, 1);
  assert.match(text(tree), /No metadata has been retrieved/);
  assert.equal(elements(tree).filter((element) => ["table", "input", "select", "textarea", "dl"].includes(element.type)).length, 0);
  assert.doesNotMatch(text(tree), /CH1|Manual|electrode/i);
});

test("Get Metadata is unavailable before connection and during an operation", () => {
  for (const props of [{ canRead: false }, { busy: true }]) {
    const button = elements(render(props)).find((element) => element.type === "button");
    assert.equal(button.props.disabled, true);
  }
});

test("Curry8 signal channel count comes from the descriptor and excludes the event output row", () => {
  const tree = render({ value: source("Neuroscan CURRY 8\nConnected channels: 2") });
  assert.equal(text(summary(tree, "Sample rate (Hz)")), "500");
  assert.equal(text(summary(tree, "Signal channels")), "2");
  assert.equal(text(summary(tree, "Output rows (including event)")), "3");
  const rows = elements(tree).find((element) => element.type === "tbody").props.children;
  assert.equal(rows.length, 2);
  assert.equal(text(rows[0].props.children[1]), "channel-0");
  assert.equal(text(rows[0].props.children[3]), "unknown");
  assert.equal(text(rows[0].props.children[2]), "C3");
  assert.equal(text(rows[1].props.children[2]), "C4");
});

test("metadata preserves opaque source text and whitespace in a visible read-only display", () => {
  const metadata = '  Device response\r\n\tFirmware: 8.1\n<status>ready & connected</status>\n';
  const value = source(metadata, null);
  const original = structuredClone(value);
  const tree = render({ value });
  assert.equal(elements(tree).filter((element) => ["input", "select", "textarea", "details"].includes(element.type)).length, 0);
  const raw = elements(tree).find((element) => element.type === "pre");
  assert.equal(raw.props["aria-label"], "Source response");
  assert.equal(text(raw), metadata);
  assert.equal(raw.props.dangerouslySetInnerHTML, undefined);
  assert.match(renderToStaticMarkup(raw), /&lt;status&gt;ready &amp; connected&lt;\/status&gt;/);
  assert.deepEqual(value, original);
  assert.equal(summary(tree, "Signal channels"), undefined);
});

test("JSON-looking source text is not interpreted as adapter output", () => {
  const metadata = '{"sample_rate": 1000, "channel_count": 99, "channel_labels": ["Cz"]}';
  const tree = render({ value: source(metadata, null) });
  assert.equal(text(elements(tree).find((element) => element.type === "pre")), metadata);
  assert.equal(summary(tree, "Sample rate (Hz)"), undefined);
  assert.equal(summary(tree, "Signal channels"), undefined);
  assert.equal(elements(tree).filter((element) => element.type === "table").length, 0);
  assert.match(text(tree), /adapter output description is unavailable/i);
});

test("an empty source response remains an empty string", () => {
  const tree = render({ value: source("", null) });
  assert.equal(text(elements(tree).find((element) => element.type === "pre")), "");
  assert.doesNotMatch(text(tree), /No metadata has been retrieved/);
});

test("configured descriptors are identified without interpreting source response text", () => {
  const value = source("This source does not support device metadata.", {
    ...descriptor, source_id: "scan45", provenance: "user-declared",
  });
  const tree = render({ value });
  assert.match(text(tree), /does not support device metadata/);
  assert.match(text(tree), /adapter output description comes from connection configuration/i);
  assert.equal(text(summary(tree, "Source")), "scan45");
  assert.equal(text(summary(tree, "Signal channels")), "2");
  assert.equal(text(elements(tree).find((element) => element.type === "pre")), value.metadata);
});

const comparison = (status, expected = null) => ({ status,
  sample_rate: { expected: expected?.sample_rate ?? null, actual: 500 },
  channel_count: { expected: expected?.channel_count ?? null, actual: 3 },
});

test("source comparison treats unspecified expectations as optional and keeps adoption explicit", () => {
  let adoptions = 0;
  const value = { ...source("Device information"), comparison: comparison("unconfirmed") };
  const tree = render({ value, canUseObserved: true, onUseObserved: () => { adoptions += 1; } });
  assert.equal(adoptions, 0);
  const table = elements(tree).find((element) => element.type === "table" && element.props["aria-label"] === "Experiment parameter comparison");
  assert.match(text(table), /Expected \(applied configuration\)Observed \(data source\)/);
  assert.match(text(table), /Use source value500/);
  assert.match(text(table), /Use source value3/);
  assert.match(text(tree), /Unspecified expectations use the observed source values/);
  assert.equal(elements(tree).some((element) => element.props.role === "alert"), false);
  const adopt = elements(tree).find((element) => element.type === "button" && text(element) === "Use observed values");
  assert.equal(adopt.props.disabled, false);
  adopt.props.onClick();
  assert.equal(adoptions, 1);
});

test("mismatched and matched observations display the comparison without changing source values", () => {
  for (const status of ["mismatched", "matched"]) {
    const value = { ...source("Device information"), comparison: comparison(status, { sample_rate: status === "matched" ? 500 : 1000, channel_count: 3 }) };
    const before = structuredClone(value);
    const tree = render({ value });
    assert.match(text(tree), status === "matched" ? /observed source values match/ : /observed source values differ/);
    assert.deepEqual(value, before);
  }
});

test("the adoption action is disabled when the current source is unavailable or busy", () => {
  for (const overrides of [{ canUseObserved: false }, { busy: true }]) {
    const value = { ...source("Device information"), comparison: comparison("unconfirmed") };
    const tree = render({ value, canUseObserved: true, onUseObserved: () => {}, ...overrides });
    const adopt = elements(tree).find((element) => element.type === "button" && text(element) === "Use observed values");
    assert.equal(adopt.props.disabled, true);
  }
});

test("Scan45 declarations are shown as unverified and have no adoption action", () => {
  const value = { ...source("Metadata is not implemented.", { ...descriptor, provenance: "user-declared" }),
    comparison: comparison("unverified", { sample_rate: 500, channel_count: 3 }) };
  const tree = render({ value, canUseObserved: true, onUseObserved: () => {} });
  assert.match(text(tree), /cannot independently verify/);
  assert.match(text(tree), /Declared \(configuration\)/);
  assert.doesNotMatch(text(tree), /Use observed values/);
});

test("missing source information offers no adoption action", () => {
  for (const value of [null, source("Device information", null), source("Device information")]) {
    const tree = render({ value, canUseObserved: true, onUseObserved: () => {} });
    assert.doesNotMatch(text(tree), /Use observed values/);
  }
});

test("new drafts show the source observation without comparing an absent saved baseline", () => {
  const value = { ...source("Device information"), comparison: comparison("unconfirmed") };
  const tree = render({ value, showComparison: false });
  assert.equal(text(summary(tree, "Sample rate (Hz)")), "500");
  assert.equal(text(summary(tree, "Output rows (including event)")), "3");
  assert.equal(text(elements(tree).find((element) => element.type === "pre")), "Device information");
  assert.doesNotMatch(text(tree), /Experiment parameter comparison|no complete confirmed baseline/);
  assert.equal(elements(tree).some((element) => element.props.role === "alert"), false);
});

test("saved profiles warn in red only for explicitly configured mismatches", () => {
  for (const channel_count of [null, 3]) {
    const value = { ...source("Device information"), comparison: comparison("mismatched", {
      sample_rate: 1000, channel_count,
    }) };
    const tree = render({ value });
    const warning = elements(tree).find((element) => element.props.role === "alert");
    assert.ok(warning.props.className.includes("source-mismatch"));
    const rows = elements(tree).filter((element) => element.type === "tr" && element.key === "sample_rate");
    assert.equal(rows[0].props.className, "source-mismatch");
    const matching = elements(tree).find((element) => element.type === "tr" && element.key === "channel_count");
    assert.equal(matching.props.className, undefined);
  }
  for (const status of ["matched", "unverified", "unconfirmed"]) {
    const tree = render({ value: { ...source("Device information"), comparison: comparison(status, {
      sample_rate: status === "unconfirmed" ? null : 500, channel_count: 3,
    }) } });
    assert.equal(elements(tree).some((element) => element.props.role === "alert"), false);
    assert.equal(elements(tree).some((element) => element.props.className?.includes("source-mismatch")), false);
  }
});
