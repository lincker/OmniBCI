/** Closed epochs remain visible without predictions and recover through paged snapshots. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { EpochProcessing } = await server.ssrLoadModule("/src/EpochProcessing.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const epoch = (epoch_number, changes = {}) => ({
  generation: "engine-1", epoch_number, event_id: 8, config_revision: "revision-1",
  status: "no_result", reason: "next_start", sample_rate: 250, channel_count: 2,
  sample_count: 128, pre_trigger_samples: 25, started_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:00:01Z", trial_number: null, result: null, error: null,
  ...changes,
});
const snapshot = (epochs, changes = {}) => ({ generation: "engine-1", total: epochs.length,
  offset: 0, limit: 25, epochs, ...changes });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const sameDependencies = (left, right) => !!left && !!right && left.length === right.length
  && left.every((value, index) => Object.is(value, right[index]));

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function button(tree, label) {
  const value = elements(tree).find((item) => item.type === "button" && text(item) === label);
  assert.ok(value, `Missing button: ${label}`);
  return value;
}
function rows(tree) {
  const body = elements(tree).find((item) => item.type === "tbody");
  return elements(body).filter((item) => item.type === "tr");
}

function harness(props = {}) {
  const original = { epochs: api.epochs, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const timers = new Map();
  let timerId = 0;
  globalThis.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  const hooks = [];
  let cursor = 0, dirty = true, tree, pendingEffects = [];
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      hooks[index].set ??= (next) => {
        const value = typeof next === "function" ? next(hooks[index].value) : next;
        if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; dirty = true; }
      };
      return [hooks[index].value, hooks[index].set];
    },
    useRef(initial) { return hooks[cursor++] ??= { current: initial }; },
    useEffect(effect, deps) {
      const index = cursor++;
      const previous = hooks[index];
      if (!previous || !sameDependencies(previous.deps, deps)) {
        hooks[index] = { deps, cleanup: previous?.cleanup };
        pendingEffects.push(() => { hooks[index].cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  return {
    get tree() { return tree; },
    get timerCount() { return timers.size; },
    poll() {
      assert.equal(timers.size, 1, "Only one refresh can be scheduled");
      const [id, timer] = [...timers.entries()][0];
      assert.equal(timer.delay, 1000);
      timers.delete(id); timer.callback();
    },
    async settle() {
      for (let round = 0; round < 32; round += 1) {
        if (dirty) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          current.current = dispatcher; cursor = 0; dirty = false;
          try { tree = EpochProcessing(props); } finally { current.current = previous; }
          const effects = pendingEffects; pendingEffects = [];
          effects.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(dirty, false, "Epoch processing did not settle");
    },
    unmount() { hooks.forEach((hook) => hook?.cleanup?.()); },
    dispose() {
      this.unmount();
      api.epochs = original.epochs;
      globalThis.setTimeout = original.setTimeout;
      globalThis.clearTimeout = original.clearTimeout;
    },
  };
}

test("the epoch list includes decoded, no-result, interrupted and error records without data controls", async () => {
  const h = harness();
  api.epochs = async () => snapshot([
    epoch(4, { status: "error", reason: "error", error: "Decoder failed" }),
    epoch(3, { status: "interrupted", reason: "processor_stop" }),
    epoch(2),
    epoch(1, { status: "decoded", reason: "prediction", trial_number: 1,
      result: { target_id: 3, confidence: 0.85, forced: false } }),
  ]);
  try {
    await h.settle();
    assert.equal(rows(h.tree).length, 4);
    assert.deepEqual(rows(h.tree).map((row) => text(elements(row).find((item) => item.type === "td"))), ["4", "3", "2", "1"]);
    for (const label of ["Epoch processing", "No result", "Interrupted", "Error", "Decoded", "Target 3", "0.85", "Decoder failed"]) {
      assert.ok(text(h.tree).includes(label), `Missing ${label}`);
    }
    assert.doesNotMatch(text(h.tree), /Detailed data|Recent results/);
    assert.equal(elements(h.tree).filter((item) => item.type === "button").length, 0);
    assert.equal(translate("zh", "epoch.title"), "epoch处理");
  } finally { h.dispose(); }
});

test("history beyond the first page is reachable and the list does not truncate to three results", async () => {
  const h = harness();
  const calls = [];
  api.epochs = async (offset, limit) => {
    calls.push([offset, limit]);
    return snapshot(offset === 0 ? Array.from({ length: 25 }, (_, index) => epoch(30 - index))
      : Array.from({ length: 5 }, (_, index) => epoch(5 - index)), { offset, total: 30 });
  };
  try {
    await h.settle();
    assert.equal(rows(h.tree).length, 25);
    assert.equal(button(h.tree, "Previous page").props.disabled, true);
    button(h.tree, "Next page").props.onClick(); await h.settle();
    assert.equal(rows(h.tree).length, 5);
    assert.match(text(h.tree), /Page 2 of 2/);
    assert.equal(button(h.tree, "Next page").props.disabled, true);
    button(h.tree, "Previous page").props.onClick(); await h.settle();
    assert.equal(rows(h.tree).length, 25);
    assert.deepEqual(calls, [[0, 25], [25, 25], [0, 25]]);
  } finally { h.dispose(); }
});

test("a generation change clears old rows and returns to the first page", async () => {
  const h = harness();
  const calls = [];
  let generation = "engine-1";
  api.epochs = async (offset) => {
    calls.push([generation, offset]);
    return generation === "engine-1" ? snapshot([epoch(offset ? 1 : 30)], { offset, total: 30 })
      : snapshot(offset ? [] : [epoch(1, { generation, event_id: 42 })], { generation, offset, total: 1 });
  };
  try {
    await h.settle();
    button(h.tree, "Next page").props.onClick(); await h.settle();
    generation = "engine-2";
    h.poll(); await h.settle();
    assert.equal(rows(h.tree).length, 1);
    assert.match(text(rows(h.tree)[0]), /42/);
    assert.equal(rows(h.tree)[0].key, "engine-2:1");
    assert.deepEqual(calls.slice(-2), [["engine-2", 25], ["engine-2", 0]]);
    assert.doesNotMatch(text(h.tree), /Page 2/);
  } finally { h.dispose(); }
});

test("failed refresh keeps the last successful page visible and Retry recovers", async () => {
  const h = harness();
  api.epochs = async () => snapshot([epoch(1)]);
  try {
    await h.settle();
    api.epochs = async () => { throw new Error("Epoch history unavailable"); };
    h.poll(); await h.settle();
    assert.equal(rows(h.tree).length, 1);
    assert.match(text(h.tree), /Epoch history unavailable/);
    api.epochs = async () => snapshot([epoch(2), epoch(1)]);
    button(h.tree, "Retry").props.onClick(); await h.settle();
    assert.equal(rows(h.tree).length, 2);
    assert.doesNotMatch(text(h.tree), /Epoch history unavailable/);
    assert.equal(h.timerCount, 1);
  } finally { h.dispose(); }
});

test("an older page response cannot overwrite a later navigation", async () => {
  const h = harness();
  api.epochs = async (offset) => snapshot([epoch(offset ? 1 : 30)], { offset, total: 30 });
  try {
    await h.settle();
    const pending = deferred();
    api.epochs = (offset) => offset === 0 ? pending.promise : Promise.resolve(snapshot([epoch(5)], { offset, total: 30 }));
    h.poll(); await h.settle();
    button(h.tree, "Next page").props.onClick(); await h.settle();
    pending.resolve(snapshot([epoch(99)], { total: 99 })); await h.settle();
    assert.equal(rows(h.tree)[0].key, "engine-1:5");
    assert.match(text(h.tree), /Page 2 of 2/);
    assert.equal(h.timerCount, 1);
  } finally { h.dispose(); }
});

test("unmount cancels polling and ignores an in-flight response", async () => {
  const h = harness();
  api.epochs = async () => snapshot([epoch(1)]);
  try {
    await h.settle();
    h.unmount();
    assert.equal(h.timerCount, 0);
  } finally { h.dispose(); }
  const pendingHarness = harness();
  const pending = deferred();
  api.epochs = () => pending.promise;
  try {
    await pendingHarness.settle(); pendingHarness.unmount();
    pending.resolve(snapshot([epoch(1)])); await pendingHarness.settle();
    assert.equal(pendingHarness.timerCount, 0);
    assert.equal(rows(pendingHarness.tree).length, 0);
  } finally { pendingHarness.dispose(); }
});

test("an empty generation provides an honest empty state", async () => {
  const h = harness();
  api.epochs = async () => snapshot([], { generation: null });
  try {
    await h.settle();
    assert.match(text(h.tree), /No closed epochs yet/);
    assert.equal(rows(h.tree).length, 0);
    assert.equal(elements(h.tree).filter((item) => item.type === "button").length, 0);
  } finally { h.dispose(); }
});

test("decoded-results navigation preserves the run console's busy guard", async () => {
  for (const disabled of [true, false]) {
    const h = harness({ disabled, onResults: () => {} });
    api.epochs = async () => snapshot([]);
    try {
      await h.settle();
      assert.equal(button(h.tree, "Decoded results").props.disabled, disabled);
    } finally { h.dispose(); }
  }
});
