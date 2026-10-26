/** Preparation and quick start reuse pinned selections without saving or applying edits. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { prepareSource, startAcquisition } = await server.ssrLoadModule("/src/source-lifecycle.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const revision = "saved-startup-revision";

async function fixture(action, configure = () => {}) {
  const original = [{ ...api }, { ...pluginConfigApi }, { ...runtimeConfigApi }];
  const calls = [], stages = [];
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
  try { await action({ state, calls, stages, report: (stage) => stages.push(stage) }); }
  finally { Object.assign(api, original[0]); Object.assign(pluginConfigApi, original[1]); Object.assign(runtimeConfigApi, original[2]); }
}

test("first start starts the engine, connects, then asks the backend to validate and read", async () => {
  await fixture(async ({ calls, stages, report }) => {
    const result = await startAcquisition(revision, report);
    assert.deepEqual(calls, ["engine", "connect", "read"]);
    assert.deepEqual(stages, ["checking", "starting_engine", "connecting", "validating"]);
    assert.equal(result.acquisition.streaming, true);
  });
});

test("setup preparation stops before reading and returns the confirmed connection identity", async () => {
  await fixture(async ({ calls, report }) => {
    const result = await prepareSource(revision, report);
    assert.deepEqual(calls, ["engine", "connect"]);
    assert.equal(result.acquisition.connection_id, "connection-1");
    assert.equal(result.runtime.generation, "engine-1");
    assert.equal(result.acquisition.streaming, false);
  });
});

for (const connected of [false, true]) {
  test(`repeat start reuses the running engine and ${connected ? "existing connection" : "connects the source"}`, async () => {
    await fixture(async ({ calls }) => {
      await startAcquisition(revision);
      assert.deepEqual(calls, connected ? ["read"] : ["connect", "read"]);
    }, (state) => {
      Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" });
      Object.assign(state.acquisition, { connected, connection_id: connected ? "connection-1" : null });
    });
  });
}

test("a source already reading is not restarted", async () => {
  await fixture(async ({ calls }) => {
    await startAcquisition(revision); assert.deepEqual(calls, []);
  }, (state) => {
    Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" });
    Object.assign(state.acquisition, { connected: true, streaming: true, connection_id: "connection-1" });
  });
});

for (const [name, configure] of [
  ["missing source", (state) => { state.selection.bindings = []; }],
  ["changed selection", (state) => { state.runtime.accepted_revision = "changed"; }],
  ["changed activation", (state) => { state.selection.revision = "changed"; }],
  ["recovery required", (state) => { state.runtime.recovery_required = true; }],
  ["configuration operation", (state) => { state.runtime.operation = { status: "queued" }; }],
  ["different applied configuration", (state) => { Object.assign(state.runtime, { running: true, applied_revision: "other", generation: "engine-1" }); }],
  ["missing connection identity", (state) => {
    Object.assign(state.runtime, { running: true, applied_revision: revision, generation: "engine-1" });
    state.acquisition.connected = true;
  }],
]) {
  test(`${name} blocks startup without changing configuration or runtime`, async () => {
    await fixture(async ({ calls }) => {
      await assert.rejects(startAcquisition(revision)); assert.deepEqual(calls, []);
    }, configure);
  });
}

test("a changed revision after engine startup stops before connecting", async () => {
  await fixture(async ({ state, calls }) => {
    const start = api.engineStart;
    api.engineStart = async () => { await start(); state.runtime.accepted_revision = "changed"; };
    await assert.rejects(startAcquisition(revision), /configuration changed/i);
    assert.deepEqual(calls, ["engine"]);
  });
});

test("an engine restart after connection stops before reading", async () => {
  await fixture(async ({ state, calls }) => {
    const connect = api.acqConnect;
    api.acqConnect = async () => { await connect(); state.runtime.generation = "engine-2"; };
    await assert.rejects(startAcquisition(revision), /engine changed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a replaced connection before reading is rejected", async () => {
  await fixture(async ({ state, calls }) => {
    const status = api.acquisitionStatus;
    let connectedReads = 0;
    api.acquisitionStatus = async () => {
      if (state.acquisition.connected && ++connectedReads === 2) state.acquisition.connection_id = "replacement";
      return status();
    };
    await assert.rejects(startAcquisition(revision), /connection changed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

for (const stage of ["engine", "connect", "read"]) {
  test(`${stage} failure does not retry a write or undo completed steps`, async () => {
    await fixture(async ({ calls }) => {
      const method = { engine: "engineStart", connect: "acqConnect", read: "acqStreamStart" }[stage];
      api[method] = async () => { calls.push(stage); throw new Error(`${stage} failed`); };
      await assert.rejects(startAcquisition(revision), new RegExp(`${stage} failed`));
      assert.deepEqual(calls, ["engine", "connect", "read"].slice(0, ["engine", "connect", "read"].indexOf(stage) + 1));
    });
  });
}

test("successful connection responses without confirmed device state are not reported as success", async () => {
  await fixture(async ({ calls }) => {
    api.acqConnect = async () => { calls.push("connect"); };
    await assert.rejects(startAcquisition(revision), /connection was not confirmed/i);
    assert.deepEqual(calls, ["engine", "connect"]);
  });
});

test("a short recording may finish after acknowledged startup without becoming a startup failure", async () => {
  await fixture(async ({ calls, state }) => {
    api.acqStreamStart = async () => { calls.push("read"); state.acquisition.streaming = false; };
    const result = await startAcquisition(revision);
    assert.equal(result.acquisition.streaming, false);
    assert.deepEqual(calls, ["engine", "connect", "read"]);
  });
});

test("unavailable status aborts before starting the engine", async () => {
  await fixture(async ({ calls }) => {
    api.acquisitionStatus = async () => null;
    await assert.rejects(startAcquisition(revision), /status is unavailable/i);
    assert.deepEqual(calls, []);
  });
});
