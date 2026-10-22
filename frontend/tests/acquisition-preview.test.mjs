/** Finite previews display retained samples and never reuse another capture's response. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { PreviewChunkTable, PreviewChunkDetail, parsePreviewSelection } = await server.ssrLoadModule("/src/AcquisitionPreview.tsx");
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

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function table(value) {
  const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = current.current;
  current.current = { useContext: () => english };
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

function detailHarness(initialProps) {
  const hooks = [];
  let cursor = 0, dirty = true, tree, pendingEffects = [];
  const props = { ...initialProps };
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (next) => {
        const value = typeof next === "function" ? next(hooks[index].value) : next;
        if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; dirty = true; }
      }];
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
    get shown() { return elements(tree).find((item) => item.type === PreviewChunkTable)?.props.chunk; },
    update(next) { Object.assign(props, next); dirty = true; },
    async settle() {
      for (let round = 0; round < 12; round += 1) {
        if (dirty) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          current.current = dispatcher; cursor = 0; dirty = false;
          try { tree = PreviewChunkDetail(props); } finally { current.current = previous; }
          const effects = pendingEffects; pendingEffects = [];
          effects.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(dirty, false, "Preview detail did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test("detail uses complete capture identity and ignores an older response", async () => {
  const original = api.acqPreviewChunk;
  const pending = [];
  api.acqPreviewChunk = (...args) => { const request = deferred(); pending.push({ ...request, args }); return request.promise; };
  const h = detailHarness({ generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
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
  const h = detailHarness({ generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
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
  const h = detailHarness({ generation: "generation-original", captureId: "capture-original", sequenceId: 41 });
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
