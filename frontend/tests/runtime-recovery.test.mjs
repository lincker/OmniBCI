/** Exercise hook admission decisions with React's dispatcher isolated from the DOM. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { I18nProvider, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const { useRuntimeConfiguration } = await server.ssrLoadModule("/src/hooks/useRuntimeConfiguration.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");
const { RuntimeConfigurationReview, RuntimeConfigurationStatus } = await server.ssrLoadModule("/src/RuntimeConfiguration.tsx");
const history = { status: "recovery_required", module: "acquisition", affected_modules: ["acquisition"], operation_id: "failed-operation", error: "Unknown startup" };
const plan = { expected_revisions: { system: "sys" }, module: "acquisition", plan_id: "new-plan", target_revisions: { acquisition: "new-revision" }, errors: [] };

function hookRenderer(status) {
  const values = [status, plan, {}, null, {}];
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
  const status = { running: false, recovery: {}, operations: { acquisition: history } };
  const hook = hookRenderer(status)();
  assert.equal(hook.recoveryRequired, false);
  assert.equal(hook.blocked, false);
  assert.equal(hook.operation, history);
  const original = { ...runtimeConfigApi };
  let submitted = 0;
  runtimeConfigApi.apply = async () => { submitted += 1; return { module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" }; };
  runtimeConfigApi.operation = async () => ({ module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" });
  runtimeConfigApi.status = async () => status;
  try { await hook.apply(); assert.equal(submitted, 1); }
  finally { Object.assign(runtimeConfigApi, original); }
});

test("connecting a source waits for configuration to finish before returning success", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const calls = [];
  runtimeConfigApi.plan = async (body) => { calls.push(body); return plan; };
  runtimeConfigApi.apply = async () => ({ module: "acquisition", affected_modules: ["acquisition"], operation_id: "connect-operation", status: "queued" });
  runtimeConfigApi.operation = async () => { calls.push("poll"); return { module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" }; };
  runtimeConfigApi.status = async () => status;
  const request = { module: "acquisition", bindings: [], refresh_slots: ["acquisition.source"], expected_revision: null };
  try {
    assert.deepEqual(await render().applyCurrentSelection(request), { ...plan.expected_revisions, ...plan.target_revisions });
    assert.deepEqual(calls[0], request);
    assert.ok(calls.includes("poll"));
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a rejected connection configuration cannot report success", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  let submitted = false;
  runtimeConfigApi.plan = async () => ({ ...plan, errors: ["Source unavailable"] });
  runtimeConfigApi.apply = async () => { submitted = true; };
  runtimeConfigApi.status = async () => status;
  try {
    assert.equal(await render().applyCurrentSelection({ module: "acquisition", bindings: [] }), null);
    assert.equal(submitted, false);
    assert.equal(render().requestError, "Source unavailable");
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("applying current parameters returns its own revision even when another client changes runtime status", async () => {
  const status = { running: false, recovery: {}, accepted: { acquisition: "before", system: "sys" }, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const target = { ...plan, target_revisions: { acquisition: "current-parameters" } };
  const request = { module: "acquisition", expected_revision: "before", bindings: [{
    slot: "acquisition.source", selection: { package_id: "example.source", contribution_id: "main", version: "1.0.0" },
    parameters: { port: 4000 },
  }] };
  let submitted = 0;
  runtimeConfigApi.plan = async (body) => { assert.deepEqual(body, request); return target; };
  runtimeConfigApi.apply = async (planId, operationId) => {
    submitted += 1;
    assert.equal(planId, target.plan_id);
    return { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: target.target_revisions.acquisition }, status: "succeeded" };
  };
  runtimeConfigApi.status = async () => ({ ...status, accepted: { acquisition: "another-client", system: "sys" } });
  try {
    assert.deepEqual(await render().applyCurrentSelection(request), { ...target.expected_revisions, ...target.target_revisions });
    assert.equal(submitted, 1);
    assert.equal(render().status.accepted.acquisition, "another-client");
    assert.equal(render().completedOperation.target_revisions.acquisition, target.target_revisions.acquisition);
  } finally { Object.assign(runtimeConfigApi, original); }
});

for (const result of ["failed", "recovery_required"]) test(`a ${result} application returns no current revision`, async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  runtimeConfigApi.plan = async () => ({ ...plan, target_revisions: { acquisition: "current-parameters" } });
  runtimeConfigApi.apply = async (planId, operationId) => ({
    module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "current-parameters" }, status: result,
  });
  runtimeConfigApi.status = async () => status;
  try {
    assert.equal(await render().applyCurrentSelection({ module: "acquisition", bindings: [] }), null);
    assert.equal(render().completedOperation, null);
    assert.equal(render().reviewing, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an invalid current configuration never submits an apply operation", async () => {
  const render = hookRenderer({ running: false, recovery: {}, operations: {} });
  const original = { ...runtimeConfigApi };
  runtimeConfigApi.plan = async () => ({ ...plan, target_revisions: { acquisition: "invalid" }, errors: ["Invalid source address"] });
  runtimeConfigApi.apply = async () => assert.fail("Invalid parameters must not be applied");
  try {
    assert.equal(await render().applyCurrentSelection({ module: "acquisition", bindings: [] }), null);
    assert.equal(render().requestError, "Invalid source address");
    assert.equal(render().reviewing, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an interrupted current application returns no revision while its result is uncertain", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  runtimeConfigApi.plan = async () => ({ ...plan, target_revisions: { acquisition: "current-parameters" } });
  runtimeConfigApi.apply = async () => { throw new Error("Connection interrupted"); };
  runtimeConfigApi.operation = async () => { throw new ApiError(404, "NOT_FOUND", "Operation not visible yet"); };
  runtimeConfigApi.status = async () => status;
  try {
    assert.equal(await render().applyCurrentSelection({ module: "acquisition", bindings: [] }), null);
    assert.equal(render().uncertain, true);
    assert.equal(render().completedOperation, null);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a stale runtime plan returns no revision and a fresh request can apply current parameters", async () => {
  const status = { running: false, recovery: {}, accepted: { acquisition: "another-client", system: "sys" }, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const planned = [];
  let submitted = 0;
  runtimeConfigApi.plan = async (body) => {
    planned.push(body);
    return { ...plan, plan_id: `plan-${planned.length}`, target_revisions: { acquisition: `target-${planned.length}` } };
  };
  runtimeConfigApi.apply = async (planId, operationId) => {
    submitted += 1;
    if (submitted === 1) throw new ApiError(409, "REVISION_CONFLICT", "Runtime configuration changed");
    return { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "target-2" }, status: "succeeded" };
  };
  runtimeConfigApi.status = async () => status;
  try {
    assert.equal(await render().applyCurrentSelection({ module: "acquisition", bindings: [], expected_revision: "before" }), null);
    assert.equal(render().uncertain, false);
    assert.equal(render().plan, null);
    assert.equal(render().completedOperation, null);
    assert.match(render().requestError, /Runtime configuration changed/);
    assert.deepEqual(await render().applyCurrentSelection({ module: "acquisition", bindings: [], expected_revision: status.accepted.acquisition }), { acquisition: "target-2", system: "sys" });
    assert.deepEqual(planned.map((request) => request.expected_revision), ["before", "another-client"]);
    assert.equal(submitted, 2);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("same-render current configuration requests share one planning and application lock", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const calls = [];
  let finishPlanning, finishApplying;
  runtimeConfigApi.plan = async (body) => {
    calls.push(["plan", body]);
    await new Promise((resolve) => { finishPlanning = resolve; });
    return { ...plan, target_revisions: { acquisition: "current-parameters" } };
  };
  runtimeConfigApi.apply = async (planId, operationId) => {
    calls.push(["apply", planId]);
    await new Promise((resolve) => { finishApplying = resolve; });
    return { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "current-parameters" }, status: "succeeded" };
  };
  runtimeConfigApi.status = async () => status;
  const request = { module: "acquisition", bindings: [] };
  try {
    const hook = render();
    const first = hook.applyCurrentSelection(request);
    assert.equal(await hook.applyCurrentSelection(request), null);
    assert.deepEqual(calls, [["plan", request]]);
    finishPlanning();
    for (let index = 0; index < 5; index += 1) await Promise.resolve();
    assert.equal(await hook.applyCurrentSelection(request), null);
    assert.deepEqual(calls, [["plan", request], ["apply", plan.plan_id]]);
    finishApplying();
    assert.deepEqual(await first, { acquisition: "current-parameters", system: "sys" });
    assert.equal(render().reviewing, false);
    assert.equal(render().submitting, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an outstanding review blocks automatic application and stale Apply callbacks", async () => {
  const render = hookRenderer({ running: false, recovery: {}, operations: {} });
  const original = { ...runtimeConfigApi };
  let finishPlanning, planned = 0;
  runtimeConfigApi.plan = async () => {
    planned += 1;
    await new Promise((resolve) => { finishPlanning = resolve; });
    return { ...plan, target_revisions: { acquisition: "reviewed" } };
  };
  runtimeConfigApi.apply = async () => assert.fail("Do not apply a stale plan during review");
  try {
    const hook = render();
    const review = hook.review({ module: "acquisition", bindings: [] });
    assert.equal(await hook.applyCurrentSelection({ module: "acquisition", bindings: [] }), null);
    assert.equal(await hook.apply(), false);
    assert.equal(planned, 1);
    finishPlanning();
    assert.equal((await review).target_revisions.acquisition, "reviewed");
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("automatic planning blocks manual review and stale Apply callbacks", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  let finishPlanning, planned = 0, submitted = 0;
  runtimeConfigApi.plan = async () => {
    planned += 1;
    await new Promise((resolve) => { finishPlanning = resolve; });
    return { ...plan, target_revisions: { acquisition: "current-parameters" } };
  };
  runtimeConfigApi.apply = async (planId, operationId) => {
    submitted += 1;
    return { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "current-parameters" }, status: "succeeded" };
  };
  runtimeConfigApi.status = async () => status;
  try {
    const hook = render();
    const applying = hook.applyCurrentSelection({ module: "acquisition", bindings: [] });
    assert.equal(await hook.review({ module: "acquisition", bindings: [] }), null);
    assert.equal(await hook.apply(), false);
    assert.equal(planned, 1);
    assert.equal(submitted, 0);
    finishPlanning();
    assert.deepEqual(await applying, { acquisition: "current-parameters", system: "sys" });
    assert.equal(submitted, 1);
  } finally { Object.assign(runtimeConfigApi, original); }
});

for (const [label, status] of [
  ["not ready but quarantined", { running: false, recovery: { system: "Unknown runtime" }, operations: { acquisition: history } }],
  ["missing status", null],
  ["missing recovery acknowledgement", { running: false, operations: { acquisition: history } }],
]) {
  test(`${label} remains blocked`, async () => {
    const hook = hookRenderer(status)();
    assert.equal(hook.blocked, true);
    const original = runtimeConfigApi.apply;
    let submitted = 0;
    runtimeConfigApi.apply = async () => { submitted += 1; return { module: "acquisition", affected_modules: ["acquisition"], status: "succeeded" }; };
    try { await hook.apply(); assert.equal(submitted, 0); }
    finally { runtimeConfigApi.apply = original; }
  });
}

test("a rejected host-settings review clears the previous plan and displays the backend error", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const message = "Backend restart required for LOGGING.level";
  const request = { module: "system", bindings: [], refresh_slots: [] };
  let submitted = 0;
  runtimeConfigApi.plan = async (body) => {
    assert.deepEqual(body, request);
    throw new ApiError(422, "VALIDATION_ERROR", message);
  };
  runtimeConfigApi.status = async () => status;
  runtimeConfigApi.apply = async () => { submitted += 1; };
  try {
    assert.equal(render().plan, plan);
    assert.equal(await render().review(request), null);
    const hook = render();
    assert.equal(hook.plan, null);
    assert.equal(hook.state("system").requestError, message);
    assert.equal(hook.reviewing, false);
    assert.match(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationStatus, { runtime: hook }))),
      /Backend restart required for LOGGING.level/);
    assert.equal(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationReview, { runtime: hook }))), "");
    await hook.apply();
    assert.equal(submitted, 0);
  } finally { Object.assign(runtimeConfigApi, original); }
});


test("review returns the immutable plan identity used to associate a later completion", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const reviewed = { ...plan, target_revisions: { acquisition: "source-target" } };
  const request = { module: "acquisition", bindings: [], expected_revision: null };
  runtimeConfigApi.plan = async (body) => {
    assert.deepEqual(body, request);
    return reviewed;
  };
  try {
    assert.equal(await render().review(request), reviewed);
    assert.equal(render().plan, reviewed);
    assert.equal(render().completedOperation, null);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a tracked success is retained when runtime status already describes another client's operation", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
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
  runtimeConfigApi.status = async () => ({ ...status, operations: { acquisition: other } });
  try {
    await render().apply();
    assert.deepEqual(polled, [own.operation_id]);
    assert.equal(render().operation, other, "global progress still reports the latest operation");
    assert.equal(render().completedOperation, own, "the editor can identify its own completed plan");
    other.status = "succeeded";
    await render().refresh();
    assert.equal(render().completedOperation, own, "another client's success cannot replace this client's completion");
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an immediately completed Apply records its own identity before global status refresh", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "other-operation", plan_id: "other-plan", target_revisions: { acquisition: "other-target" }, status: "succeeded" };
  let own;
  runtimeConfigApi.apply = async (planId, operationId) => {
    own = { module: "acquisition", affected_modules: ["acquisition"], operation_id: operationId, plan_id: planId, target_revisions: { acquisition: "source-target" }, status: "succeeded" };
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...status, operations: { acquisition: other } });
  try {
    assert.equal(await render().apply(), true);
    assert.equal(render().operation, other);
    assert.equal(render().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("confirming an interrupted Apply records the original operation success without resubmitting", async () => {
  const status = { running: false, recovery: {}, operations: {} };
  const render = hookRenderer(status);
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
  runtimeConfigApi.status = async () => ({ ...status, operations: { acquisition: other } });
  try {
    assert.equal(await render().apply(), false);
    assert.equal(render().uncertain, true);
    assert.equal(render().completedOperation, null);
    await render().retryUnconfirmed("acquisition");
    assert.equal(submitted, 1);
    assert.equal(render().uncertain, false);
    assert.equal(render().operation, other);
    assert.equal(render().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});


test("only the successful reviewed revert marks its module for current-value synchronization", async () => {
  const original = { ...runtimeConfigApi };
  const status = { running: false, recovery: {}, operations: {}, accepted: { processing: "before", system: "sys" } };
  const render = hookRenderer(status);
  const target = { ...plan, plan_id: "restore-plan", module: "processing", target_revisions: { processing: "restored" } };
  const other = { module: "acquisition", affected_modules: ["acquisition"], operation_id: "another-operation", plan_id: "another-plan", status: "succeeded", target_revisions: { acquisition: "other" } };
  runtimeConfigApi.apply = async (_plan, operationId) => ({ module: "processing", affected_modules: ["processing"], operation_id: operationId, plan_id: target.plan_id,
    status: "succeeded", target_revisions: target.target_revisions });
  runtimeConfigApi.status = async () => ({ ...status, operations: { acquisition: other } });
  try {
    render().reviewPlan(target, "processing");
    assert.equal(render().completedRestoration, null);
    assert.equal(await render().apply(), true);
    const restoration = render().completedRestoration;
    assert.equal(restoration.module, "processing"); assert.equal(restoration.revision, "restored");
    assert.notEqual(restoration.operationId, other.operation_id);
    await render().refresh();
    assert.deepEqual(render().completedRestoration, restoration);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("unrelated module applications retain independent operation IDs and completion records", async () => {
  const original = { ...runtimeConfigApi };
  const status = { running: true, accepted: {}, applied: {}, generation: "engine", operations: {}, recovery: {} };
  const render = hookRenderer(status);
  const pending = new Map(), operations = new Map();
  runtimeConfigApi.status = async () => status;
  runtimeConfigApi.plan = async ({ module }) => ({ ...plan, module, plan_id: module, target_revisions: { [module]: `${module}-new` } });
  runtimeConfigApi.apply = async (module, operationId) => {
    operations.set(module, { module, affected_modules: [module], operation_id: operationId, plan_id: module,
      target_revisions: { [module]: `${module}-new` }, status: "succeeded" });
    await new Promise((resolve) => pending.set(module, resolve));
    return operations.get(module);
  };
  try {
    const processing = render().applyCurrentSelection({ module: "processing", bindings: [] });
    const stimulation = render().applyCurrentSelection({ module: "stimulation", bindings: [] });
    for (let i = 0; i < 10; i++) await Promise.resolve();
    assert.equal(pending.size, 2);
    assert.equal(render().state("processing").submitting, true);
    assert.equal(render().state("stimulation").submitting, true);
    assert.equal(render().state("actuation").blocked, false);
    assert.notEqual(operations.get("processing").operation_id, operations.get("stimulation").operation_id);
    pending.get("stimulation")();
    assert.deepEqual(await stimulation, { system: "sys", stimulation: "stimulation-new" });
    assert.equal(render().state("processing").submitting, true);
    pending.get("processing")();
    assert.deepEqual(await processing, { system: "sys", processing: "processing-new" });
    assert.equal(render().state("stimulation").completedOperation, operations.get("stimulation"));
    assert.equal(render().state("processing").completedOperation, operations.get("processing"));
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("an uncertain module retries its own ID after another module has succeeded", async () => {
  const original = { ...runtimeConfigApi };
  const status = { running: true, accepted: {}, applied: {}, generation: "engine", operations: {}, recovery: {} };
  const render = hookRenderer(status);
  const submitted = [], completed = {};
  let visible = false;
  runtimeConfigApi.status = async () => status;
  runtimeConfigApi.plan = async ({ module }) => ({ ...plan, module, plan_id: module, target_revisions: { [module]: "new" } });
  runtimeConfigApi.apply = async (module, operationId) => {
    submitted.push([module, operationId]);
    completed[module] = { module, affected_modules: [module], operation_id: operationId, plan_id: module,
      target_revisions: { [module]: "new" }, status: "succeeded" };
    if (module === "processing") throw new Error("Response interrupted");
    return completed[module];
  };
  runtimeConfigApi.operation = async (id) => {
    assert.equal(id, completed.processing.operation_id);
    if (!visible) throw new ApiError(404, "NOT_FOUND", "Not visible");
    return completed.processing;
  };
  try {
    assert.equal(await render().applyCurrentSelection({ module: "processing", bindings: [] }), null);
    assert.equal(render().state("processing").uncertain, true);
    assert.equal(render().state("stimulation").blocked, false);
    assert.ok(await render().applyCurrentSelection({ module: "stimulation", bindings: [] }));
    visible = true;
    await render().retryUnconfirmed("processing");
    assert.equal(submitted.length, 2, "Confirmation never resubmits a completed operation");
    assert.equal(render().state("processing").completedOperation, completed.processing);
    assert.equal(render().state("stimulation").completedOperation, completed.stimulation);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("affected modules and system recovery bound the configuration gate", () => {
  const status = { running: true, operations: { processing: { module: "processing", affected_modules: ["acquisition", "processing"], status: "applying" } }, recovery: {} };
  const runtime = hookRenderer(status)();
  assert.equal(runtime.state("acquisition").blocked, true);
  assert.equal(runtime.state("processing").blocked, true);
  assert.equal(runtime.state("stimulation").blocked, false);
  assert.equal(runtime.state("actuation").blocked, false);
  assert.equal(runtime.state("system").blocked, true);
  status.operations = {}; status.recovery = { processing: "Unknown processing" };
  assert.equal(runtime.state("processing").blocked, true);
  assert.equal(runtime.state("stimulation").blocked, false);
  status.recovery.system = "Unknown engine";
  assert.equal(runtime.state("stimulation").blocked, true);
});
