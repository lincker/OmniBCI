/** Results filters apply only to the retained buffer and keep source values inspectable. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { TrialResults } = await server.ssrLoadModule("/src/TrialResults.tsx");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const { useResultsFeed } = await server.ssrLoadModule("/src/hooks/useResultsFeed.ts");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const desktop = await server.ssrLoadModule("/src/desktop.ts");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const trial = (trial_number, changes = {}) => ({ generation: "engine-1", trial_number,
  target_id: 2, event_id: 8, confidence: null, forced: false,
  produced_at: "2026-01-01T00:00:00Z", processing_revision: "revision-1", ...changes });

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function rows(tree) {
  return elements(elements(tree).find((item) => item.type === "tbody")).filter((item) => item.type === "tr");
}
function harness(props) {
  const hooks = [];
  let cursor = 0, tree;
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (value) => { hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value; }];
    },
  };
  return {
    render(next = {}) {
      Object.assign(props, next);
      const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
      const previous = current.current;
      current.current = dispatcher; cursor = 0;
      try { tree = TrialResults(props); } finally { current.current = previous; }
      return tree;
    },
    change(label, value) {
      const input = elements(tree).find((item) => item.props["aria-label"] === label);
      assert.ok(input, `Missing ${label}`); input.props.onChange({ target: { value } });
      return this.render();
    },
    clear() {
      const button = elements(tree).find((item) => item.type === "button" && text(item) === "Clear filters");
      assert.ok(button); button.props.onClick(); return this.render();
    },
  };
}

test("confidence preserves missing, zero and source precision instead of rounding", () => {
  const h = harness({ trials: [trial(1), trial(2, { confidence: 0 }), trial(3, { confidence: 0.00004 })] });
  const rendered = rows(h.render());
  assert.equal(rendered.length, 3);
  const confidence = (row) => elements(row).filter((item) => item.type === "td")[3];
  assert.equal(text(confidence(rendered[0])), "0.00004");
  assert.equal(text(confidence(rendered[1])), "0");
  assert.equal(text(confidence(rendered[2])), "—");
});

test("time is converted to UTC with the original timestamp available", () => {
  const original = "2026-01-01T08:30:01.123456+08:00";
  const h = harness({ trials: [trial(1, { produced_at: original })] });
  const tree = h.render();
  assert.match(text(tree), /2026-01-01 00:30:01\.123456/);
  assert.ok(elements(tree).some((item) => item.props.title === original));
  assert.doesNotMatch(text(h.render({ trials: [trial(1, { produced_at: "invalid" })] })), /Invalid Date/);
  assert.doesNotMatch(text(h.render({ trials: [trial(1, { produced_at: "2026-01-01T08:30:01" })] })), /2026-01-01/,
    "A timestamp without a timezone must not be presented as UTC");
});

test("scope explains the retained buffer, generation and newest-first ordering", () => {
  const h = harness({ trials: [trial(1), trial(2)], generation: "engine-1" });
  const tree = h.render();
  assert.match(text(tree), /latest 200/);
  assert.match(text(tree), /not a complete history/);
  assert.match(text(tree), /Generation: engine-1/);
  assert.match(text(tree), /Showing 2 of 2 retained results/);
  assert.deepEqual(rows(tree).map((row) => row.key), ["engine-1:2", "engine-1:1"]);
  const region = elements(tree).find((item) => item.props.role === "region");
  assert.equal(region.props.tabIndex, 0);
});

test("text and forced filters combine, count only visible rows and clear without changing the buffer", () => {
  const data = [trial(1), trial(2, { processing_revision: "revision-2", forced: true }), trial(3, { target_id: 4, forced: true })];
  const h = harness({ trials: data });
  h.render();
  assert.equal(rows(h.change("Search retained results", "revision-2")).length, 1);
  assert.equal(rows(h.change("Forced result filter", "unforced")).length, 0);
  assert.match(text(h.render()), /No retained results match these filters/);
  assert.doesNotMatch(text(h.render()), /No trial results yet/);
  const restored = h.clear();
  assert.equal(rows(restored).length, 3);
  assert.equal(data.length, 3);
  assert.equal(rows(h.change("Search retained results", "   ")).length, 3);
});

test("empty and failed refresh states do not imply historical completeness or fresh data", () => {
  const h = harness({ trials: [], generation: null });
  assert.match(text(h.render()), /No trial results yet/);
  assert.match(text(h.render()), /latest 200/);
  const tree = h.render({ trials: [trial(1)], generation: "engine-1", stale: true });
  assert.match(text(tree), /last successful/);
  assert.equal(rows(tree).length, 1);
});


test("results retain and search both acquisition and processing provenance", () => {
  const h = harness({ trials: [trial(1, { acquisition_revision: "source-A", processing_revision: "processing-A" }), trial(2, { acquisition_revision: "source-B" })] });
  const tree = h.render();
  assert.ok(elements(tree).some((item) => item.props.title === "source-A"));
  assert.ok(elements(tree).some((item) => item.props.title === "processing-A"));
  assert.equal(rows(h.change("Search retained results", "source-A")).length, 1);
});

const feedTrial = (trial_number, generation = "engine-1") => ({
  generation, trial_number, target_id: 2, event_id: 8, confidence: null,
  forced: false, produced_at: "2026-01-01T00:00:00Z", processing_revision: "revision-1",
});
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

function feedHarness() {
  const original = { window: globalThis.window, results: api.results,
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  let deliver, interval, reconnect, cleanup;
  globalThis.setInterval = (callback) => { interval = callback; return 1; };
  globalThis.clearInterval = () => {};
  globalThis.setTimeout = (callback) => { reconnect = callback; return 1; };
  globalThis.clearTimeout = () => {};
  globalThis.window = { omnibciDesktop: { subscribeResults(_id, listener) { deliver = listener; return () => {}; } } };
  desktop.setDesktopConnection({ id: "server", kind: "owned", status: "ready" });
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
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: [feedTrial(1)] });
  try {
    h.open(); await flush();
    h.send({ event: "TrialCompleted", ...feedTrial(1) });
    h.send({ event: "TrialCompleted", ...feedTrial(2) });
    assert.equal(h.render().generation, "engine-1");
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
    assert.equal(h.render().error, null);
    assert.equal("session" in h.render(), false);
  } finally { h.close(); }
});

test("an in-flight snapshot preserves newer WebSocket increments", async () => {
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: [feedTrial(1)] });
  try {
    h.open(); await flush();
    const pending = deferred();
    api.results = () => pending.promise;
    h.poll();
    h.send({ event: "TrialCompleted", ...feedTrial(2) });
    pending.resolve({ generation: "engine-1", trials: [feedTrial(1)] });
    await flush();
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
  } finally { h.close(); }
});

test("engine restart clears old results even before the first new trial", async () => {
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: [feedTrial(1)] });
  try {
    h.open(); await flush();
    api.results = async () => ({ generation: "engine-2", trials: [] });
    h.poll(); await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials, []);
    h.send({ event: "TrialCompleted", ...feedTrial(2, "engine-1") });
    await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials, []);
    h.send({ event: "TrialCompleted", ...feedTrial(1, "engine-2") });
    assert.equal(h.render().trials[0].generation, "engine-2");
  } finally { h.close(); }
});

test("new generation messages reconcile from the authoritative snapshot", async () => {
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: [] });
  try {
    h.open(); await flush();
    api.results = async () => ({ generation: "engine-2", trials: [feedTrial(1, "engine-2")] });
    h.send({ event: "TrialCompleted", ...feedTrial(1, "engine-2") });
    await flush();
    assert.deepEqual(h.render().trials, [feedTrial(1, "engine-2")]);
  } finally { h.close(); }
});

test("reconnect backfills missed trials and rejects older snapshot responses", async () => {
  const h = feedHarness();
  const old = deferred();
  api.results = () => old.promise;
  try {
    h.open();
    api.results = async () => ({ generation: "engine-2", trials: [feedTrial(1, "engine-2"), feedTrial(2, "engine-2")] });
    h.reconnect(); await flush();
    old.resolve({ generation: "engine-1", trials: [feedTrial(1)] });
    await flush();
    assert.equal(h.render().generation, "engine-2");
    assert.deepEqual(h.render().trials.map((item) => item.trial_number), [1, 2]);
  } finally { h.close(); }
});

test("failed snapshots preserve visible trials and expose the failure", async () => {
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: [feedTrial(1)] });
  try {
    h.open(); await flush();
    api.results = async () => { throw new Error("Results are unavailable"); };
    h.poll(); await flush();
    assert.equal(h.render().trials.length, 1);
    assert.equal(h.render().error, "Results are unavailable");
  } finally { h.close(); }
});

test("the live view retains only the latest 200 trials", async () => {
  const h = feedHarness();
  api.results = async () => ({ generation: "engine-1", trials: Array.from({ length: 200 }, (_, index) => feedTrial(index + 1)) });
  try {
    h.open(); await flush();
    h.send({ event: "TrialCompleted", ...feedTrial(201) });
    assert.equal(h.render().trials.length, 200);
    assert.equal(h.render().trials[0].trial_number, 2);
    assert.equal(h.render().trials.at(-1).trial_number, 201);
  } finally { h.close(); }
});
