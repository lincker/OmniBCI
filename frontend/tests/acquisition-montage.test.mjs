/** The acquisition editor supplements missing source facts without replacing observations. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { AcquisitionMontage, acquisitionMontageErrors } = await server.ssrLoadModule("/src/AcquisitionMontage.tsx");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const clone = (value) => structuredClone(value);
const observed = { source_id: "curry8", sample_rate: 500, trigger_row_index: 2, connection_id: "connection", native_event: "separate-events", provenance: "device-reported",
  channels: [{ index: 0, name: "C3", signal_type: "eeg", unit: "uV", position: [0.01, 0.02, 0.03], coordinate_frame: "head", position_provenance: "device-reported" },
    { index: 1, name: "CH2", name_provenance: "generated", signal_type: "unknown" }] };
function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function editor(overrides = {}) {
  const props = { observed: null, value: null, sourceId: "curry8", parameters: { sample_rate: 500, channel_number: 3 }, busy: false,
    canRead: true, readHint: "Connect this saved profile before reading.", onRead: () => {}, ...overrides };
  props.onChange = (value) => { props.value = value; };
  return { props, get tree() { return AcquisitionMontage(props); } };
}
function control(view, label) {
  const result = elements(view.tree).find((element) => element.props["aria-label"] === label);
  assert.ok(result, `Missing control: ${label}`); return result;
}
function change(view, label, value) {
  const target = control(view, label);
  assert.equal(!!target.props.readOnly, false, `${label} is read-only`);
  assert.equal(!!target.props.disabled, false, `${label} is disabled`);
  target.props.onChange({ target: { value } });
}

test("reported source fields stay read-only while missing fields can be supplied", () => {
  const original = clone(observed);
  const view = editor({ observed });
  for (const label of ["Source row 1 name", "Source row 1 unit", "Source row 1 x (m)", "Montage sample rate", "Montage signal channel count"]) {
    assert.equal(control(view, label).props.readOnly, true, label);
  }
  assert.equal(control(view, "Source row 1 type").props.disabled, true);
  change(view, "Source row 2 name", "C4");
  change(view, "Source row 2 type", "eeg");
  change(view, "Source row 2 unit", "uV");
  change(view, "Montage original reference", "Cz");
  assert.deepEqual(view.props.observed, original, "Editing must never mutate source observations");
  assert.equal(view.props.value.provenance, "user-declared");
  assert.equal(view.props.value.connection_id, "");
  assert.equal(view.props.value.channels[0].name, "C3");
  assert.equal(view.props.value.channels[0].signal_type, "unknown");
  assert.equal(view.props.value.channels[0].unit, undefined);
  assert.equal(view.props.value.channels[0].position, undefined);
  assert.equal(view.props.value.channels[1].name, "C4");
  assert.equal(view.props.value.channels[1].name_provenance, undefined);
  assert.equal(view.props.value.channels[1].unit, "uV");
  assert.equal(view.props.value.original_reference, "Cz");
  assert.deepEqual(acquisitionMontageErrors(view.props.value), []);
});

test("filling one missing fact does not turn reported facts into manual fallback for another recording", () => {
  const firstRecording = { ...clone(observed), original_reference: "Cz" };
  const view = editor({ observed: firstRecording });
  change(view, "Source row 2 unit", "uV");
  assert.equal(view.props.value.original_reference, undefined);
  assert.deepEqual(view.props.value.channels[0], { index: 0, name: "C3", signal_type: "unknown" });
  assert.equal(control(view, "Source row 1 unit").props.value, "uV", "The current observation remains visible");
  assert.equal(control(view, "Source row 1 x (m)").props.value, 0.01);
  view.props.observed = { ...clone(observed), channels: [
    { index: 0, name: "C3", signal_type: "unknown" },
    { index: 1, name: "CH2", name_provenance: "generated", signal_type: "unknown" },
  ] };
  assert.equal(control(view, "Source row 1 unit").props.value, "");
  assert.equal(control(view, "Source row 1 x (m)").props.value, "");
  assert.equal(control(view, "Montage original reference").props.value, "");
  assert.equal(control(view, "Source row 2 unit").props.value, "uV", "Keep the fact that the user explicitly supplied");
});

test("offline declarations derive signal count from profile output rows and keep unknown fields unknown", () => {
  const view = editor({ sourceId: "scan45", parameters: { sample_rate: 1000, channel_number: 4 }, canRead: false });
  assert.equal(view.props.value, null, "Rendering must not create a manual declaration");
  assert.equal(control(view, "Montage signal channel count").props.value, 3);
  change(view, "Source row 1 name", "P3");
  assert.equal(view.props.value.source_id, "scan45");
  assert.equal(view.props.value.sample_rate, 1000);
  assert.equal(view.props.value.trigger_row_index, 3);
  assert.equal(view.props.value.channels.length, 3);
  assert.equal(view.props.value.channels[0].signal_type, "unknown");
  assert.equal(view.props.value.channels[0].unit, undefined);
  assert.match(text(view.tree), /No source observation is saved/);
});

test("positions remain optional and incomplete coordinates prevent saving", () => {
  const view = editor({ observed });
  change(view, "Source row 2 name", "C4");
  assert.deepEqual(acquisitionMontageErrors(view.props.value), []);
  change(view, "Source row 2 x (m)", "0.01");
  assert.match(acquisitionMontageErrors(view.props.value).join(" "), /all three finite coordinates/);
  change(view, "Source row 2 y (m)", "-0.02");
  change(view, "Source row 2 z (m)", "0.08");
  assert.match(acquisitionMontageErrors(view.props.value).join(" "), /coordinate frame/);
  change(view, "Source row 2 coordinate frame", "head");
  assert.deepEqual(acquisitionMontageErrors(view.props.value), []);
  assert.equal(view.props.value.channels[1].position_provenance, "user-declared");
  for (const axis of ["x", "y", "z"]) change(view, `Source row 2 ${axis} (m)`, "");
  assert.equal(view.props.value.channels[1].position, undefined);
  assert.deepEqual(acquisitionMontageErrors(view.props.value), []);
});

test("Scan45 declarations are explicitly distinguished from device-reported observations", () => {
  const view = editor({ observed: { ...clone(observed), source_id: "scan45", provenance: "user-declared" } });
  assert.match(text(view.tree), /Configured source declaration/);
  assert.match(text(view.tree), /not reported by the device/);
  assert.doesNotMatch(text(view.tree), /Saved source observation/);
});

test("new observations reveal conflicting manual labels without discarding them", () => {
  const manual = clone(observed); manual.channels[0].name = "P3";
  const view = editor({ observed, value: manual });
  assert.equal(control(view, "Source row 1 name").props.value, "C3");
  assert.equal(view.props.value.channels[0].name, "P3");
  assert.match(text(view.tree), /Manual declaration conflicts with the source/);
});

test("generated manual placeholders accept a reported name without a false conflict", () => {
  const manual = clone(observed);
  manual.channels[0] = { index: 0, name: "CH1", name_provenance: "generated", signal_type: "unknown" };
  const view = editor({ observed, value: manual });
  assert.equal(control(view, "Source row 1 name").props.value, "C3");
  assert.doesNotMatch(text(view.tree), /Manual declaration conflicts with the source/);
});

test("profile requests omit untouched manual metadata and refresh targets the exact profile", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
    return new Response(JSON.stringify({ success: true, data: {} }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const profile = { profile_id: "profile-a", revision: 3, acquisition_montage: observed };
    await pluginConfigApi.update(profile, "Lab", { sample_rate: 500 });
    assert.equal("manual_montage" in calls[0].body, false);
    assert.equal("acquisition_montage" in calls[0].body, false);
    await pluginConfigApi.update(profile, "Lab", { sample_rate: 500 }, null);
    assert.equal(calls[1].body.manual_montage, null, "Explicit clearing must reach the backend");
    const declaration = { ...clone(observed), provenance: "user-declared", connection_id: "" };
    await pluginConfigApi.create("acquisition", { package_id: "example.curry", contribution_id: "main", version: "1.0.0" }, "New lab", {}, declaration);
    assert.deepEqual(calls[2].body.manual_montage, declaration);
    await pluginConfigApi.readMontage("profile-a");
    assert.equal(calls[3].url, "/api/v1/acquisition/metadata/refresh?profile_id=profile-a");
    assert.equal(calls[3].method, "POST");
    const savedManual = { ...declaration, signal_channel_count: 2, output_row_count: 3 };
    await pluginConfigApi.create("acquisition", { package_id: "example.curry", contribution_id: "main", version: "1.0.0" }, "Copied lab", {}, savedManual);
    assert.deepEqual(calls[4].body.manual_montage, declaration, "Duplicate carries manual fields without response-derived counts");
    const resized = { ...savedManual, trigger_row_index: 3, channels: [...declaration.channels,
      { index: 2, name: "CH3", name_provenance: "generated", signal_type: "unknown" }] };
    await pluginConfigApi.update(profile, "Larger montage", { channel_number: 4 }, resized);
    assert.equal(calls[5].body.manual_montage.channels.length, 3);
    assert.equal("signal_channel_count" in calls[5].body.manual_montage, false);
    assert.equal("output_row_count" in calls[5].body.manual_montage, false);
  } finally { globalThis.fetch = originalFetch; }
});
