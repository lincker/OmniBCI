/** Preparation and quick start reuse pinned selections without saving or applying edits. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { connectSource, startReading } = await server.ssrLoadModule("/src/source-lifecycle.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const revision = "saved-startup-revision";

async function fixture(action, configure = () => {}) {
  const original = [{ ...api }, { ...pluginConfigApi }, { ...runtimeConfigApi }];
  const calls = [];
  const state = {
    runtime: { running: false, accepted_revision: revision, applied_revision: null, generation: null,
      recovery_required: false, operation: null },
    acquisition: { connected: false, streaming: false, connection_id: null },
    selection: { revision, bindings: [{ slot: "acquisition.source", profile_id: "lab", profile_revision: 2 }] },
  };
  api.acquisitionStatus = async () => structuredClone(state.acquisition);
  runtimeConfigApi.status = async () => structuredClone(state.runtime);
  pluginConfigApi.activation = async () => structuredClone(state.selection);
  api.engineStart = async () => {
    calls.push("engine"); Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" });
  };
  api.acqConnect = async () => {
    calls.push("connect"); Object.assign(state.acquisition, { connected: true, connection_id: "connection-1" });
  };
  api.acqStreamStart = async () => { calls.push("read"); state.acquisition.streaming = true; };
  for (const name of ["acqMetadata", "engineStop", "acqDisconnect", "stimStart", "trialStart"]) {
    api[name] = async () => assert.fail(`Unexpected ${name}`);
  }
  for (const name of ["create", "update"]) pluginConfigApi[name] = async () => assert.fail(`Unexpected profile ${name}`);
  for (const name of ["plan", "apply"]) runtimeConfigApi[name] = async () => assert.fail(`Unexpected configuration ${name}`);
  configure(state);
  try { await action({ state, calls }); }
  finally { Object.assign(api, original[0]); Object.assign(pluginConfigApi, original[1]); Object.assign(runtimeConfigApi, original[2]); }
}

function connected(state) {
  Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" });
  Object.assign(state.acquisition, { connected: true, streaming: false, connection_id: "connection-1" });
}

test("Connect starts the engine if needed and connects without metadata or reading", async () => {
  await fixture(async ({ calls }) => {
    const result = await connectSource(revision);
    assert.deepEqual(calls, ["engine", "connect"]);
    assert.equal(result.acquisition.connected, true);
    assert.equal(result.acquisition.streaming, false);
    assert.equal(result.acquisition.connection_id, "connection-1");
  });
});

test("Connect reuses an existing connection without reconnecting", async () => {
  await fixture(async ({ calls }) => { await connectSource(revision); assert.deepEqual(calls, []); }, connected);
});

for (const running of [false, true]) test(`Start reading refuses an unconnected source with engine running=${running}`, async () => {
  await fixture(async ({ calls }) => {
    await assert.rejects(startReading(revision), /connect/i);
    assert.deepEqual(calls, []);
  }, (state) => { if (running) Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" }); });
});

test("Start reading only starts reading on the existing connection", async () => {
  await fixture(async ({ calls }) => {
    const result = await startReading(revision);
    assert.deepEqual(calls, ["read"]);
    assert.equal(result.acquisition.streaming, true);
  }, connected);
});

test("a source already reading is not restarted", async () => {
  await fixture(async ({ calls }) => { await startReading(revision); assert.deepEqual(calls, []); },
    (state) => { connected(state); state.acquisition.streaming = true; });
});

for (const [name, configure] of [
  ["missing source", (state) => { state.selection.bindings = []; }],
  ["changed selection", (state) => { state.runtime.accepted_revision = "changed"; }],
  ["changed activation", (state) => { state.selection.revision = "changed"; }],
  ["recovery required", (state) => { state.runtime.recovery_required = true; }],
  ["configuration operation", (state) => { state.runtime.operation = { status: "queued" }; }],
  ["different applied configuration", (state) => { connected(state); state.runtime.applied_revision = "other"; }],
  ["missing connection identity", (state) => { connected(state); state.acquisition.connection_id = null; }],
]) test(`${name} blocks connecting without changing configuration or runtime`, async () => {
  await fixture(async ({ calls }) => { await assert.rejects(connectSource(revision)); assert.deepEqual(calls, []); }, configure);
});

test("a changed revision after engine startup stops before connecting", async () => {
  await fixture(async ({ state, calls }) => {
    const start = api.engineStart;
    api.engineStart = async () => { await start(); state.runtime.accepted_revision = "changed"; };
    await assert.rejects(connectSource(revision), /configuration changed/i);
    assert.deepEqual(calls, ["engine"]);
  });
});

test("an engine restart during connection is rejected", async () => {
  await fixture(async ({ state, calls }) => {
    const connect = api.acqConnect;
    api.acqConnect = async () => { await connect(); state.runtime.generation = "engine-2"; };
    await assert.rejects(connectSource(revision), /engine changed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a replaced connection before reading is rejected", async () => {
  await fixture(async ({ state, calls }) => {
    const status = api.acquisitionStatus;
    let reads = 0;
    api.acquisitionStatus = async () => { if (++reads === 2) state.acquisition.connection_id = "replacement"; return status(); };
    await assert.rejects(startReading(revision), /connection changed/i);
    assert.deepEqual(calls, []);
  }, connected);
});

for (const stage of ["engine", "connect", "read"]) test(`${stage} failure does not retry a write or undo completed steps`, async () => {
  await fixture(async ({ calls }) => {
    const method = { engine: "engineStart", connect: "acqConnect", read: "acqStreamStart" }[stage];
    api[method] = async () => { calls.push(stage); throw new Error(`${stage} failed`); };
    await assert.rejects(stage === "read" ? startReading(revision) : connectSource(revision), new RegExp(`${stage} failed`));
    assert.deepEqual(calls, stage === "read" ? ["read"] : stage === "engine" ? ["engine"] : ["engine", "connect"]);
  }, stage === "read" ? connected : undefined);
});

test("successful connection responses without confirmed device state are not reported as success", async () => {
  await fixture(async ({ calls }) => {
    api.acqConnect = async () => { calls.push("connect"); };
    await assert.rejects(connectSource(revision), /connection was not confirmed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a short recording reaching EOF after acknowledged reading is not a failure", async () => {
  await fixture(async ({ calls, state }) => {
    api.acqStreamStart = async () => { calls.push("read"); state.acquisition.streaming = false; };
    const result = await startReading(revision);
    assert.equal(result.acquisition.streaming, false);
    assert.deepEqual(calls, ["read"]);
  }, connected);
});

test("unavailable status aborts before starting the engine", async () => {
  await fixture(async ({ calls }) => {
    api.acquisitionStatus = async () => null;
    await assert.rejects(connectSource(revision), /status is unavailable/i);
    assert.deepEqual(calls, []);
  });
});
