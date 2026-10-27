/** Static render contracts; browser interactions still need browser verification. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { RuntimeConfigurationReview, RuntimeConfigurationStatus, RuntimeConfigurationProgress, runtimeBlockReason } = await server.ssrLoadModule("/src/RuntimeConfiguration.tsx");
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { I18nProvider, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { TrialResults } = await server.ssrLoadModule("/src/TrialResults.tsx");
const { readTrialControls } = await server.ssrLoadModule("/src/plugin-controls.ts");
globalThis.window = { localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
const noop = () => {};
const status = { running: true, accepted_revision: "selected-previous", applied_revision: "selected-previous", generation: "generation-1", operation: null };
const plan = { plan_id: "plan-1", expected_revision: status.accepted_revision, target_revision: "selected-next", applied_revision: status.applied_revision,
  generation: status.generation, running: true, strategy: "trial_boundary", changed_slots: ["processing.decoder"], changed_settings: [], errors: [] };
const baseRuntime = { status, plan, operation: null, blocked: false, inProgress: false, recoveryRequired: false, submitting: false,
  uncertain: false, error: null, requestError: null, review: noop, apply: noop, dismissPlan: noop, refresh: noop, retryUnconfirmed: noop };
const render = (component, props) => renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(component, props)));
function buttonDisabled(html, label) {
  const tag = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
    .find(([, , body]) => body.replace(/<[^>]*>/g, "") === label);
  assert.ok(tag, `Missing button: ${label}`);
  return /\bdisabled=/.test(tag[1]);
}

test("review presents the impact before Apply and blocks invalid plans", () => {
  const html = render(RuntimeConfigurationReview, { runtime: baseRuntime });
  assert.match(html, /Apply after the current trial completes/);
  assert.match(html, /processing.decoder/);
  assert.equal(buttonDisabled(html, "Apply configuration"), false);
  const invalid = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, errors: ["Selected package is unavailable"] } } });
  assert.match(invalid, /Selected package is unavailable/);
  assert.equal(buttonDisabled(invalid, "Apply configuration"), true);
  const rebuild = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, strategy: "rebuild" } } });
  assert.match(rebuild, /Pause and rebuild the engine/);
  assert.match(rebuild, /Acquisition or an in-progress epoch may be interrupted/);
  const nextStart = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, strategy: "select_on_start" } } });
  assert.match(nextStart, /It does not start the engine/);
  const channels = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan,
    signal_summary: "Channels: C4, C3; types: EEG" } } });
  assert.match(channels, /Channels: C4, C3; types: EEG/);
});

test("routine apply progress is local and never reports success before completion", () => {
  assert.equal(render(RuntimeConfigurationProgress, { runtime: baseRuntime }), "");
  const waiting = render(RuntimeConfigurationProgress, { runtime: { ...baseRuntime,
    operation: { status: "applying", stage: "waiting_for_trial_boundary" } } });
  assert.match(waiting, /Waiting for the current trial to finish/);
  assert.match(waiting, /role="status"/);
  const uncertain = render(RuntimeConfigurationProgress, { runtime: { ...baseRuntime, uncertain: true,
    operation: { status: "succeeded", stage: "complete" } } });
  assert.match(uncertain, /Confirming the last operation/);
  assert.doesNotMatch(uncertain, /succeeded|successfully/);
});

test("blocked reviews explain whether to wait, reconnect, or recover", () => {
  for (const [overrides, expected] of [
    [{ uncertain: true, inProgress: true }, "feedback.confirming"],
    [{ inProgress: true }, "feedback.applying"],
    [{ recoveryRequired: true }, "feedback.recovery"],
    [{ status: null }, "feedback.runtimeUnavailable"],
  ]) {
    const runtime = { ...baseRuntime, blocked: true, ...overrides };
    assert.equal(runtimeBlockReason(runtime), expected);
    const html = render(RuntimeConfigurationReview, { runtime });
    assert.match(html, /id="configuration-review-blocked"/);
    assert.equal(buttonDisabled(html, "Apply configuration"), true);
  }
});

for (const [field, value] of [["accepted_revision", "changed"], ["applied_revision", "changed"], ["generation", "changed"], ["running", false]]) {
  test(`a changed ${field} invalidates the preview without applying it`, () => {
    const html = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, status: { ...status, [field]: value } } });
    assert.match(html, /runtime or selected configuration changed/);
    assert.equal(buttonDisabled(html, "Apply configuration"), true);
    assert.equal(buttonDisabled(html, "Cancel"), false);
  });
}

test("configuration feedback retains errors and recovery without revision details", () => {
  const runtime = { ...baseRuntime, recoveryRequired: true,
    status: { ...status, accepted_revision: "selected-new", applied_revision: "applied-old" },
    operation: { status: "recovery_required", stage: "rollback", error: "Module acknowledgement is missing", rolled_back: false } };
  const html = render(RuntimeConfigurationStatus, { runtime });
  assert.doesNotMatch(html, /selected-new|applied-old|Configuration details|<details/);
  assert.match(html, /Only stop controls are available/);
  assert.match(html, /Module acknowledgement is missing/);
  const uncertain = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, uncertain: true } });
  assert.match(uncertain, /Confirming operation result…/);
  assert.match(uncertain, /Check or retry the same operation/);
  const failed = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime,
    operation: { status: "failed", stage: "rolling_back", error: "Update failed.", rolled_back: true } } });
  assert.match(failed, /Update failed\. Previous configuration restored\./);
  const unavailable = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, error: "Connection lost" } });
  assert.match(unavailable, /Connection lost/);
  assert.equal(buttonDisabled(unavailable, "Refresh status"), false);
  const rejected = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, requestError: "Review again" } });
  assert.match(rejected, /Review again/);
});

test("routine configuration states render no status bar or revision details", () => {
  for (const overrides of [
    {},
    { status: null },
    { status: { ...status, running: false, applied_revision: null } },
    { status: { ...status, applied_revision: "older-configuration" } },
    { status: { ...status, running: false, accepted_revision: null, applied_revision: null } },
    { operation: { status: "queued", stage: "queued" } },
    { operation: { status: "applying", stage: "waiting_for_trial_boundary" } },
    { operation: { status: "succeeded", stage: "complete", result: { outcome: "selected" } } },
    { operation: { status: "succeeded", stage: "complete", result: { outcome: "applied" } } },
  ]) {
    assert.equal(render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, ...overrides } }), "");
  }
});

test("Run preserves system progress and permits stop controls only after an operation settles", () => {
  const props = { engine: { data: { running: true, acquisition_alive: true, processor_alive: true }, error: null, refresh: noop },
    acquisition: { data: { connected: true, streaming: true, sample_rate: 250, channel_count: 8 }, error: null, refresh: noop, pending: false,
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name, { run: noop, pending: false, error: null }])) },
    feed: { generation: "engine-1", trials: [], acks: [] }, onBusyChange: noop, onEditSetup: noop, onResults: noop };
  const run = (runtime, streaming) => renderToStaticMarkup(React.createElement(I18nProvider, null,
    React.createElement(RunConsole, { ...props, runtime,
      acquisition: { ...props.acquisition, data: { ...props.acquisition.data, streaming } } })));
  for (const streaming of [false, true]) {
    const pending = run({ ...baseRuntime, blocked: true, inProgress: true }, streaming);
    assert.match(pending, /aria-label="System progress"/);
    for (const label of ["Start engine", "Stop engine", "Connect", "Disconnect", "Start reading", "Stop reading", "Launch stimulation", "Trigger trial"]) {
      assert.equal(buttonDisabled(pending, label), true, label);
    }
    const recovery = run({ ...baseRuntime, blocked: true, recoveryRequired: true }, streaming);
    assert.equal(buttonDisabled(recovery, "Stop engine"), false);
    assert.equal(buttonDisabled(recovery, "Stop reading"), !streaming);
    assert.equal(buttonDisabled(recovery, "Start reading"), true);
    for (const label of ["Start engine", "Connect", "Disconnect", "Launch stimulation", "Trigger trial"]) {
      assert.equal(buttonDisabled(recovery, label), true, label);
    }
    for (const html of [pending, recovery]) {
      const engineControls = html.match(/<section\b[^>]*aria-label="Engine controls"[^>]*>[\s\S]*?<\/section>/)?.[0];
      assert.ok(engineControls, "Engine controls remain visible while configuration changes or recovery is needed");
      assert.ok(html.indexOf(engineControls) < html.indexOf('aria-label="Startup configuration"'));
      for (const label of ["Start engine", "Stop engine"]) {
        buttonDisabled(engineControls, label);
        const buttons = [...html.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)]
          .filter(([, body]) => body.replace(/<[^>]*>/g, "").trim() === label);
        assert.equal(buttons.length, 1, label);
      }
      for (const [details] of html.matchAll(/<details\b[^>]*>[\s\S]*?<\/details>/g)) {
        assert.doesNotMatch(details, /Engine controls|Start engine|Stop engine/);
      }
    }
  }
});

test("trial results display the revision pinned to each trial", () => {
  const html = render(TrialResults, { trials: [
    { trial_number: 1, target_id: 2, event_id: 1, confidence: null, forced: false, produced_at: "2026-09-24T00:00:00", config_revision: "old-trial-revision" },
    { trial_number: 2, target_id: 2, event_id: 1, confidence: null, forced: false, produced_at: "2026-09-24T00:00:01", config_revision: "new-trial-revision" },
  ] });
  assert.match(html, /title="old-trial-revision"/);
  assert.match(html, /title="new-trial-revision"/);
});

test("trial controls validate supported fields and numeric defaults before rendering", () => {
  const controls = { count: { type: "int", field: "n_trials", default: 1, min: 1 }, cue: { field: "trial_params", default: 2, max: 40 } };
  const copied = readTrialControls(controls);
  assert.deepEqual(copied, controls);
  copied.count.default = 10;
  assert.equal(controls.count.default, 1);
  for (const invalid of [null, [], { a: null }, { a: { field: "unknown" } },
    { a: { field: "n_trials", type: "text" } }, { a: { field: "n_trials", label: {} } },
    { a: { field: "n_trials", default: 1.5 } }, { a: { field: "trial_params", min: NaN } },
    { a: { field: "trial_params", min: 2, max: 1 } }, { a: { field: "trial_params", default: 5, max: 3 } },
    { a: { field: "n_trials" }, b: { field: "n_trials" } }]) {
    assert.throws(() => readTrialControls(invalid));
  }
});

for (const [name, activeInput, expected] of [
  ["ordered mapped names", { schema_version: 2, outputs: [
    { source_channel_id: "sample-1", name: "right", signal_type: "eeg" },
    { source_channel_id: "sample-0", name: "left" },
  ] }, "right, left"],
  ["all-source identity", { schema_version: 2, outputs: [] }, "All source channels"],
  ["no active mapping", null, "No active channel mapping"],
]) test(`Run reports ${name} from the active channel mapping`, () => {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  let stateIndex = 0;
  // Active mapping is meaningful only after the matching decoder configuration has loaded.
  const seeded = {
    2: { active_input: activeInput, readiness: { ready: true } },
    4: { revision: status.accepted_revision, bindings: [{ slot: "processing.decoder", kind: "decoder",
      selection: { package_id: "example.decoder", contribution_id: "main", version: "1.0.0" }, parameters: {} }] },
    21: { status: "ready", revision: status.accepted_revision },
  };
  dispatcher.current = {
    useState: (initial) => {
      const index = stateIndex++;
      return [Object.hasOwn(seeded, index) ? seeded[index] : typeof initial === "function" ? initial() : initial, noop];
    },
    useContext: () => english, useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback, useEffect: noop,
  };
  let tree;
  try {
    tree = RunConsole({ engine: { data: { running: true }, error: null, refresh: noop },
      acquisition: { data: { connected: true, streaming: false }, error: null, refresh: noop, pending: false,
        ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name, { run: noop, pending: false, error: null }])) },
      feed: { generation: null, trials: [], acks: [] }, runtime: baseRuntime,
      onBusyChange: noop, onEditSetup: noop, onResults: noop });
  } finally { dispatcher.current = previous; }
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, tree));
  assert.ok(html.includes(`<strong>${expected}</strong>`));
  assert.doesNotMatch(html, /Active reference mode|montage|excluded channels/i);
});
