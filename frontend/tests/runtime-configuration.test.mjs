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
const { useRuntimeConfiguration } = await server.ssrLoadModule("/src/hooks/useRuntimeConfiguration.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");
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

const history = { status: "recovery_required", module: "acquisition", affected_modules: ["acquisition"], operation_id: "failed-operation", error: "Unknown startup" };
const recoveryPlan = { expected_revisions: { engine: "sys" }, module: "acquisition", plan_id: "new-plan", target_revisions: { acquisition: "new-revision" }, errors: [] };

function hookRenderer(hookStatus) {
  const values = [hookStatus, recoveryPlan, {}, null, {}];
  const refs = [];
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  return () => {
    let stateIndex = 0, refIndex = 0;
    const previous = dispatcher.current;
    dispatcher.current = {
      useContext: () => english,
      useState: (initial) => {
        const index = stateIndex++;
        if (index >= values.length) values[index] = typeof initial === "function" ? initial() : initial;
        return [values[index], (value) => { values[index] = typeof value === "function" ? value(values[index]) : value; }];
      },
      useRef: (value) => refs[refIndex++] ??= { current: value },
      useCallback: (callback) => callback,
      useEffect: () => {},
    };
    try { const hook = useRuntimeConfiguration(); return { ...hook, ...hook.state("acquisition"), completedOperation: hook.state("acquisition").completedOperation ?? null, completedRestoration: hook.completedRestorations.processing ?? null }; }
    finally { dispatcher.current = previous; }
  };
}

test("a confirmed cleared quarantine permits Apply despite failed-operation history", async () => {
  const hookStatus = { running: false, recovery: {}, operations: { acquisition: history } };
  const hook = hookRenderer(hookStatus)();
  assert.equal(hook.recoveryRequired, false);
  assert.equal(hook.blocked, false);
  assert.equal(hook.operation, history);
  const original = { ...runtimeConfigApi };
  let submitted = 0;
  runtimeConfigApi.apply = async () => { submitted += 1; return { module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" }; };
  runtimeConfigApi.operation = async () => ({ module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" });
  runtimeConfigApi.status = async () => hookStatus;
  try { await hook.apply(); assert.equal(submitted, 1); }
  finally { Object.assign(runtimeConfigApi, original); }
});

for (const [label, hookStatus] of [
  ["not ready but quarantined", { running: false, recovery: { engine: "Unknown runtime" }, operations: { acquisition: history } }],
  ["missing status", null],
  ["missing recovery acknowledgement", { running: false, operations: { acquisition: history } }],
]) {
  test(`${label} remains blocked`, async () => {
    const hook = hookRenderer(hookStatus)();
    assert.equal(hook.blocked, true);
    const original = runtimeConfigApi.apply;
    let submitted = 0;
    runtimeConfigApi.apply = async () => { submitted += 1; return { module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" }; };
    try { await hook.apply(); assert.equal(submitted, 0); }
    finally { runtimeConfigApi.apply = original; }
  });
}

test("a rejected host-settings review clears the previous plan and displays the server error", async () => {
  const hookStatus = { running: false, recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const message = "Server restart required for LOGGING.level";
  const request = { module: "engine", bindings: [], refresh_slots: [] };
  let submitted = 0;
  runtimeConfigApi.plan = async (body) => {
    assert.deepEqual(body, request);
    throw new ApiError(422, "VALIDATION_ERROR", message);
  };
  runtimeConfigApi.status = async () => hookStatus;
  runtimeConfigApi.apply = async () => { submitted += 1; };
  try {
    assert.equal(renderHook().plan, recoveryPlan);
    assert.equal(await renderHook().review(request), null);
    const hook = renderHook();
    assert.equal(hook.plan, null);
    assert.equal(hook.state("engine").requestError, message);
    assert.equal(hook.reviewing, false);
    assert.match(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationStatus, { runtime: hook }))),
      /Server restart required for LOGGING.level/);
    assert.equal(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationReview, { runtime: hook }))), "");
    await hook.apply();
    assert.equal(submitted, 0);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("review returns the immutable plan identity used to associate a later completion", async () => {
  const hookStatus = { running: false, recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const reviewed = { ...recoveryPlan, target_revisions: { acquisition: "source-target" } };
  const request = { module: "acquisition", bindings: [], expected_revision: null };
  runtimeConfigApi.plan = async (body) => {
    assert.deepEqual(body, request);
    return reviewed;
  };
  try {
    assert.equal(await renderHook().review(request), reviewed);
    assert.equal(renderHook().plan, reviewed);
    assert.equal(renderHook().completedOperation, null);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a tracked success is retained when runtime status already describes another client's operation", async () => {
  const hookStatus = { running: false, recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "other-operation", plan_id: "other-plan", target_revisions: { acquisition: "other-target" }, status: "applying" };
  let own;
  const polled = [];
  runtimeConfigApi.apply = async (planId, operationId) => {
    own = { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "source-target" }, status: "succeeded" };
    return { ...own, status: "queued" };
  };
  runtimeConfigApi.operation = async (operationId) => {
    polled.push(operationId);
    assert.equal(operationId, own.operation_id);
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...hookStatus, operations: { acquisition: other } });
  try {
    await renderHook().apply();
    assert.deepEqual(polled, [own.operation_id]);
    assert.equal(renderHook().operation, other, "global progress still reports the latest operation");
    assert.equal(renderHook().completedOperation, own, "the editor can identify its own completed plan");
    other.status = "succeeded";
    await renderHook().refresh();
    assert.equal(renderHook().completedOperation, own, "another client's success cannot replace this client's completion");
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an immediately completed Apply records its own identity before global status refresh", async () => {
  const hookStatus = { running: false, recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "other-operation", plan_id: "other-plan", target_revisions: { acquisition: "other-target" }, status: "succeeded" };
  let own;
  runtimeConfigApi.apply = async (planId, operationId) => {
    own = { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "source-target" }, status: "succeeded" };
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...hookStatus, operations: { acquisition: other } });
  try {
    assert.equal(await renderHook().apply(), true);
    assert.equal(renderHook().operation, other);
    assert.equal(renderHook().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("confirming an interrupted Apply records the original operation success without resubmitting", async () => {
  const hookStatus = { running: false, recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "other-operation", plan_id: "other-plan", target_revisions: { acquisition: "other-target" }, status: "succeeded" };
  let own;
  let submitted = 0;
  let checked = 0;
  runtimeConfigApi.apply = async (planId, operationId) => {
    submitted += 1;
    own = { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "source-target" }, status: "succeeded" };
    throw new Error("Connection interrupted");
  };
  runtimeConfigApi.operation = async (operationId) => {
    assert.equal(operationId, own.operation_id);
    checked += 1;
    if (checked === 1) throw new ApiError(404, "NOT_FOUND", "Operation not visible yet");
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...hookStatus, operations: { acquisition: other } });
  try {
    assert.equal(await renderHook().apply(), false);
    assert.equal(renderHook().uncertain, true);
    assert.equal(renderHook().completedOperation, null);
    await renderHook().retryUnconfirmed("acquisition");
    assert.equal(submitted, 1);
    assert.equal(renderHook().uncertain, false);
    assert.equal(renderHook().operation, other);
    assert.equal(renderHook().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("only the successful reviewed revert marks its module for current-value synchronization", async () => {
  const original = { ...runtimeConfigApi };
  const hookStatus = { running: false, recovery: {}, operations: {}, accepted: { processing: "before", engine: "sys" } };
  const renderHook = hookRenderer(hookStatus);
  const target = { ...recoveryPlan, plan_id: "restore-plan", module: "processing", target_revisions: { processing: "restored" } };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "another-operation", plan_id: "another-plan", status: "succeeded", target_revisions: { acquisition: "other" } };
  runtimeConfigApi.apply = async (_plan, operationId) => ({ module: "processing", affected_modules: ["processing"], operation_id: operationId, plan_id: target.plan_id,
    status: "succeeded", target_revisions: target.target_revisions });
  runtimeConfigApi.status = async () => ({ ...hookStatus, operations: { acquisition: other } });
  try {
    renderHook().reviewPlan(target, "processing");
    assert.equal(renderHook().completedRestoration, null);
    assert.equal(await renderHook().apply(), true);
    const restoration = renderHook().completedRestoration;
    assert.equal(restoration.module, "processing"); assert.equal(restoration.revision, "restored");
    assert.notEqual(restoration.operationId, other.operation_id);
    await renderHook().refresh();
    assert.deepEqual(renderHook().completedRestoration, restoration);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("affected modules and engine recovery bound the configuration gate", () => {
  const hookStatus = { running: true, operations: { processing: { module: "processing", affected_modules: ["acquisition", "processing"], status: "applying" } }, recovery: {} };
  const runtime = hookRenderer(hookStatus)();
  assert.equal(runtime.state("acquisition").blocked, true);
  assert.equal(runtime.state("processing").blocked, true);
  assert.equal(runtime.state("stimulation").blocked, false);
  assert.equal(runtime.state("actuation").blocked, false);
  assert.equal(runtime.state("engine").blocked, true);
  hookStatus.operations = {}; hookStatus.recovery = { processing: "Unknown processing" };
  assert.equal(runtime.state("processing").blocked, true);
  assert.equal(runtime.state("stimulation").blocked, false);
  hookStatus.recovery.engine = "Unknown engine";
  assert.equal(runtime.state("stimulation").blocked, true);
});

const currentConfiguration = { expected_revision: "original", settings: { "ACQUISITION.buffer_size": 16 },
  bindings: [{ slot: "acquisition.source", selection: { package_id: "source", contribution_id: "main", version: "1.0.0" }, parameters: { gain: 2 } }] };
const completedAction = (body, module = "acquisition", overrides = {}) => ({ operation_id: body.operation_id, module,
  affected_modules: [module], status: "succeeded", target_revisions: { [module]: "applied" },
  result: { configuration_applied: true, action: "connected" }, ...overrides });

test("a current module action sends exactly one request and acknowledges only its own accepted configuration", async () => {
  const renderHook = hookRenderer({ recovery: {}, operations: {} });
  const original = { ...runtimeConfigApi };
  const submitted = [], applied = [];
  runtimeConfigApi.plan = async () => assert.fail("Module actions do not create a separate plan");
  runtimeConfigApi.apply = async () => assert.fail("Module actions do not send a separate apply");
  runtimeConfigApi.status = async () => assert.fail("Action admission does not use a check-then-act status read");
  try {
    const result = await renderHook().execute("acquisition", currentConfiguration, async (body) => {
      submitted.push(body); return completedAction(body);
    }, (operation) => applied.push(operation));
    assert.equal(submitted.length, 1);
    assert.deepEqual(submitted[0].configuration, currentConfiguration);
    assert.match(submitted[0].operation_id, /^[0-9a-f-]{36}$/);
    assert.deepEqual(applied, [result]);
    assert.equal(renderHook().state("acquisition").uncertain, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("same-render duplicate module actions are blocked while unrelated modules remain independent", async () => {
  const renderHook = hookRenderer({ recovery: {}, operations: {} });
  const hook = renderHook();
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const sent = [];
  const first = hook.execute("acquisition", currentConfiguration, async (body) => { sent.push(body); await pending; return completedAction(body); });
  await assert.rejects(hook.execute("acquisition", currentConfiguration, async () => assert.fail("Duplicate action")));
  const other = await hook.execute("actuation", { expected_revision: null, settings: {}, bindings: [] }, async (body) => {
    sent.push(body); return completedAction(body, "actuation");
  });
  assert.equal(other.status, "succeeded");
  assert.notEqual(sent[0].operation_id, sent[1].operation_id);
  finish(); await first;
  assert.equal(renderHook().state("acquisition").inProgress, false);
});

for (const accepted of [false, true]) test(`failed module startup acknowledges configuration only when accepted=${accepted}`, async () => {
  const renderHook = hookRenderer({ recovery: {}, operations: {} });
  const applied = [];
  const result = await renderHook().execute("acquisition", currentConfiguration, async (body) => completedAction(body, "acquisition", {
    status: "failed", error: "Connection rejected", result: { configuration_applied: accepted },
  }), (operation) => applied.push(operation));
  assert.equal(result.status, "failed");
  assert.equal(applied.length, accepted ? 1 : 0);
  assert.equal(renderHook().state("acquisition").blocked, false);
});

test("an interrupted action confirms its original operation once without resending or borrowing another client's revision", async () => {
  const hookStatus = { recovery: {}, operations: {} };
  const renderHook = hookRenderer(hookStatus);
  const original = { ...runtimeConfigApi };
  const applied = [], checked = [];
  let own, sent = 0;
  runtimeConfigApi.operation = async (id) => { checked.push(id); return own; };
  runtimeConfigApi.status = async () => ({ ...hookStatus, operations: { acquisition: { ...own, operation_id: "other-client", target_revisions: { acquisition: "unrelated" } } } });
  runtimeConfigApi.apply = async () => assert.fail("An action must not be repeated through Apply");
  try {
    await assert.rejects(renderHook().execute("acquisition", currentConfiguration, async (body) => {
      sent++; own = completedAction(body); throw new Error("HTTP reply lost");
    }, (operation) => applied.push(operation)), /HTTP reply lost/);
    assert.equal(renderHook().state("acquisition").uncertain, true);
    assert.deepEqual(applied, []);
    await renderHook().retryUnconfirmed("acquisition");
    await renderHook().refresh();
    assert.equal(sent, 1);
    assert.deepEqual(checked, [own.operation_id]);
    assert.deepEqual(applied, [own]);
    assert.equal(renderHook().state("acquisition").uncertain, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a rejected action preserves the server validation error without leaving an uncertain operation", async () => {
  const renderHook = hookRenderer({ recovery: {}, operations: {} });
  await assert.rejects(renderHook().execute("acquisition", currentConfiguration, async () => {
    throw new ApiError(409, "CONFLICT", "Current configuration changed");
  }, () => assert.fail("Rejected configuration cannot advance the baseline")), /Current configuration changed/);
  assert.equal(renderHook().state("acquisition").requestError, "Current configuration changed");
  assert.equal(renderHook().state("acquisition").uncertain, false);
});
