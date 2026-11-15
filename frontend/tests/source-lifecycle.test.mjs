/** Preparation and quick start reuse pinned selections without saving or applying edits. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { assertConfigurationRevision, connectSource, startReading } = await server.ssrLoadModule("/src/source-lifecycle.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const revision = "saved-startup-revision";
const expected = { acquisition: revision, system: "system-revision" };

async function fixture(action, configure = () => {}) {
  const original = [{ ...api }, { ...pluginConfigApi }, { ...runtimeConfigApi }];
  const calls = [];
  const state = {
    runtime: { running: false, accepted: { ...expected }, applied: null, generation: null,
      recovery: {}, operations: {} },
    acquisition: { connected: false, streaming: false, connection_id: null },
    selection: { revision, bindings: [{ slot: "acquisition.source", profile_id: "lab", profile_revision: 2 }] },
  };
  api.acquisitionStatus = async () => structuredClone(state.acquisition);
  runtimeConfigApi.status = async () => structuredClone(state.runtime);
  pluginConfigApi.module = async () => structuredClone(state.selection);
  pluginConfigApi.modules = async () => state.selection && ({
    modules: { acquisition: structuredClone(state.selection) }, system: { revision: expected.system, settings: {} },
  });
  api.engineStart = async () => {
    calls.push("engine"); Object.assign(state.runtime, { running: true, applied: { ...expected }, generation: "engine-1" });
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
  Object.assign(state.runtime, { running: true, applied: { ...expected }, generation: "engine-1" });
  Object.assign(state.acquisition, { connected: true, streaming: false, connection_id: "connection-1" });
}

test("Connect starts the engine if needed and connects without metadata or reading", async () => {
  await fixture(async ({ calls }) => {
    const result = await connectSource(expected);
    assert.deepEqual(calls, ["engine", "connect"]);
    assert.equal(result.acquisition.connected, true);
    assert.equal(result.acquisition.streaming, false);
    assert.equal(result.acquisition.connection_id, "connection-1");
  });
});

test("Connect reuses an existing connection without reconnecting", async () => {
  await fixture(async ({ calls }) => { await connectSource(expected); assert.deepEqual(calls, []); }, connected);
});

for (const running of [false, true]) test(`Start reading refuses an unconnected source with engine running=${running}`, async () => {
  await fixture(async ({ calls }) => {
    await assert.rejects(startReading(expected), /connect/i);
    assert.deepEqual(calls, []);
  }, (state) => { if (running) Object.assign(state.runtime, { running: true, applied: { ...expected }, generation: "engine-1" }); });
});

test("Start reading only starts reading on the existing connection", async () => {
  await fixture(async ({ calls }) => {
    const result = await startReading(expected);
    assert.deepEqual(calls, ["read"]);
    assert.equal(result.acquisition.streaming, true);
  }, connected);
});

test("a source already reading is not restarted", async () => {
  await fixture(async ({ calls }) => { await startReading(expected); assert.deepEqual(calls, []); },
    (state) => { connected(state); state.acquisition.streaming = true; });
});

for (const [name, configure] of [
  ["missing source", (state) => { state.selection.bindings = []; }],
  ["changed selection", (state) => { state.runtime.accepted.acquisition = "changed"; }],
  ["changed activation", (state) => { state.selection.revision = "changed"; }],
  ["recovery required", (state) => { state.runtime.recovery = { acquisition: "Unknown acquisition" }; }],
  ["configuration operation", (state) => { state.runtime.operations = { acquisition: { module: "acquisition", affected_modules: ["acquisition"], status: "queued" } }; }],
  ["different applied configuration", (state) => { connected(state); state.runtime.applied.acquisition = "other"; }],
  ["missing connection identity", (state) => { connected(state); state.acquisition.connection_id = null; }],
]) test(`${name} blocks connecting without changing configuration or runtime`, async () => {
  await fixture(async ({ calls }) => {
    await assert.rejects(connectSource(expected), name === "different applied configuration" ? /Stop and restart that module/ : undefined);
    assert.deepEqual(calls, []);
  }, configure);
});

test("a changed revision after engine startup stops before connecting", async () => {
  await fixture(async ({ state, calls }) => {
    const start = api.engineStart;
    api.engineStart = async () => { await start(); state.runtime.accepted.acquisition = "changed"; };
    await assert.rejects(connectSource(expected), /configuration changed/i);
    assert.deepEqual(calls, ["engine"]);
  });
});

test("a cancelled startup does not reconnect or report failure after Engine Stop completes", async () => {
  await fixture(async ({ state, calls }) => {
    let valid = true;
    api.engineStart = async () => {
      calls.push("engine");
      valid = false;
      Object.assign(state.runtime, { running: false, applied: null, generation: null });
    };
    assert.equal(await connectSource(expected, () => valid), undefined);
    assert.deepEqual(calls, ["engine"]);
  });
});

test("an engine restart during connection is rejected", async () => {
  await fixture(async ({ state, calls }) => {
    const connect = api.acqConnect;
    api.acqConnect = async () => { await connect(); state.runtime.generation = "engine-2"; };
    await assert.rejects(connectSource(expected), /engine changed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a replaced connection before reading is rejected", async () => {
  await fixture(async ({ state, calls }) => {
    const status = api.acquisitionStatus;
    let reads = 0;
    api.acquisitionStatus = async () => { if (++reads === 2) state.acquisition.connection_id = "replacement"; return status(); };
    await assert.rejects(startReading(expected), /connection changed/i);
    assert.deepEqual(calls, []);
  }, connected);
});

for (const stage of ["engine", "connect", "read"]) test(`${stage} failure does not retry a write or undo completed steps`, async () => {
  await fixture(async ({ calls }) => {
    const method = { engine: "engineStart", connect: "acqConnect", read: "acqStreamStart" }[stage];
    api[method] = async () => { calls.push(stage); throw new Error(`${stage} failed`); };
    await assert.rejects(stage === "read" ? startReading(expected) : connectSource(expected), new RegExp(`${stage} failed`));
    assert.deepEqual(calls, stage === "read" ? ["read"] : stage === "engine" ? ["engine"] : ["engine", "connect"]);
  }, stage === "read" ? connected : undefined);
});

test("successful connection responses without confirmed device state are not reported as success", async () => {
  await fixture(async ({ calls }) => {
    api.acqConnect = async () => { calls.push("connect"); };
    await assert.rejects(connectSource(expected), /connection was not confirmed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a short recording reaching EOF after acknowledged reading is not a failure", async () => {
  await fixture(async ({ calls, state }) => {
    api.acqStreamStart = async () => { calls.push("read"); state.acquisition.streaming = false; };
    const result = await startReading(expected);
    assert.equal(result.acquisition.streaming, false);
    assert.deepEqual(calls, ["read"]);
  }, connected);
});

test("unavailable status aborts before starting the engine", async () => {
  await fixture(async ({ calls }) => {
    api.acquisitionStatus = async () => null;
    await assert.rejects(connectSource(expected), /status is unavailable/i);
    assert.deepEqual(calls, []);
  });
});

test("configuration checks allow a stopped engine and do not issue control commands", async () => {
  await fixture(async ({ calls }) => {
    const runtime = await assertConfigurationRevision(expected);
    assert.equal(runtime.running, false);
    assert.deepEqual(calls, []);
  });
});

test("configuration checks confirm the expected running generation", async () => {
  await fixture(async ({ calls }) => {
    const runtime = await assertConfigurationRevision(expected, "engine-1");
    assert.deepEqual(runtime.applied, expected);
    assert.deepEqual(calls, []);
  }, connected);
});

for (const [name, configure, generation] of [
  ["a concurrent accepted revision", (state) => { state.runtime.accepted.acquisition = "new-selection"; }],
  ["a concurrent activation revision", (state) => { state.selection.revision = "new-selection"; state.runtime.accepted.acquisition = "new-selection"; }],
  ["a removed activation", (state) => { state.selection = null; state.runtime.accepted.acquisition = null; }],
  ["a different applied revision", (state) => { connected(state); state.runtime.applied.acquisition = "previous-selection"; }],
  ["an unacknowledged generation", (state) => { connected(state); state.runtime.generation = null; }],
  ["a replaced generation", connected, "engine-2"],
  ["a pending operation", (state) => { state.runtime.operations = { acquisition: { module: "acquisition", affected_modules: ["acquisition"], status: "applying" } }; }],
  ["a recovery state", (state) => { state.runtime.recovery = { acquisition: "Unknown acquisition" }; }],
]) test(`${name} prevents a subsequent engine start or stimulation launch`, async () => {
  await fixture(async ({ calls }) => {
    await assert.rejects(async () => {
      await assertConfigurationRevision(expected, generation);
      await api.engineStart();
      await api.stimStart();
    });
    assert.deepEqual(calls, []);
  }, configure);
});

for (const module of ["processing", "stimulation", "actuation"]) test(`${module} changes do not block source connection`, async () => {
  await fixture(async ({ calls }) => {
    await connectSource(expected);
    assert.deepEqual(calls, []);
  }, (state) => {
    connected(state);
    state.runtime.accepted[module] = "new";
    state.runtime.applied[module] = "old";
  });
});

for (const field of ["accepted", "applied"]) test(`a changed ${field} system revision blocks source commands`, async () => {
  await fixture(async ({ calls }) => {
    await assert.rejects(connectSource(expected));
    assert.deepEqual(calls, []);
  }, (state) => { connected(state); state.runtime[field].system = "different-system"; });
});

test("Connect can prepare an accepted source on a foundation that has only applied system settings", async () => {
  await fixture(async ({ state, calls }) => {
    const connect = api.acqConnect;
    api.acqConnect = async () => {
      state.runtime.applied.acquisition = expected.acquisition;
      await connect();
    };
    const result = await connectSource(expected);
    assert.equal(result.acquisition.connected, true);
    assert.deepEqual(calls, ["connect"]);
  }, (state) => {
    state.runtime.running = true;
    state.runtime.generation = "engine-1";
    state.runtime.applied = { system: expected.system };
  });
});
