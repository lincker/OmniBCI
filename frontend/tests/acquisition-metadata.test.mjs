/** Source metadata stays read-only and preserves fields supplied by the data source. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { AcquisitionMetadata } = await server.ssrLoadModule("/src/AcquisitionMetadata.tsx");

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function render(overrides = {}) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  dispatcher.current = { useContext: () => english };
  try {
    return AcquisitionMetadata({ value: null, busy: false, canRead: true,
      readHint: "Connect the source before reading metadata.", onRead: () => {}, ...overrides });
  } finally { dispatcher.current = previous; }
}
const source = (metadata) => ({ source_id: "curry8", connection_id: "connection-a", metadata,
  descriptor: metadata.channel_count ? { source_id: "curry8", connection_id: "connection-a", sample_rate: metadata.sample_rate,
    signal_channel_count: metadata.channel_count, output_row_count: metadata.output_channel_count,
    trigger_row_index: metadata.channel_count, native_event: "final-row", provenance: "device-reported",
    channels: metadata.channel_labels.map((name, index) => ({ index, channel_id: `channel-${index}`, name, signal_type: "unknown" })) } : null });
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

test("Curry8 signal channel count is unchanged by the additional event output row", () => {
  const tree = render({ value: source({ device: "Neuroscan CURRY 8", sample_rate: 500,
    channel_count: 2, output_channel_count: 3, channel_labels: ["C3", "C4"] }) });
  assert.equal(text(summary(tree, "Device")), "Neuroscan CURRY 8");
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

test("read-only metadata retains arbitrary nested fields without creating montage declarations", () => {
  const value = source({ device: "Amplifier", protocol_version: { major: 8, minor: 1 },
    capabilities: { allow_recording: true, ranges: [10, 20], extra: null }, channel_labels: ["Cz"],
    channel_positions: [{ coordinate_frame: "device", position: [1, 2, 3] }] });
  const original = structuredClone(value);
  const tree = render({ value });
  assert.equal(elements(tree).filter((element) => ["input", "select", "textarea"].includes(element.type)).length, 0);
  const raw = elements(tree).find((element) => element.type === "pre");
  assert.deepEqual(JSON.parse(text(raw)), original);
  assert.deepEqual(value, original);
  assert.equal(summary(tree, "Signal channels"), undefined, "Do not infer missing counts from labels");
});

test("a source without metadata support identifies configuration values honestly", () => {
  const value = { source_id: "scan45", connection_id: "connection-b", metadata: {
    metadata_supported: false, sample_rate: 1000, output_channel_count: 5, channel_labels: [], provenance: "user-declared",
  } };
  const tree = render({ value });
  assert.match(text(tree), /does not support device metadata/);
  assert.match(text(tree), /connection configuration/);
  assert.equal(summary(tree, "Signal channels"), undefined);
  assert.equal(elements(tree).filter((element) => element.type === "table").length, 0);
  assert.doesNotMatch(text(tree), /CH1|device-reported/);
  assert.deepEqual(JSON.parse(text(elements(tree).find((element) => element.type === "pre"))), value);
});
