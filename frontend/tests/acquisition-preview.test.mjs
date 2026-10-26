/** Live previews preserve the selected snapshot without reusing another capture's response. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { AcquisitionPreview, PreviewChunkTable, PreviewChunkDetail, PreviewChunks, parsePreviewSelection, previewValueKey } = await server.ssrLoadModule("/src/AcquisitionPreview.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };

const limits = { max_chunks: 32, max_rows: 16, max_samples: 512, max_values: 8192,
  max_frame_bytes: 131072, max_cache_bytes: 4194304, preview_queue_size: 8, capture_timeout_seconds: 10 };
const form = (changes = {}) => ({ rows: "0", sampleStart: "0", sampleCount: "128", chunkCount: "32", includeTrigger: true, ...changes });
const chunk = (changes = {}) => ({
  capture_id: "capture-original", generation: "generation-original", connection_id: "connection-original",
  config_revision: "a".repeat(64), source_id: "source-original", sequence_id: 41,
  timestamp_ns: "1780000000000000001", original_shape: [5, 12], dtype: "float64",
  row_indices: [2, 4], sample_start: 5, sample_count: 3, trigger_row_index: 4,
  values: [[1.25, "NaN", "Infinity"], ["9007199254740993", "-Infinity", true]],
  descriptor: { source_id: "source-original", connection_id: "connection-original", sample_rate: 500,
    trigger_row_index: 4, native_event: "final-row", provenance: "device-reported",
    signal_channel_count: 4, output_row_count: 5,
    channels: ["C1", "C2", "C3", "C4"].map((name, index) => ({ channel_id: `channel-${index}`,
      index, name, signal_type: "eeg", unit: "uV" })) },
  ...changes,
});
const summary = (sequenceId, sampleCount = 3) => ({
  sequence_id: sequenceId, timestamp_ns: "1780000000000000001", original_shape: [5, 12],
  dtype: "float64", row_indices: [2, 4], sample_start: 5, sample_count: sampleCount,
  trigger_row_index: 4, encoded_bytes: 512,
});
const snapshot = (chunks, changes = {}) => ({
  generation: "generation-original", capture_id: "capture-original", chunks,
  config_revision: "a".repeat(64), source_id: "source-original", connection_id: "connection-original",
  status: "capturing", reason: null, continuous: true, available: true, cache_errors: 0,
  dropped: 0, oversized: 0, stream_chunk_count: chunks.length, stream_sample_count: chunks.length * 3,
  last_sequence: chunks.at(-1)?.sequence_id ?? -1, limits,
  selection: { rows: [2], sample_start: 5, sample_count: 3, chunk_count: 32, include_trigger: true },
  observed: chunks.length, enqueued: chunks.length, received: chunks.length, ...changes,
});

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function chunkButtons(tree) {
  return elements(tree).filter((item) => item.type === "button" && typeof item.props["aria-pressed"] === "boolean");
}
function selectedDetail(tree) {
  return elements(tree).find((item) => item.type === PreviewChunkDetail);
}
function table(value) {
  const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = current.current;
  current.current = {
    useContext: () => english,
    useState: (initial) => [typeof initial === "function" ? initial() : initial, () => {}],
    useRef: (initial) => ({ current: initial }),
    useEffect: () => {},
  };
  try { return PreviewChunkTable({ chunk: value }); }
  finally { current.current = previous; }
}

test("selection preserves the explicit row order and zero-based sample offset", () => {
  assert.deepEqual(parsePreviewSelection(form({ rows: "2, 0", sampleStart: "5", sampleCount: "3", chunkCount: "7" }), limits), {
    rows: [2, 0], sample_start: 5, sample_count: 3, chunk_count: 7, include_trigger: true,
  });
});

test("trigger-only and maximum valid selections remain available", () => {
  assert.deepEqual(parsePreviewSelection(form({ rows: "", sampleCount: "512" }), limits).rows, []);
  assert.equal(parsePreviewSelection(form({ rows: Array.from({ length: 16 }, (_, i) => i).join(","),
    sampleCount: "512", includeTrigger: false }), limits).sample_count, 512);
});

for (const [label, changes] of [
  ["duplicate rows", { rows: "0,0" }], ["negative row", { rows: "-1" }],
  ["fractional row", { rows: "1.5" }], ["unsafe row", { rows: "9007199254740992" }],
  ["empty row between commas", { rows: "0,,1" }], ["trailing comma", { rows: "0," }],
  ["hexadecimal row", { rows: "0x10" }], ["exponential row", { rows: "1e2" }],
  ["no rows or trigger", { rows: "", includeTrigger: false }],
  ["too many rows", { rows: Array.from({ length: 17 }, (_, i) => i).join(",") }],
  ["negative sample start", { sampleStart: "-1" }], ["empty sample start", { sampleStart: "" }],
  ["unsafe sample start", { sampleStart: "9007199254740992" }],
  ["fractional samples", { sampleCount: "1.5" }], ["zero samples", { sampleCount: "0" }],
  ["too many samples", { sampleCount: "513" }], ["nonfinite samples", { sampleCount: "Infinity" }],
  ["zero chunks", { chunkCount: "0" }], ["too many chunks", { chunkCount: "33" }],
  ["trigger exceeds the value budget", { rows: Array.from({ length: 16 }, (_, i) => i).join(","), sampleCount: "512" }],
]) {
  test(`selection rejects ${label}`, () => assert.throws(() => parsePreviewSelection(form(changes), limits)));
}

test("selection uses server-provided limits including the trigger row", () => {
  const small = { ...limits, max_chunks: 2, max_rows: 2, max_samples: 8, max_values: 8 };
  assert.throws(() => parsePreviewSelection(form({ sampleCount: "4", chunkCount: "3" }), small));
  assert.throws(() => parsePreviewSelection(form({ rows: "0,1", sampleCount: "4", chunkCount: "2" }), small));
  assert.equal(parsePreviewSelection(form({ sampleCount: "4", chunkCount: "2" }), small).sample_count, 4);
});

test("the table labels the actual source rows and samples and preserves exact values", () => {
  const value = chunk();
  const before = structuredClone(value);
  const tree = table(value);
  const head = elements(tree).find((item) => item.type === "thead");
  const headings = elements(head).filter((item) => item.type === "th").map(text);
  assert.equal(headings.length, 4);
  for (const [index, sample] of [5, 6, 7].entries()) assert.match(headings[index + 1], new RegExp(`\\b${sample}\\b`));
  const body = elements(tree).find((item) => item.type === "tbody");
  const rows = elements(body).filter((item) => item.type === "tr");
  assert.equal(rows.length, 2);
  const rowLabels = rows.map((row) => text(elements(row).find((item) => item.type === "th")));
  assert.match(rowLabels[0], /\b2\b/);
  assert.match(rowLabels[0], /C3/);
  assert.match(rowLabels[1], /\b4\b/);
  assert.match(rowLabels[1], /trigger/i);
  assert.deepEqual(rows.map((row) => elements(row).filter((item) => item.type === "td").map(text)), [
    ["1.25", "NaN", "Infinity"], ["9007199254740993", "-Infinity", "true"],
  ]);
  assert.deepEqual(value, before);
});

test("chunk context identifies capture-time provenance, shape, dtype and exact timestamp", () => {
  const tree = table(chunk());
  const rendered = text(tree);
  for (const expected of ["source-original", "connection-original", "generation-original", "a".repeat(64),
    "1780000000000000001", "float64"]) {
    assert.ok(rendered.includes(expected), `Missing captured context: ${expected}`);
  }
  assert.match(rendered, /device[- ]reported/i);
  const shape = elements(tree).find((item) => item.type === "div"
    && item.props.children?.[0]?.type === "dt" && text(item.props.children[0]) === "Original shape / dtype");
  assert.equal(text(elements(shape).find((item) => item.type === "dd")), "5 × 12 / float64");
});

test("missing descriptor never invents source channel labels", () => {
  const tree = table(chunk({ descriptor: null }));
  assert.doesNotMatch(text(tree), /C3|device[- ]reported/i);
  assert.match(text(tree), /trigger/i);
});

test("matrix scope reports the selected rows and chunk-relative sample range without implying a timeline", () => {
  const rendered = text(table(chunk()));
  assert.match(rendered, /Rows 2, 4.*samples 5–7/);
  assert.match(rendered, /Sample indices restart within each chunk/);
  assert.match(rendered, /bounded sample preview/);
  assert.match(text(table(chunk({ sample_count: 0, values: [[], []] }))), /no samples/);
});

test("sample text is inert and an empty retained selection creates no synthetic cells", () => {
  const html = renderToStaticMarkup(table(chunk({ values: [["<script>sample</script>", "NaN", 2], [0, 0, 1]] })));
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;sample&lt;\/script&gt;/);
  const empty = table(chunk({ sample_count: 0, sample_start: 99, values: [[], []] }));
  const body = elements(empty).find((item) => item.type === "tbody");
  assert.equal(elements(body).filter((item) => item.type === "td").length, 0);
});

const sameDependencies = (left, right) => !!left && !!right && left.length === right.length
  && left.every((value, index) => Object.is(value, right[index]));

function previewHarness(Component, initialProps) {
  const hooks = [];
  let cursor = 0, dirty = true, tree, pendingEffects = [];
  const props = { ...initialProps };
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
    useCallback(callback, deps) {
      const index = cursor++;
      if (!hooks[index] || !sameDependencies(hooks[index].deps, deps)) hooks[index] = { value: callback, deps };
      return hooks[index].value;
    },
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
    get shown() { return elements(tree).find((item) => item.type === PreviewChunkTable)?.props.chunk; },
    update(next) { Object.assign(props, next); dirty = true; },
    async settle() {
      for (let round = 0; round < 64; round += 1) {
        if (dirty) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          current.current = dispatcher; cursor = 0; dirty = false;
          try { tree = Component(props); } finally { current.current = previous; }
          const effects = pendingEffects; pendingEffects = [];
          effects.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(dirty, false, "Preview component did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

function component(tree, name) {
  return elements(tree).find((item) => typeof item.type === "function" && item.type.name === name);
}

async function panelHarness() {
  const props = { generation: "generation-original", revision: "a".repeat(64),
    connectionId: "connection-original", streaming: true, disabled: false };
  const preview = previewHarness(AcquisitionPreview, props);
  await preview.settle();
  const element = component(preview.tree, "CapturePanel");
  const panel = previewHarness(element.type, element.props);
  await panel.settle();
  return { preview, panel };
}

function polling(responses) {
  const original = { request: api.acqPreview, document: globalThis.document,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const requests = [], timers = [];
  globalThis.document = { hidden: false };
  globalThis.setTimeout = (callback, delay) => {
    const timer = { callback, delay, cancelled: false };
    timers.push(timer);
    return timer;
  };
  globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
  api.acqPreview = (...args) => {
    requests.push(args);
    assert.ok(responses.length, "Unexpected preview polling request");
    const value = responses.shift();
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  };
  return {
    requests,
    async next() {
      const timer = timers.find((item) => !item.cancelled);
      assert.ok(timer, "No preview poll was scheduled");
      timer.cancelled = true;
      assert.equal(timer.delay, 250);
      await timer.callback();
    },
    restore() {
      api.acqPreview = original.request;
      if (original.document === undefined) delete globalThis.document;
      else globalThis.document = original.document;
      globalThis.setTimeout = original.setTimeout;
      globalThis.clearTimeout = original.clearTimeout;
    },
  };
}

test("live preview opens automatically and polling does not overwrite edited matrix selection", async () => {
  const original = api.acqPreviewStart;
  const requests = [];
  api.acqPreviewStart = async (body) => {
    requests.push(body);
    return snapshot([], { capture_id: "capture-selection" });
  };
  const { preview, panel } = await panelHarness();
  try {
    assert.equal(preview.tree.props.open, true);
    let status = component(panel.tree, "PreviewStatus");
    status.props.onSnapshot(snapshot([summary(41)])); await panel.settle();
    const selection = elements(panel.tree).find((item) => item.type === "details");
    selection.props.onToggle({ currentTarget: { open: true } }); await panel.settle();
    const rowsInput = () => elements(panel.tree).find((item) => item.type === "input");
    assert.equal(rowsInput().props.value, "2");
    rowsInput().props.onChange({ target: { value: "1, 0" } }); await panel.settle();
    status.props.onSnapshot(snapshot([summary(41), summary(42)])); await panel.settle();
    assert.equal(rowsInput().props.value, "1, 0");
    assert.equal(component(panel.tree, "PreviewStatus").key, status.key);
    assert.equal(requests.length, 0, "Opening or polling the live view must not start another capture");

    const apply = elements(panel.tree).find((item) => item.type === "button" && text(item) === "Apply selection");
    assert.equal(apply.props.disabled, false);
    apply.props.onClick(); await panel.settle();
    assert.deepEqual(requests, [{ generation: "generation-original", expected_revision: "a".repeat(64),
      connection_id: "connection-original", continuous: true,
      selection: { rows: [1, 0], sample_start: 5, sample_count: 3, chunk_count: 32, include_trigger: true } }]);
    assert.notEqual(component(panel.tree, "PreviewStatus").key, status.key);

    const priorPanel = component(preview.tree, "CapturePanel");
    preview.update({ generation: "generation-next" }); await preview.settle();
    assert.notEqual(component(preview.tree, "CapturePanel").key, priorPanel.key);
  } finally { panel.dispose(); preview.dispose(); api.acqPreviewStart = original; }
});

test("latest-capture polling preserves selection for new snapshots and resets it for a new reading", async () => {
  const { preview, panel } = await panelHarness();
  const polls = polling([snapshot([summary(41)]), snapshot([summary(41), summary(42)]),
    snapshot([summary(41)], { capture_id: "capture-next" })]);
  const status = component(panel.tree, "PreviewStatus");
  const h = previewHarness(status.type, status.props);
  let chunks;
  try {
    await h.settle();
    const first = component(h.tree, "PreviewChunks");
    chunks = previewHarness(first.type, first.props); await chunks.settle();
    chunkButtons(chunks.tree)[0].props.onClick(); await chunks.settle();
    const selected = selectedDetail(chunks.tree);

    await polls.next(); await h.settle();
    const appended = component(h.tree, "PreviewChunks");
    assert.equal(appended.key, first.key);
    chunks.update(appended.props); await chunks.settle();
    assert.equal(selectedDetail(chunks.tree).key, selected.key);
    assert.deepEqual(chunkButtons(chunks.tree).map((item) => item.props["aria-pressed"]), [true, false]);

    await polls.next(); await h.settle();
    const restarted = component(h.tree, "PreviewChunks");
    assert.notEqual(restarted.key, first.key);
    chunks.dispose(); chunks = previewHarness(restarted.type, restarted.props); await chunks.settle();
    assert.equal(selectedDetail(chunks.tree), undefined);
    assert.deepEqual(chunkButtons(chunks.tree).map((item) => item.props["aria-pressed"]), [false]);
    assert.deepEqual(polls.requests, Array.from({ length: 3 }, () => ["generation-original"]));
  } finally { chunks?.dispose(); h.dispose(); panel.dispose(); preview.dispose(); polls.restore(); }
});

test("an empty preview does not present the no-sequence sentinel as observed data", async () => {
  const { preview, panel } = await panelHarness();
  const polls = polling([snapshot([])]);
  const status = component(panel.tree, "PreviewStatus");
  const h = previewHarness(status.type, status.props);
  try {
    await h.settle();
    assert.match(text(h.tree), /No chunks observed yet/);
    assert.doesNotMatch(text(h.tree), /Last sequence: -1/);
  } finally { h.dispose(); panel.dispose(); preview.dispose(); polls.restore(); }
});

test("a temporary status polling failure does not unmount the selected live-window detail", async () => {
  const { preview, panel } = await panelHarness();
  const polls = polling([snapshot([summary(41)]), new Error("Temporary preview status failure"),
    snapshot([summary(41), summary(42)])]);
  const status = component(panel.tree, "PreviewStatus");
  const h = previewHarness(status.type, status.props);
  let chunks;
  try {
    await h.settle();
    const first = component(h.tree, "PreviewChunks");
    chunks = previewHarness(first.type, first.props); await chunks.settle();
    chunkButtons(chunks.tree)[0].props.onClick(); await chunks.settle();
    const selected = selectedDetail(chunks.tree);
    await polls.next(); await h.settle();
    const failed = component(h.tree, "PreviewChunks");
    assert.ok(failed, "The retained window must stay mounted during a temporary polling failure");
    assert.equal(failed.key, first.key);
    assert.equal(failed.props.snapshot, first.props.snapshot);
    assert.match(text(h.tree), /Temporary preview status failure/);
    chunks.update(failed.props); await chunks.settle();
    assert.equal(selectedDetail(chunks.tree).key, selected.key);
    await panel.settle();
    const apply = () => elements(panel.tree).find((item) => item.type === "button" && text(item) === "Apply selection");
    assert.equal(apply().props.disabled, true);
    await polls.next(); await h.settle();
    const recovered = component(h.tree, "PreviewChunks");
    assert.equal(recovered.key, first.key);
    chunks.update(recovered.props); await chunks.settle();
    assert.equal(selectedDetail(chunks.tree).key, selected.key);
    await panel.settle();
    assert.equal(apply().props.disabled, false);
  } finally { chunks?.dispose(); h.dispose(); panel.dispose(); preview.dispose(); polls.restore(); }
});

test("retained chunk points expose their sequence and samples and select the matching detail", async () => {
  const h = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(42, 2)]) });
  try {
    await h.settle();
    const buttons = chunkButtons(h.tree);
    assert.equal(buttons.length, 2);
    for (const [index, [sequenceId, sampleCount]] of [[41, 3], [42, 2]].entries()) {
      for (const attribute of ["title", "aria-label"]) {
        assert.match(buttons[index].props[attribute], new RegExp(`\\b${sequenceId}\\b`));
        assert.match(buttons[index].props[attribute], new RegExp(`\\b${sampleCount}\\b`));
      }
      assert.equal(buttons[index].props.type, "button");
      assert.equal(buttons[index].props["aria-pressed"], false);
    }
    assert.equal(selectedDetail(h.tree), undefined);

    buttons[1].props.onClick(); await h.settle();
    assert.deepEqual(chunkButtons(h.tree).map((item) => item.props["aria-pressed"]), [false, true]);
    const detailProps = selectedDetail(h.tree).props;
    assert.deepEqual([detailProps.generation, detailProps.captureId, detailProps.sequenceId],
      ["generation-original", "capture-original", 42]);
    assert.equal(detailProps.snapshot.chunks.length, 2);
    assert.equal(typeof detailProps.onSelectSequence, "function");
    assert.equal(typeof detailProps.selectionKey, "number");

    chunkButtons(h.tree)[0].props.onClick(); await h.settle();
    assert.deepEqual(chunkButtons(h.tree).map((item) => item.props["aria-pressed"]), [true, false]);
    assert.equal(selectedDetail(h.tree).props.sequenceId, 41);
  } finally { h.dispose(); }
});

test("retained window and frozen matrix scopes remain distinct as new chunks arrive", async () => {
  const h = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(43)]) });
  try {
    await h.settle();
    assert.match(text(h.tree), /2 retained chunks.*#41–#43/);
    assert.match(text(h.tree), /Gaps may exist/);
    assert.match(text(h.tree), /Select a chunk to inspect/);
    chunkButtons(h.tree)[0].props.onClick(); await h.settle();
    assert.match(text(h.tree), /Selected chunk #41.*fixed/);
    h.update({ snapshot: snapshot([summary(43), summary(48)]) }); await h.settle();
    assert.match(text(h.tree), /2 retained chunks.*#43–#48/);
    assert.match(text(h.tree), /Selected chunk #41.*fixed/);
    assert.equal(selectedDetail(h.tree).props.sequenceId, 41);
  } finally { h.dispose(); }
});

test("new retained chunks preserve the user's existing selection", async () => {
  const h = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(42)]) });
  try {
    await h.settle();
    chunkButtons(h.tree)[0].props.onClick(); await h.settle();
    h.update({ snapshot: snapshot([summary(41), summary(42), summary(47, 1)]) });
    await h.settle();

    assert.deepEqual(chunkButtons(h.tree).map((item) => item.props["aria-pressed"]), [true, false, false]);
    assert.equal(selectedDetail(h.tree).props.sequenceId, 41);
  } finally { h.dispose(); }
});

test("observed or sent chunks never create points before they reach the cache", async () => {
  const h = previewHarness(PreviewChunks, {
    snapshot: snapshot([], { observed: 32, enqueued: 8, received: 0, last_sequence: 42 }),
  });
  try {
    await h.settle();
    assert.equal(chunkButtons(h.tree).length, 0);
    assert.equal(selectedDetail(h.tree), undefined);
    assert.match(text(h.tree), /No chunks retained/i);
  } finally { h.dispose(); }
});

test("leaving the live window preserves the selected detail and its fetched snapshot", async () => {
  const original = api.acqPreviewChunk;
  const calls = [];
  const retained = chunk();
  api.acqPreviewChunk = async (...args) => { calls.push(args); return retained; };
  const h = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(42)]) });
  let detail;
  try {
    await h.settle();
    chunkButtons(h.tree)[0].props.onClick(); await h.settle();
    const selected = selectedDetail(h.tree);
    detail = previewHarness(PreviewChunkDetail, selected.props);
    await detail.settle();
    assert.deepEqual(detail.shown, retained);

    h.update({ snapshot: snapshot([summary(42), summary(47)]) }); await h.settle();

    assert.equal(chunkButtons(h.tree).length, 2);
    assert.deepEqual(chunkButtons(h.tree).map((item) => item.props["aria-pressed"]), [false, false]);
    assert.equal(selectedDetail(h.tree).key, selected.key);
    const updated = selectedDetail(h.tree).props;
    for (const name of ["generation", "captureId", "sequenceId", "selectionKey"]) {
      assert.equal(updated[name], selected.props[name]);
    }
    assert.deepEqual(updated.snapshot.chunks.map((item) => item.sequence_id), [42, 47]);
    const statuses = elements(h.tree).filter((item) => item.props.role === "status");
    assert.match(text(statuses), /41.*left the live window/i);

    detail.update(selectedDetail(h.tree).props); await detail.settle();
    assert.deepEqual(detail.shown, retained);
    assert.deepEqual(calls, [["generation-original", "capture-original", 41]]);

    elements(h.tree).find((item) => item.type === "input").props.onChange({ target: { value: "41" } });
    await h.settle();
    elements(h.tree).find((item) => item.type === "form").props.onSubmit({ preventDefault() {} });
    await h.settle();
    assert.equal(selectedDetail(h.tree).key, selected.key);
    assert.match(text(elements(h.tree).filter((item) => item.props.role === "status")), /41.*left the live window/i);
    detail.update(selectedDetail(h.tree).props); await detail.settle();
    assert.deepEqual(detail.shown, retained);
    assert.equal(calls.length, 1);
  } finally { detail?.dispose(); h.dispose(); api.acqPreviewChunk = original; }
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test("detail uses complete capture identity and ignores an older response", async () => {
  const original = api.acqPreviewChunk;
  const pending = [];
  api.acqPreviewChunk = (...args) => { const request = deferred(); pending.push({ ...request, args }); return request.promise; };
  const h = previewHarness(PreviewChunkDetail, { generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
  try {
    await h.settle();
    assert.deepEqual(pending[0].args, ["generation-original", "capture-original", 41]);
    h.update({ generation: "generation-next", captureId: "capture-next", sequenceId: 42 });
    await h.settle();
    const current = chunk({ generation: "generation-next", capture_id: "capture-next", sequence_id: 42 });
    pending[1].resolve(current); await h.settle();
    assert.deepEqual(h.shown, current);
    pending[0].resolve(chunk()); await h.settle();
    assert.deepEqual(h.shown, current);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("changing a selected chunk clears the previous detail while the next request is pending", async () => {
  const original = api.acqPreviewChunk;
  const next = deferred();
  let calls = 0;
  api.acqPreviewChunk = () => ++calls === 1 ? Promise.resolve(chunk()) : next.promise;
  const h = previewHarness(PreviewChunkDetail, { generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
  try {
    await h.settle();
    assert.equal(h.shown?.sequence_id, 41);
    h.update({ sequenceId: 42 }); await h.settle();
    assert.equal(h.shown, undefined);
    next.reject(new Error("Requested chunk was not retained")); await h.settle();
    assert.equal(h.shown, undefined);
    assert.match(text(h.tree), /Requested chunk was not retained/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("an obsolete request failure cannot hide the newly selected chunk", async () => {
  const original = api.acqPreviewChunk;
  const pending = [];
  api.acqPreviewChunk = () => { const request = deferred(); pending.push(request); return request.promise; };
  const h = previewHarness(PreviewChunkDetail, { generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
  try {
    await h.settle();
    h.update({ captureId: "capture-next" }); await h.settle();
    pending[1].resolve(chunk({ capture_id: "capture-next" })); await h.settle();
    pending[0].reject(new Error("Obsolete failure")); await h.settle();
    assert.equal(h.shown?.capture_id, "capture-next");
    assert.doesNotMatch(text(h.tree), /Obsolete failure/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("preview client methods retain identity in API requests through the shared transport", async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ url: new URL(path, "https://example.test"), options });
    return new Response(JSON.stringify({ success: true, message: "ok", data: null }), { status: 200 });
  };
  try {
    const body = { generation: "generation", connection_id: "connection", expected_revision: "a".repeat(64),
      selection: { rows: [0], sample_start: 0, sample_count: 2, chunk_count: 32, include_trigger: true } };
    await api.acqPreviewStart(body);
    await api.acqPreviewStop("generation", "capture");
    await api.acqPreview("generation");
    await api.acqPreview("generation", "capture");
    await api.acqPreviewChunk("generation", "capture", 41);
    assert.equal(calls[0].url.pathname, "/api/v1/acquisition/preview/start");
    assert.equal(calls[0].options.method, "POST");
    assert.deepEqual(JSON.parse(calls[0].options.body), body);
    assert.equal(calls[1].url.pathname, "/api/v1/acquisition/preview/stop");
    assert.equal(calls[1].options.method, "POST");
    assert.deepEqual(JSON.parse(calls[1].options.body), { generation: "generation", capture_id: "capture" });
    assert.equal(calls[2].url.searchParams.get("generation"), "generation");
    assert.equal(calls[2].url.searchParams.has("capture_id"), false);
    assert.equal(calls[3].url.searchParams.get("capture_id"), "capture");
    assert.equal(calls[4].url.pathname, "/api/v1/acquisition/preview/chunks/41");
    assert.equal(calls[4].url.searchParams.get("generation"), "generation");
    assert.equal(calls[4].url.searchParams.get("capture_id"), "capture");
  } finally { globalThis.fetch = previous; }
});


function searchControl(tree, label) {
  return elements(tree).find((item) => item.props["aria-label"] === label);
}
function searchButton(tree, label) {
  return elements(tree).find((item) => item.type === "button" && text(item) === label);
}
function matchedCells(tree, current = false) {
  const className = current ? "preview-search-current" : "preview-search-match";
  return elements(tree).filter((item) => item.type === "td" && item.props.className?.split(/\s+/).includes(className));
}
async function searchTable(h, value, channel = "all") {
  searchControl(h.tree, "Search channel").props.onChange({ target: { value: channel } });
  searchControl(h.tree, "Equals value").props.onChange({ target: { value } });
  await h.settle();
  searchControl(h.tree, "Search displayed chunk").props.onSubmit({ preventDefault() {} });
  await h.settle();
}

for (const forms of [
  ["1.25", "1.250", "125e-2", "+001.2500", " 1.25 "],
  ["0", "-0", "+0.00", "0e9999999999999999999999999999999"],
  ["-0.125", "-.1250", "-125e-3"],
  ["9007199254740993", "9007199254740993.0", "9.007199254740993e15"],
  ["0.00000000000000000001", "1e-20", ".0000000000000000000100"],
]) {
  test(`equal-value search normalizes decimal representations of ${forms[0]} exactly`, () => {
    const expected = previewValueKey(forms[0]);
    assert.notEqual(expected, null);
    for (const value of forms) assert.equal(previewValueKey(value), expected, value);
  });
}

test("equal-value search never rounds adjacent int64 or decimal values together", () => {
  for (const [left, right] of [
    ["9007199254740992", "9007199254740993"],
    ["-9223372036854775808", "-9223372036854775807"],
    ["0.1", "0.10000000000000001"],
    ["1e9999999999999999999999999999999", "1e9999999999999999999999999999998"],
  ]) {
    assert.notEqual(previewValueKey(left), null);
    assert.notEqual(previewValueKey(right), null);
    assert.notEqual(previewValueKey(left), previewValueKey(right));
  }
});

test("equal-value search supports displayed special values without numeric boolean coercion", () => {
  const keys = ["NaN", "Infinity", "-Infinity", "true", "false", "1", "0"].map(previewValueKey);
  assert.ok(keys.every((key) => key !== null));
  assert.equal(new Set(keys).size, keys.length);
});

test("equal-value search rejects incomplete, nondecimal and excessively long queries", () => {
  for (const value of ["", " ", "+", "-", ".", "1e", "1e+", "1 2", "1,000", "0x10", "0b10",
    "1.2.3", "null", "undefined", "1".repeat(129), "1e" + "9".repeat(127)]) {
    assert.equal(previewValueKey(value), null, value);
  }
});

test("search highlights all displayed channels and navigates actual row and sample indices", async () => {
  const value = chunk({ values: [[2, 8, 2], [0, 2, 1]] });
  const before = structuredClone(value);
  const h = previewHarness(PreviewChunkTable, { chunk: value });
  try {
    await h.settle();
    const options = elements(searchControl(h.tree, "Search channel")).filter((item) => item.type === "option");
    assert.deepEqual(options.map((item) => item.props.value), ["all", "2", "4"]);
    assert.match(text(options[1]), /C3/);
    assert.match(text(options[2]), /trigger/i);
    await searchTable(h, "2.0");
    assert.equal(matchedCells(h.tree).length, 3);
    assert.deepEqual(matchedCells(h.tree).map(text), ["2", "2", "2"]);
    assert.equal(matchedCells(h.tree, true).length, 1);
    assert.match(text(h.tree), /3 matches/);
    assert.match(text(h.tree), /row 2.*sample 5/i);

    searchButton(h.tree, "Next match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 2.*sample 7/i);
    searchButton(h.tree, "Next match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 4.*sample 6/i);
    searchButton(h.tree, "Next match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 2.*sample 5/i);
    searchButton(h.tree, "Previous match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 4.*sample 6/i);
    searchButton(h.tree, "Previous match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 2.*sample 7/i);
    assert.equal(matchedCells(h.tree, true).length, 1);
    assert.deepEqual(value, before);
  } finally { h.dispose(); }
});

test("search can select one captured channel or the trigger row without hiding the matrix", async () => {
  const h = previewHarness(PreviewChunkTable, { chunk: chunk({ values: [[2, 8, 2], [0, 2, 1]] }) });
  try {
    await h.settle();
    await searchTable(h, "2", "2");
    assert.equal(matchedCells(h.tree).length, 2);
    assert.match(text(h.tree), /2 matches/);
    await searchTable(h, "2", "4");
    assert.equal(matchedCells(h.tree).length, 1);
    assert.match(text(h.tree), /row 4.*sample 6/i);
    assert.equal(elements(h.tree).filter((item) => item.type === "td").length, 6);
  } finally { h.dispose(); }
});

test("search reports no matches, rejects invalid input and clears its highlights", async () => {
  const h = previewHarness(PreviewChunkTable, { chunk: chunk({ values: [[2, 8, 2], [0, 2, 1]] }) });
  try {
    await h.settle();
    await searchTable(h, "999");
    assert.equal(matchedCells(h.tree).length, 0);
    assert.match(text(h.tree), /0 matches/);
    for (const label of ["Previous match", "Next match"]) {
      const button = searchButton(h.tree, label);
      assert.ok(!button || button.props.disabled, "No result must not expose active match navigation");
    }
    await searchTable(h, "not a number");
    assert.ok(elements(h.tree).some((item) => item.props.role === "alert"));
    assert.equal(matchedCells(h.tree).length, 0);
    await searchTable(h, "2");
    assert.equal(matchedCells(h.tree).length, 3);
    searchButton(h.tree, "Clear").props.onClick(); await h.settle();
    assert.equal(matchedCells(h.tree).length, 0);
    assert.equal(matchedCells(h.tree, true).length, 0);
    assert.equal(searchControl(h.tree, "Equals value").props.value, "");
    assert.equal(elements(h.tree).filter((item) => item.props.role === "alert").length, 0);
  } finally { h.dispose(); }
});

test("matrix search matches preserved large integers and special values exactly", async () => {
  const h = previewHarness(PreviewChunkTable, { chunk: chunk() });
  try {
    await h.settle();
    for (const [query, expected] of [["9.007199254740993e15", "9007199254740993"],
      ["NaN", "NaN"], ["Infinity", "Infinity"], ["-Infinity", "-Infinity"], ["true", "true"]]) {
      await searchTable(h, query);
      assert.deepEqual(matchedCells(h.tree).map(text), [expected]);
    }
    for (const query of ["9007199254740992", "1", "false"]) {
      await searchTable(h, query);
      assert.equal(matchedCells(h.tree).length, 0, query);
    }
  } finally { h.dispose(); }
});

test("search only inspects the selected snapshot and keeps it selected as the live window advances", async () => {
  const original = api.acqPreviewChunk;
  const calls = [];
  const retained = chunk();
  api.acqPreviewChunk = async (...args) => { calls.push(args); return retained; };
  const chunks = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(42)]) });
  let detail, matrix;
  try {
    await chunks.settle();
    chunkButtons(chunks.tree)[0].props.onClick(); await chunks.settle();
    const selected = selectedDetail(chunks.tree);
    detail = previewHarness(PreviewChunkDetail, selected.props); await detail.settle();
    matrix = previewHarness(PreviewChunkTable, { chunk: detail.shown }); await matrix.settle();
    await searchTable(matrix, "1.25", "2");
    chunks.update({ snapshot: snapshot([summary(42), summary(47)]) }); await chunks.settle();
    detail.update(selectedDetail(chunks.tree).props); await detail.settle();
    matrix.update({ chunk: detail.shown }); await matrix.settle();
    assert.equal(selectedDetail(chunks.tree).key, selected.key);
    assert.equal(selectedDetail(chunks.tree).props.sequenceId, 41);
    assert.deepEqual(matchedCells(matrix.tree).map(text), ["1.25"]);
    assert.deepEqual(calls, [["generation-original", "capture-original", 41]]);
  } finally { matrix?.dispose(); detail?.dispose(); chunks.dispose(); api.acqPreviewChunk = original; }
});


test("editing search criteria clears obsolete highlights until the next search", async () => {
  const h = previewHarness(PreviewChunkTable, { chunk: chunk({ values: [[2, 8, 2], [0, 2, 1]] }) });
  try {
    await h.settle();
    await searchTable(h, "2");
    assert.equal(matchedCells(h.tree).length, 3);
    searchControl(h.tree, "Equals value").props.onChange({ target: { value: "8" } }); await h.settle();
    assert.equal(matchedCells(h.tree).length, 0);
    assert.doesNotMatch(text(h.tree), /3 matches/);
    await searchTable(h, "2");
    searchControl(h.tree, "Search channel").props.onChange({ target: { value: "4" } }); await h.settle();
    assert.equal(matchedCells(h.tree).length, 0);
    assert.doesNotMatch(text(h.tree), /3 matches/);
    await searchTable(h, "2");
    await searchTable(h, "bad value");
    assert.equal(matchedCells(h.tree).length, 0);
    assert.ok(elements(h.tree).some((item) => item.props.role === "alert"));
  } finally { h.dispose(); }
});

async function setSearchScope(h, value = "all") {
  const scope = searchControl(h.tree, "Search scope");
  assert.ok(scope, "Search scope must be selectable");
  scope.props.onChange({ target: { value } });
  await h.settle();
}

test("retained-window scope uses the server limit and resets a channel absent from the current matrix", async () => {
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot([summary(41), { ...summary(42), row_indices: [1, 4] }],
      { limits: { ...limits, max_chunks: 8 } }), onSelectChunk() {},
  });
  try {
    await h.settle();
    assert.match(text(searchControl(h.tree, "Search scope")), /up to 8/);
    assert.doesNotMatch(text(searchControl(h.tree, "Search scope")), /32/);
    await setSearchScope(h);
    searchControl(h.tree, "Search channel").props.onChange({ target: { value: "1" } }); await h.settle();
    assert.equal(searchControl(h.tree, "Search channel").props.value, "1");
    await setSearchScope(h, "current");
    assert.equal(searchControl(h.tree, "Search channel").props.value, "all");
    searchControl(h.tree, "Search channel").props.onChange({ target: { value: "2" } }); await h.settle();
    await setSearchScope(h); await setSearchScope(h, "current");
    assert.equal(searchControl(h.tree, "Search channel").props.value, "2");
  } finally { h.dispose(); }
});

function matrixElement(h) {
  return elements(h.tree).find((item) => item.type === PreviewChunkTable);
}

test("all-chunk search applies the channel filter and navigates frozen result matrices", async () => {
  const original = api.acqPreviewChunk;
  const first = chunk({ values: [[2, 8, 2], [0, 2, 1]] });
  const second = chunk({ sequence_id: 42, values: [[2, 9, 9], [9, 2, 9]], sample_start: 12 });
  const third = chunk({ sequence_id: 43, values: [[9, 9, 9], [9, 9, 9]] });
  const values = new Map([[41, first], [42, second], [43, third]]);
  const calls = [], selected = [];
  api.acqPreviewChunk = async (...args) => { calls.push(args); return values.get(args[2]); };
  let h;
  h = previewHarness(PreviewChunkTable, {
    chunk: first, snapshot: snapshot([summary(41), summary(42), summary(43)]),
    onSelectChunk: (value) => { selected.push(value); h.update({ chunk: value }); },
  });
  try {
    await h.settle();
    assert.equal(searchControl(h.tree, "Search scope").props.value, "current");
    await setSearchScope(h);
    await searchTable(h, "2.0", "2");
    assert.match(text(h.tree), /3 matches/);
    assert.match(text(h.tree), /Searched 3 of 3 chunks/i);
    assert.ok(calls.some((args) => args[2] === 42));
    assert.ok(calls.some((args) => args[2] === 43));
    assert.ok(calls.every((args) => args[0] === "generation-original" && args[1] === "capture-original"));
    assert.equal(matchedCells(h.tree).length, 2);
    searchButton(h.tree, "Next match").props.onClick(); await h.settle();
    assert.match(text(h.tree), /row 2.*sample 7/i);
    searchButton(h.tree, "Next match").props.onClick(); await h.settle();
    assert.equal(selected.at(-1), second);
    assert.match(text(h.tree), /Chunk #42/);
    assert.match(text(h.tree), /row 2.*sample 12/i);
    assert.equal(matchedCells(h.tree).length, 1);
    const fetched = calls.length;
    h.update({ snapshot: snapshot([summary(43), summary(44)]) }); await h.settle();
    searchButton(h.tree, "Previous match").props.onClick(); await h.settle();
    assert.equal(selected.at(-1), first);
    assert.match(text(h.tree), /3 matches/);
    assert.equal(calls.length, fetched, "Navigating loaded results must not fetch evicted chunks again");
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("all-chunk search freezes its submitted window and fetches at most four chunks concurrently", async () => {
  const original = api.acqPreviewChunk;
  const pending = [], calls = [];
  let active = 0, maximum = 0;
  api.acqPreviewChunk = (...args) => {
    const request = deferred();
    calls.push(args); pending.push({ ...request, sequence: args[2] });
    active += 1; maximum = Math.max(maximum, active);
    return request.promise.finally(() => { active -= 1; });
  };
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot(Array.from({ length: 6 }, (_, index) => summary(50 + index))),
    onSelectChunk: (value) => h.update({ chunk: value }),
  });
  try {
    await h.settle(); await setSearchScope(h); await searchTable(h, "2");
    assert.equal(pending.length, 4);
    h.update({ snapshot: snapshot([summary(60), summary(61)]) }); await h.settle();
    pending.slice(0, 4).forEach((request) => request.resolve(chunk({ sequence_id: request.sequence,
      values: [[2, 9, 9], [9, 9, 9]] })));
    await h.settle();
    assert.equal(pending.length, 6);
    pending.slice(4).forEach((request) => request.resolve(chunk({ sequence_id: request.sequence,
      values: [[2, 9, 9], [9, 9, 9]] })));
    await h.settle();
    assert.equal(maximum, 4);
    assert.deepEqual(calls.map((args) => args[2]), [50, 51, 52, 53, 54, 55]);
    assert.match(text(h.tree), /Searched 6 of 6 chunks/i);
    assert.match(text(h.tree), /6 matches/);
    assert.doesNotMatch(text(h.tree), /Chunk #60|Chunk #61/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("all-chunk search reports partial coverage and rejects every mismatched identity", async () => {
  const original = api.acqPreviewChunk;
  const values = new Map([
    [50, chunk({ sequence_id: 50, values: [[2, 9, 9], [9, 9, 9]] })],
    [51, new Error("Requested chunk was not retained")],
    [52, chunk({ sequence_id: 52, generation: "generation-obsolete" })],
    [53, chunk({ sequence_id: 53, capture_id: "capture-obsolete" })],
    [54, chunk({ sequence_id: 999 })],
    [55, null],
    [56, chunk({ sequence_id: 56, connection_id: "connection-obsolete" })],
    [57, chunk({ sequence_id: 57, source_id: "source-obsolete" })],
    [58, chunk({ sequence_id: 58, config_revision: "b".repeat(64) })],
  ]);
  api.acqPreviewChunk = async (_generation, _capture, sequence) => {
    const value = values.get(sequence);
    if (value instanceof Error) throw value;
    return value;
  };
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot([...values.keys()].map((sequence) => summary(sequence))),
    onSelectChunk: (value) => h.update({ chunk: value }),
  });
  try {
    await h.settle(); await setSearchScope(h); await searchTable(h, "2");
    await h.settle();
    assert.match(text(h.tree), /1 match\b/);
    assert.match(text(h.tree), /Searched 1 of 9 chunks/i);
    assert.match(text(h.tree), /Unavailable chunks/i);
    for (const sequence of [51, 52, 53, 54, 55, 56, 57, 58]) assert.match(text(h.tree), new RegExp(`\\b${sequence}\\b`));
    assert.match(text(h.tree), /Chunk #50/);
    assert.doesNotMatch(text(h.tree), /Chunk #999/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

for (const action of ["query", "channel", "scope", "clear", "unmount"]) {
  test(`${action} cancels pending all-chunk results and prevents later batches`, async () => {
    const original = api.acqPreviewChunk;
    const pending = [], selected = [];
    api.acqPreviewChunk = (...args) => {
      const request = deferred(); pending.push({ ...request, sequence: args[2] }); return request.promise;
    };
    const h = previewHarness(PreviewChunkTable, {
      chunk: chunk(), snapshot: snapshot(Array.from({ length: 8 }, (_, index) => summary(50 + index))),
      onSelectChunk: (value) => selected.push(value),
    });
    try {
      await h.settle(); await setSearchScope(h); await searchTable(h, "2");
      assert.equal(pending.length, 4);
      if (action === "query") searchControl(h.tree, "Equals value").props.onChange({ target: { value: "9" } });
      else if (action === "channel") searchControl(h.tree, "Search channel").props.onChange({ target: { value: "4" } });
      else if (action === "scope") searchControl(h.tree, "Search scope").props.onChange({ target: { value: "current" } });
      else if (action === "clear") searchButton(h.tree, "Clear").props.onClick();
      else h.dispose();
      await h.settle();
      pending.forEach((request) => request.resolve(chunk({ sequence_id: request.sequence,
        values: [[2, 9, 9], [9, 9, 9]] })));
      await h.settle();
      assert.equal(pending.length, 4, "Cancelled searches must not start additional requests");
      assert.equal(selected.length, 0, "Cancelled results must not navigate the selected chunk");
      assert.equal(matchedCells(h.tree).length, 0);
      assert.doesNotMatch(text(h.tree), /4 matches|Searched 4 of 8 chunks/i);
    } finally { h.dispose(); api.acqPreviewChunk = original; }
  });
}

test("all-chunk search remains available when the selected matrix has no samples", async () => {
  const original = api.acqPreviewChunk;
  const second = chunk({ sequence_id: 42, values: [[2, 9, 9], [9, 9, 9]] });
  api.acqPreviewChunk = async (_generation, _capture, sequence) => sequence === 42 ? second : chunk({ sample_count: 0, values: [[], []] });
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk({ sample_count: 0, values: [[], []] }), snapshot: snapshot([summary(41, 0), summary(42)]),
    onSelectChunk: (value) => h.update({ chunk: value }),
  });
  try {
    await h.settle();
    assert.equal(searchButton(h.tree, "Search").props.disabled, true);
    await setSearchScope(h);
    assert.equal(!!searchButton(h.tree, "Search").props.disabled, false);
    await searchTable(h, "2");
    assert.match(text(h.tree), /1 match\b/);
    assert.match(text(h.tree), /Chunk #42/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("result navigation preserves the matrix component and green-dot selection without another GET", async () => {
  const original = api.acqPreviewChunk;
  const first = chunk(), second = chunk({ sequence_id: 42 });
  const calls = [];
  api.acqPreviewChunk = async (...args) => { calls.push(args); return args[2] === 41 ? first : second; };
  const chunks = previewHarness(PreviewChunks, { snapshot: snapshot([summary(41), summary(42)]) });
  let detail;
  try {
    await chunks.settle(); chunkButtons(chunks.tree)[0].props.onClick(); await chunks.settle();
    const initial = selectedDetail(chunks.tree);
    detail = previewHarness(PreviewChunkDetail, initial.props); await detail.settle();
    const initialTable = matrixElement(detail);
    assert.equal(initialTable.props.chunk, first);
    assert.equal(typeof initialTable.props.onSelectChunk, "function");
    initialTable.props.onSelectChunk(second);
    await chunks.settle(); detail.update(selectedDetail(chunks.tree).props); await detail.settle();
    assert.deepEqual(chunkButtons(chunks.tree).map((item) => item.props["aria-pressed"]), [false, true]);
    assert.equal(selectedDetail(chunks.tree).key, initial.key);
    assert.equal(selectedDetail(chunks.tree).props.selectionKey, initial.props.selectionKey);
    assert.equal(matrixElement(detail).key, initialTable.key);
    assert.equal(detail.shown, second);
    assert.deepEqual(calls, [["generation-original", "capture-original", 41]]);

    chunkButtons(chunks.tree)[1].props.onClick(); await chunks.settle();
    detail.update(selectedDetail(chunks.tree).props); await detail.settle();
    assert.notEqual(selectedDetail(chunks.tree).props.selectionKey, initial.props.selectionKey);
    assert.notEqual(matrixElement(detail).key, initialTable.key, "Explicit dot selection must reset search state");
    assert.equal(detail.shown, second);
    assert.equal(calls.length, 1);
  } finally { detail?.dispose(); chunks.dispose(); api.acqPreviewChunk = original; }
});

test("all-chunk search honors the retained chunk limit and reports zero matches honestly", async () => {
  const original = api.acqPreviewChunk;
  const calls = [];
  api.acqPreviewChunk = async (...args) => { calls.push(args); return chunk({ sequence_id: args[2] }); };
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot(Array.from({ length: 36 }, (_, index) => summary(50 + index))),
    onSelectChunk: () => assert.fail("A search with no matches must not navigate"),
  });
  try {
    await h.settle(); await setSearchScope(h); await searchTable(h, "999");
    assert.deepEqual(calls.map((args) => args[2]), Array.from({ length: 32 }, (_, index) => 54 + index));
    assert.match(text(h.tree), /Searched 32 of 32 chunks/i);
    assert.match(text(h.tree), /0 matches/);
    assert.equal(matchedCells(h.tree).length, 0);
    for (const label of ["Previous match", "Next match"]) {
      const button = searchButton(h.tree, label);
      assert.ok(!button || button.props.disabled);
    }
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("a superseded all-chunk request cannot replace a newer search or its selected matrix", async () => {
  const original = api.acqPreviewChunk;
  const pending = [], selected = [];
  api.acqPreviewChunk = (...args) => { const request = deferred(); pending.push({ ...request, sequence: args[2] }); return request.promise; };
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot([summary(50), summary(51)]),
    onSelectChunk: (value) => { selected.push(value); h.update({ chunk: value }); },
  });
  try {
    await h.settle(); await setSearchScope(h); await searchTable(h, "2");
    assert.equal(pending.length, 2);
    await searchTable(h, "9");
    assert.equal(pending.length, 4);
    const next = chunk({ sequence_id: 51, values: [[9, 3, 3], [3, 3, 3]] });
    pending[2].resolve(chunk({ sequence_id: 50, values: [[3, 3, 3], [3, 3, 3]] }));
    pending[3].resolve(next); await h.settle();
    assert.match(text(h.tree), /1 match\b/);
    assert.equal(selected.at(-1), next);
    pending[0].resolve(chunk({ sequence_id: 50, values: [[2, 2, 2], [2, 2, 2]] }));
    pending[1].reject(new Error("Obsolete transport failure")); await h.settle();
    assert.match(text(h.tree), /1 match\b/);
    assert.deepEqual(matchedCells(h.tree).map(text), ["9"]);
    assert.equal(selected.length, 1);
    assert.equal(selected.at(-1), next);
    assert.doesNotMatch(text(h.tree), /Obsolete transport failure|Results are partial/);
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});

test("invalid all-chunk criteria make no network requests", async () => {
  const original = api.acqPreviewChunk;
  let calls = 0;
  api.acqPreviewChunk = async () => { calls += 1; return chunk(); };
  const h = previewHarness(PreviewChunkTable, {
    chunk: chunk(), snapshot: snapshot([summary(41), summary(42)]), onSelectChunk() {},
  });
  try {
    await h.settle(); await setSearchScope(h); await searchTable(h, "not a number");
    assert.equal(calls, 0);
    assert.ok(elements(h.tree).some((item) => item.props.role === "alert"));
  } finally { h.dispose(); api.acqPreviewChunk = original; }
});
