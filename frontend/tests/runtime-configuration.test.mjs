import { runtimeController, currentController } from "./module-fixtures.mjs";
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
const { useRuntimeControls } = await server.ssrLoadModule("/src/hooks/useRuntimeControls.ts");
const { I18nProvider, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { TrialResults } = await server.ssrLoadModule("/src/TrialResults.tsx");
const { readTrialControls } = await server.ssrLoadModule("/src/plugin-controls.ts");
globalThis.window = { localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
const noop = () => {};
const current = { ready: true, error: null, entries: {}, filterOrder: [], hasChanges: () => false, validationError: () => null };
const revisions = { acquisition: "a", processing: "selected-previous", stimulation: "s", actuation: "e", engine: "sys" };
const status = { running: true, accepted: revisions, applied: revisions, generation: "generation-1", operations: {}, recovery: {}, errors: {} };
const plan = { plan_id: "plan-1", module: "processing", expected_revision: revisions.processing, expected_revisions: { processing: revisions.processing, engine: revisions.engine }, target_revisions: { processing: "selected-next" }, applied_revisions: status.applied,
  generation: status.generation, running: true, strategy: "trial_boundary", changed_slots: ["processing.decoder"], changed_settings: [], errors: [] };
const baseRuntime = { status, plan, operation: null, blocked: false, inProgress: false, recoveryRequired: false, submitting: false,
  uncertain: false, error: null, requestError: null, review: noop, apply: noop, dismissPlan: noop, refresh: noop, retryUnconfirmed: noop };
const render = (component, props) => renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(component, { ...props, runtime: runtimeController(props.runtime) })));
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
  assert.match(rebuild, /Restart the affected module/);
  assert.match(rebuild, /The selected module will restart/);
  assert.doesNotMatch(rebuild, /The engine will restart/);
  const engineRebuild = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, module: "engine", strategy: "rebuild" } } });
  assert.match(engineRebuild, /All running modules will be interrupted/);
  const nextStart = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, strategy: "select_on_start" } } });
  assert.match(nextStart, /It does not start any module/);
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
    assert.equal(runtimeBlockReason(runtimeController(runtime)), expected);
    const html = render(RuntimeConfigurationReview, { runtime });
    assert.match(html, /id="configuration-review-blocked"/);
    assert.equal(buttonDisabled(html, "Apply configuration"), true);
  }
});

for (const [field, value] of [["accepted", { ...revisions, processing: "changed" }], ["applied", { ...revisions, processing: "changed" }], ["generation", "changed"], ["running", false]]) {
  test(`a changed ${field} invalidates the preview without applying it`, () => {
    const html = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, status: { ...status, [field]: value } } });
    assert.match(html, /runtime or selected configuration changed/);
    assert.equal(buttonDisabled(html, "Apply configuration"), true);
    assert.equal(buttonDisabled(html, "Cancel"), false);
  });
}

test("configuration feedback retains errors and recovery without revision details", () => {
  const runtime = { ...baseRuntime, recoveryRequired: true,
    status: { ...status, accepted: { ...revisions, processing: "selected-new" }, applied: { ...revisions, processing: "applied-old" } },
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
    { status: { ...status, running: false, applied: null } },
    { status: { ...status, applied: { ...revisions, processing: "older-configuration" } } },
    { status: { ...status, running: false, accepted: {}, applied: null } },
    { operation: { status: "queued", stage: "queued" } },
    { operation: { status: "applying", stage: "waiting_for_trial_boundary" } },
    { operation: { status: "succeeded", stage: "complete", result: { outcome: "selected" } } },
    { operation: { status: "succeeded", stage: "complete", result: { outcome: "applied" } } },
  ]) {
    assert.equal(render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, ...overrides } }), "");
  }
});

test("Run preserves independent module controls and permits Stop while configuration operations are pending", () => {
  const props = { engine: { data: { running: true, acquisition_alive: true, processor_alive: true }, error: null, refresh: noop },
    acquisition: { data: { connected: true, streaming: true, sample_rate: 250, channel_count: 8 }, error: null, refresh: noop, pending: false,
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name, { run: noop, pending: false, error: null }])) },
    feed: { generation: "engine-1", trials: [], acks: [] }, current, onBusyChange: noop, onEditSetup: noop, onResults: noop };
  function RunWithControls({ runtime: value, streaming }) {
    const runtime = runtimeController(value);
    const configuration = currentController(null, current);
    const acquisition = { ...props.acquisition, data: { ...props.acquisition.data, streaming } };
    const runtimeControls = useRuntimeControls(runtime, configuration, acquisition, props.engine);
    return React.createElement(RunConsole, { ...props, runtime, current: configuration, acquisition, runtimeControls });
  }
  const run = (runtime, streaming) => renderToStaticMarkup(React.createElement(I18nProvider, null,
    React.createElement(RunWithControls, { runtime, streaming })));
  for (const streaming of [false, true]) {
    const pending = run({ ...baseRuntime, blocked: true, inProgress: true }, streaming);
    assert.doesNotMatch(pending, /aria-label="Engine progress"/);
    assert.match(pending, /Start processing/);
    assert.match(pending, /Start actuation/);
    for (const label of ["Start engine", "Connect", "Disconnect", "Start reading", "Launch stimulation", "Trigger trial"]) {
      assert.equal(buttonDisabled(pending, label), true, label);
    }
    const recovery = run({ ...baseRuntime, status: { ...status, recovery: { engine: "Foundation state unknown" } }, blocked: true, recoveryRequired: true }, streaming);
    assert.equal(buttonDisabled(recovery, "Stop engine"), false);
    assert.equal(buttonDisabled(recovery, "Stop reading"), !streaming);
    assert.equal(buttonDisabled(recovery, "Start reading"), true);
    for (const label of ["Start engine", "Connect", "Disconnect", "Launch stimulation", "Trigger trial"]) {
      assert.equal(buttonDisabled(recovery, label), true, label);
    }
    for (const html of [pending, recovery]) {
      const engineControls = html.match(/<section\b[^>]*aria-label="Engine controls"[^>]*>[\s\S]*?<\/section>/)?.[0];
      assert.ok(engineControls, "Engine controls remain visible while configuration changes or recovery is needed");
      assert.ok(html.indexOf(engineControls) < html.indexOf('aria-label="Current parameters"'));
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
    { trial_number: 1, target_id: 2, event_id: 1, confidence: null, forced: false, produced_at: "2026-09-24T00:00:00", processing_revision: "old-trial-revision" },
    { trial_number: 2, target_id: 2, event_id: 1, confidence: null, forced: false, produced_at: "2026-09-24T00:00:01", processing_revision: "new-trial-revision" },
  ] });
  assert.match(html, /title="old-trial-revision"/);
  assert.match(html, /title="new-trial-revision"/);
});

test("trial controls validate supported fields and numeric defaults before rendering", () => {
  const controls = { count: { type: "int", field: "n_trials", default: 1, min: 1 }, cue: { field: "trial_params", payload_key: "event_id", default: 2, max: 40 } };
  const copied = readTrialControls(controls);
  assert.deepEqual(copied, controls);
  copied.count.default = 10;
  assert.equal(controls.count.default, 1);
  for (const invalid of [null, [], { a: null }, { a: { field: "unknown" } },
    { a: { field: "n_trials", type: "text" } }, { a: { field: "n_trials", label: {} } },
    { a: { field: "n_trials", default: 1.5 } }, { a: { field: "trial_params", payload_key: "event_id", min: NaN } },
    { a: { field: "trial_params", payload_key: "event_id", min: 2, max: 1 } }, { a: { field: "trial_params", payload_key: "event_id", default: 5, max: 3 } },
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
  dispatcher.current = {
    useState: (initial) => [typeof initial === "function" ? initial() : initial, noop],
    useContext: () => english, useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback, useEffect: noop,
  };
  let tree;
  try {
    const engine = { data: { running: true, processor_alive: true }, error: null, refresh: noop };
    const acquisition = { data: { connected: true, streaming: false }, error: null, refresh: noop, pending: false,
      ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name, { run: noop, pending: false, error: null }])) };
    const runtime = runtimeController(baseRuntime);
    const configuration = currentController({ revision: revisions.processing, bindings: [{ slot: "processing.decoder", kind: "decoder",
      selection: { package_id: "example.decoder", contribution_id: "main", version: "1.0.0" }, parameters: {} }] }, current);
    const runtimeControls = useRuntimeControls(runtime, configuration, acquisition, engine);
    tree = RunConsole({ engine, acquisition,
      feed: { generation: null, trials: [], acks: [] }, runtime, current: configuration,
      runtimeControls: { ...runtimeControls, signal: { data: { active_input: activeInput, readiness: { ready: true } }, error: null } },
      onBusyChange: noop, onEditSetup: noop, onResults: noop });
  } finally { dispatcher.current = previous; }
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, tree));
  assert.ok(html.includes(`<strong>${expected}</strong>`));
  assert.doesNotMatch(html, /Active reference mode|montage|excluded channels/i);
});


test("unrelated accepted and applied module changes do not stale a processing review", () => {
  const html = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, status: { ...status,
    accepted: { ...revisions, stimulation: "next-stimulation" }, applied: { ...revisions, stimulation: "next-stimulation" },
  } } });
  assert.equal(buttonDisabled(html, "Apply configuration"), false);
  assert.doesNotMatch(html, /runtime or selected configuration changed/);
});

test("shared engine changes invalidate a module review", () => {
  const html = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, status: { ...status,
    accepted: { ...revisions, engine: "next-engine" },
  } } });
  assert.equal(buttonDisabled(html, "Apply configuration"), true);
});

test("an applied configuration reports a failed history write without calling the application failed", () => {
  const html = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime,
    operation: { module: "processing", status: "succeeded", result: { applied: true, history_saved: false,
      warning: "Configuration applied, but its history could not be saved." } } } });
  assert.match(html, /Configuration applied, but its history could not be saved/);
  assert.doesNotMatch(html, /Recovery is required|Update failed/);
});
