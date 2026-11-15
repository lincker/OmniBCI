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

const currentConfiguration = { expected_revision: "original", settings: { "ACQUISITION.buffer_size": 16 },
  bindings: [{ slot: "acquisition.source", selection: { package_id: "source", contribution_id: "main", version: "1.0.0" }, parameters: { gain: 2 } }] };
const completedAction = (body, module = "acquisition", overrides = {}) => ({ operation_id: body.operation_id, module,
  affected_modules: [module], status: "succeeded", target_revisions: { [module]: "applied" },
  result: { configuration_applied: true, action: "connected" }, ...overrides });

test("a current module action sends exactly one request and acknowledges only its own accepted configuration", async () => {
  const render = hookRenderer({ recovery: {}, operations: {} });
  const original = { ...runtimeConfigApi };
  const submitted = [], applied = [];
  runtimeConfigApi.plan = async () => assert.fail("Module actions do not create a separate plan");
  runtimeConfigApi.apply = async () => assert.fail("Module actions do not send a separate apply");
  runtimeConfigApi.status = async () => assert.fail("Action admission does not use a check-then-act status read");
  try {
    const result = await render().execute("acquisition", currentConfiguration, async (body) => {
      submitted.push(body); return completedAction(body);
    }, (operation) => applied.push(operation));
    assert.equal(submitted.length, 1);
    assert.deepEqual(submitted[0].configuration, currentConfiguration);
    assert.match(submitted[0].operation_id, /^[0-9a-f-]{36}$/);
    assert.deepEqual(applied, [result]);
    assert.equal(render().state("acquisition").uncertain, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("same-render duplicate module actions are blocked while unrelated modules remain independent", async () => {
  const render = hookRenderer({ recovery: {}, operations: {} });
  const hook = render();
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
  assert.equal(render().state("acquisition").inProgress, false);
});

for (const accepted of [false, true]) test(`failed module startup acknowledges configuration only when accepted=${accepted}`, async () => {
  const render = hookRenderer({ recovery: {}, operations: {} });
  const applied = [];
  const result = await render().execute("acquisition", currentConfiguration, async (body) => completedAction(body, "acquisition", {
    status: "failed", error: "Connection rejected", result: { configuration_applied: accepted },
  }), (operation) => applied.push(operation));
  assert.equal(result.status, "failed");
  assert.equal(applied.length, accepted ? 1 : 0);
  assert.equal(render().state("acquisition").blocked, false);
});

test("an interrupted action confirms its original operation once without resending or borrowing another client's revision", async () => {
  const status = { recovery: {}, operations: {} };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const applied = [], checked = [];
  let own, sent = 0;
  runtimeConfigApi.operation = async (id) => { checked.push(id); return own; };
  runtimeConfigApi.status = async () => ({ ...status, operations: { acquisition: { ...own, operation_id: "other-client", target_revisions: { acquisition: "unrelated" } } } });
  runtimeConfigApi.apply = async () => assert.fail("An action must not be repeated through Apply");
  try {
    await assert.rejects(render().execute("acquisition", currentConfiguration, async (body) => {
      sent++; own = completedAction(body); throw new Error("HTTP reply lost");
    }, (operation) => applied.push(operation)), /HTTP reply lost/);
    assert.equal(render().state("acquisition").uncertain, true);
    assert.deepEqual(applied, []);
    await render().retryUnconfirmed("acquisition");
    await render().refresh();
    assert.equal(sent, 1);
    assert.deepEqual(checked, [own.operation_id]);
    assert.deepEqual(applied, [own]);
    assert.equal(render().state("acquisition").uncertain, false);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a rejected action preserves the backend validation error without leaving an uncertain operation", async () => {
  const render = hookRenderer({ recovery: {}, operations: {} });
  await assert.rejects(render().execute("acquisition", currentConfiguration, async () => {
    throw new ApiError(409, "CONFLICT", "Current configuration changed");
  }, () => assert.fail("Rejected configuration cannot advance the baseline")), /Current configuration changed/);
  assert.equal(render().state("acquisition").requestError, "Current configuration changed");
  assert.equal(render().state("acquisition").uncertain, false);
});
