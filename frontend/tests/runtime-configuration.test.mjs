/** Static render contracts; browser interactions still need browser verification. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { RuntimeConfigurationReview, RuntimeConfigurationStatus } = await server.ssrLoadModule("/src/RuntimeConfiguration.tsx");
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { I18nProvider } = await server.ssrLoadModule("/src/i18n.tsx");
const { TrialResults } = await server.ssrLoadModule("/src/TrialResults.tsx");
const { readTrialControls } = await server.ssrLoadModule("/src/plugin-controls.ts");
globalThis.window = { localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
const noop = () => {};
const status = { running: true, accepted_revision: "selected-previous", applied_revision: "selected-previous", generation: "generation-1", operation: null };
const plan = { plan_id: "plan-1", expected_revision: status.accepted_revision, target_revision: "selected-next", applied_revision: status.applied_revision,
  generation: status.generation, running: true, strategy: "trial_boundary", session_effect: "preserve", changed_slots: ["processing.decoder"], changed_settings: [], errors: [] };
const baseRuntime = { status, plan, operation: null, blocked: false, inProgress: false, recoveryRequired: false, submitting: false,
  uncertain: false, error: null, requestError: null, review: noop, apply: noop, dismissPlan: noop, refresh: noop, retryUnconfirmed: noop };
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
function buttonDisabled(html, label) {
  const tag = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
    .find(([, , body]) => body.replace(/<[^>]*>/g, "") === label);
  assert.ok(tag, `Missing button: ${label}`);
  return /\bdisabled=/.test(tag[1]);
}

test("review presents the impact before Apply and blocks invalid plans", () => {
  const html = render(RuntimeConfigurationReview, { runtime: baseRuntime });
  assert.match(html, /Apply after the current trial completes/);
  assert.match(html, /Preserve the current session/);
  assert.match(html, /processing.decoder/);
  assert.equal(buttonDisabled(html, "Apply configuration"), false);
  const invalid = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, errors: ["Selected package is unavailable"] } } });
  assert.match(invalid, /Selected package is unavailable/);
  assert.equal(buttonDisabled(invalid, "Apply configuration"), true);
  const rebuild = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, plan: { ...plan, strategy: "rebuild", session_effect: "continuation" } } });
  assert.match(rebuild, /Pause and rebuild the engine/);
  assert.match(rebuild, /continuation session/);
});

for (const [field, value] of [["accepted_revision", "changed"], ["applied_revision", "changed"], ["generation", "changed"], ["running", false]]) {
  test(`a changed ${field} invalidates the preview without applying it`, () => {
    const html = render(RuntimeConfigurationReview, { runtime: { ...baseRuntime, status: { ...status, [field]: value } } });
    assert.match(html, /runtime or selected configuration changed/);
    assert.equal(buttonDisabled(html, "Apply configuration"), true);
    assert.equal(buttonDisabled(html, "Cancel"), false);
  });
}

test("operation status distinguishes selected and applied revisions and reports recovery", () => {
  const runtime = { ...baseRuntime, recoveryRequired: true,
    status: { ...status, accepted_revision: "selected-new", applied_revision: "applied-old" },
    operation: { status: "recovery_required", stage: "rollback", error: "Module acknowledgement is missing", rolled_back: false } };
  const html = render(RuntimeConfigurationStatus, { runtime });
  assert.match(html, /Selected <code title="selected-new"/);
  assert.match(html, /Applied <code title="applied-old"/);
  assert.match(html, /Only stop controls are available/);
  assert.match(html, /Module acknowledgement is missing/);
  const uncertain = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime, uncertain: true } });
  assert.match(uncertain, /Check or retry the same operation/);
});

test("select-on-start success is distinguished from an applied runtime change", () => {
  const operation = { status: "succeeded", stage: "complete", result: { outcome: "selected" } };
  const selected = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime,
    status: { ...status, running: false, applied_revision: null }, operation } });
  assert.match(selected, /Selected for next start · complete/);
  assert.match(selected, /Applied <code title="">None/);
  assert.doesNotMatch(selected, /<strong>Applied · complete/);
  const applied = render(RuntimeConfigurationStatus, { runtime: { ...baseRuntime,
    operation: { ...operation, result: { outcome: "applied" } } } });
  assert.match(applied, /<strong>Applied · complete/);
});

test("Run preserves system progress and permits stop controls only after an operation settles", () => {
  const props = { engine: { data: { running: true, acquisition_alive: true, processor_alive: true }, error: null, refresh: noop },
    acquisition: { data: { connected: true, streaming: true, sample_rate: 250, channel_count: 8 }, error: null, refresh: noop },
    feed: { session: { state: "running", session_id: "session-1" }, trials: [], acks: [] }, onBusyChange: noop, onEditSetup: noop, onResults: noop };
  const run = (runtime) => renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RunConsole, { ...props, runtime })));
  const pending = run({ ...baseRuntime, blocked: true, inProgress: true });
  assert.match(pending, /aria-label="System progress"/);
  for (const label of ["Start engine", "Stop engine", "Connect", "Disconnect", "Start reading", "Stop reading", "Launch stimulation", "Trigger trial"]) {
    assert.equal(buttonDisabled(pending, label), true, label);
  }
  const recovery = run({ ...baseRuntime, blocked: true, recoveryRequired: true });
  for (const label of ["Stop engine", "Stop reading"]) assert.equal(buttonDisabled(recovery, label), false, label);
  for (const label of ["Start engine", "Connect", "Disconnect", "Start reading", "Launch stimulation", "Trigger trial"]) {
    assert.equal(buttonDisabled(recovery, label), true, label);
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
