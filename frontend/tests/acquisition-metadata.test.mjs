/** Source metadata stays read-only and preserves fields supplied by the data source. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
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
  assert.match(text(table), /Expected \(applied profile\)Observed \(data source\)/);
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
  const tree = render({ value, canUseObserved: true, onUseObserved: () => {}, onSaveObservedProfile: () => {} });
  assert.match(text(tree), /cannot independently verify/);
  assert.match(text(tree), /Declared \(configuration\)/);
  assert.doesNotMatch(text(tree), /Use observed values/);
  assert.doesNotMatch(text(tree), /Save observed values as new profile/);
});

test("saving observations as a new profile requires its own explicit action and a current source", () => {
  let saves = 0;
  const value = { ...source("Device information"), comparison: comparison("mismatched", { sample_rate: 1000, channel_count: 10 }) };
  const props = { value, canUseObserved: true, onSaveObservedProfile: () => { saves += 1; } };
  const tree = render(props);
  const save = elements(tree).find((element) => element.type === "button" && text(element) === "Save observed values as new profile");
  assert.equal(saves, 0);
  assert.equal(save.props.disabled, false);
  save.props.onClick();
  assert.equal(saves, 1);
  for (const overrides of [{ busy: true }, { canUseObserved: false }]) {
    const disabled = elements(render({ ...props, ...overrides }))
      .find((element) => element.type === "button" && text(element) === "Save observed values as new profile");
    assert.equal(disabled.props.disabled, true);
  }
});

test("missing source information offers no new-profile action", () => {
  for (const value of [null, source("Device information", null), source("Device information")]) {
    const tree = render({ value, canUseObserved: true, onSaveObservedProfile: () => {} });
    assert.doesNotMatch(text(tree), /Save observed values as new profile/);
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
