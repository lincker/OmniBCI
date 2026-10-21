/** Live results do not depend on session creation, and snapshots cannot lose increments. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { useResultsFeed } = await server.ssrLoadModule("/src/hooks/useResultsFeed.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const desktop = await server.ssrLoadModule("/src/desktop.ts");
const trial = (trial_number, generation = "engine-1") => ({
  generation, trial_number, target_id: 2, event_id: 8, confidence: null,
  forced: false, produced_at: "2026-01-01T00:00:00Z", config_revision: "revision-1",
});
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

function harness() {
  const original = { window: globalThis.window, results: api.results,
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  let deliver, interval, reconnect, cleanup;
  globalThis.setInterval = (callback) => { interval = callback; return 1; };
  globalThis.clearInterval = () => {};
  globalThis.setTimeout = (callback) => { reconnect = callback; return 1; };
  globalThis.clearTimeout = () => {};
  globalThis.window = { omnibciDesktop: { subscribeResults(_id, listener) { deliver = listener; return () => {}; } } };
  desktop.setDesktopConnection({ id: "backend", kind: "local", status: "ready" });
  const values = [], effects = [];
  let cursor = 0;
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  function render() {
    cursor = 0;
    const previous = dispatcher.current;
    dispatcher.current = {
      useState(initial) {
        const index = cursor++;
        if (!(index in values)) values[index] = typeof initial === "function" ? initial() : initial;
        return [values[index], (value) => { values[index] = typeof value === "function" ? value(values[index]) : value; }];
      },
      useEffect(effect) { if (!cleanup) effects.push(effect); },
    };
    try { return useResultsFeed(); }
    finally { dispatcher.current = previous; }
  }
  render();
  cleanup = effects[0]();
  return {
    render,
    open: () => deliver({ type: "open" }),
    send: (value) => deliver({ type: "message", data: JSON.stringify(value) }),
    poll: () => interval(),
    reconnect() { deliver({ type: "closed" }); reconnect(); deliver({ type: "open" }); },
    close() {
      cleanup();
      Object.assign(globalThis, { window: original.window, setInterval: original.setInterval,
        clearInterval: original.clearInterval, setTimeout: original.setTimeout, clearTimeout: original.clearTimeout });
      api.results = original.results;
    },
  };
}

test("snapshot and live increments deduplicate without a session", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: [trial(1)] });
  try {
    h.open(); await flush();
    h.send({ event: "TrialCompleted", ...trial(1) });
    h.send({ event: "TrialCompleted", ...trial(2) });
    assert.equal(h.render().generation, "engine-1");
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
    assert.equal(h.render().error, null);
    assert.equal("session" in h.render(), false);
  } finally { h.close(); }
});

test("an in-flight snapshot preserves newer WebSocket increments", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: [trial(1)] });
  try {
    h.open(); await flush();
    const pending = deferred();
    api.results = () => pending.promise;
    h.poll();
    h.send({ event: "TrialCompleted", ...trial(2) });
    pending.resolve({ generation: "engine-1", trials: [trial(1)] });
    await flush();
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
  } finally { h.close(); }
});

test("engine restart clears old results even before the first new trial", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: [trial(1)] });
  try {
    h.open(); await flush();
    api.results = async () => ({ generation: "engine-2", trials: [] });
    h.poll(); await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials, []);
    h.send({ event: "TrialCompleted", ...trial(2, "engine-1") });
    await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials, []);
    h.send({ event: "TrialCompleted", ...trial(1, "engine-2") });
    assert.equal(h.render().trials[0].generation, "engine-2");
  } finally { h.close(); }
});

test("new generation messages reconcile from the authoritative snapshot", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: [] });
  try {
    h.open(); await flush();
    api.results = async () => ({ generation: "engine-2", trials: [trial(1, "engine-2")] });
    h.send({ event: "TrialCompleted", ...trial(1, "engine-2") });
    await flush();
    assert.deepEqual(h.render().trials, [trial(1, "engine-2")]);
  } finally { h.close(); }
});

test("reconnect backfills missed trials and rejects older snapshot responses", async () => {
  const h = harness();
  const old = deferred();
  api.results = () => old.promise;
  try {
    h.open();
    api.results = async () => ({ generation: "engine-2", trials: [trial(1, "engine-2"), trial(2, "engine-2")] });
    h.reconnect(); await flush();
    old.resolve({ generation: "engine-1", trials: [trial(1)] });
    await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
  } finally { h.close(); }
});

test("failed snapshots preserve visible trials and expose the failure", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: [trial(1)] });
  try {
    h.open(); await flush();
    api.results = async () => { throw new Error("Results are unavailable"); };
    h.poll(); await flush();
    assert.equal(h.render().trials.length, 1);
    assert.equal(h.render().error, "Results are unavailable");
  } finally { h.close(); }
});

test("the live view retains only the latest 200 trials", async () => {
  const h = harness();
  api.results = async () => ({ generation: "engine-1", trials: Array.from({ length: 200 }, (_, index) => trial(index + 1)) });
  try {
    h.open(); await flush();
    h.send({ event: "TrialCompleted", ...trial(201) });
    assert.equal(h.render().trials.length, 200);
    assert.equal(h.render().trials[0].trial_number, 2);
    assert.equal(h.render().trials.at(-1).trial_number, 201);
  } finally { h.close(); }
});
