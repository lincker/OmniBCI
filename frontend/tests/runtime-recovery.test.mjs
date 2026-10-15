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
const history = { status: "recovery_required", operation_id: "failed-operation", error: "Unknown startup" };
const plan = { plan_id: "new-plan", errors: [] };

function hookRenderer(status) {
  const values = [status, history, plan, false, false, false, null, null];
  const refs = [];
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  return () => {
    let stateIndex = 0, refIndex = 0;
    const previous = dispatcher.current;
    dispatcher.current = {
      useContext: () => english,
      useState: () => {
        const index = stateIndex++;
        return [values[index], (value) => { values[index] = value; }];
      },
      useRef: (value) => refs[refIndex++] ??= { current: value },
      useCallback: (callback) => callback,
      useEffect: () => {},
    };
    try { return useRuntimeConfiguration(); }
    finally { dispatcher.current = previous; }
  };
}

test("a confirmed cleared quarantine permits Apply despite failed-operation history", async () => {
  const status = { running: false, recovery_required: false, operation: history };
  const hook = hookRenderer(status)();
  assert.equal(hook.recoveryRequired, false);
  assert.equal(hook.blocked, false);
  assert.equal(hook.operation, history);
  const original = { ...runtimeConfigApi };
  let submitted = 0;
  runtimeConfigApi.apply = async () => { submitted += 1; return { status: "succeeded" }; };
  runtimeConfigApi.operation = async () => ({ status: "succeeded" });
  runtimeConfigApi.status = async () => status;
  try { await hook.apply(); assert.equal(submitted, 1); }
  finally { Object.assign(runtimeConfigApi, original); }
});

test("connecting a source waits for configuration to finish before returning success", async () => {
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const calls = [];
  runtimeConfigApi.plan = async (body) => { calls.push(body); return plan; };
  runtimeConfigApi.apply = async () => ({ operation_id: "connect-operation", status: "queued" });
  runtimeConfigApi.operation = async () => { calls.push("poll"); return { status: "succeeded" }; };
  runtimeConfigApi.status = async () => status;
  const request = { bindings: [], refresh_slots: ["acquisition.source"], expected_revision: null };
  try {
    assert.equal(await render().applySelection(request), true);
    assert.deepEqual(calls[0], request);
    assert.ok(calls.includes("poll"));
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("a rejected connection configuration cannot report success", async () => {
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  let submitted = false;
  runtimeConfigApi.plan = async () => ({ ...plan, errors: ["Source unavailable"] });
  runtimeConfigApi.apply = async () => { submitted = true; };
  runtimeConfigApi.status = async () => status;
  try {
    assert.equal(await render().applySelection({ bindings: [] }), false);
    assert.equal(submitted, false);
    assert.equal(render().requestError, "Source unavailable");
  } finally { Object.assign(runtimeConfigApi, original); }
});

for (const [label, status] of [
  ["not ready but quarantined", { running: false, recovery_required: true, operation: history }],
  ["missing status", null],
  ["missing recovery acknowledgement", { running: false, operation: history }],
]) {
  test(`${label} remains blocked`, async () => {
    const hook = hookRenderer(status)();
    assert.equal(hook.blocked, true);
    const original = runtimeConfigApi.apply;
    let submitted = 0;
    runtimeConfigApi.apply = async () => { submitted += 1; return { status: "succeeded" }; };
    try { await hook.apply(); assert.equal(submitted, 0); }
    finally { runtimeConfigApi.apply = original; }
  });
}

test("a rejected host-settings review clears the previous plan and displays the backend error", async () => {
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const message = "Backend restart required for LOGGING.level";
  const request = { bindings: [], refresh_slots: [], include_host_settings: true };
  let submitted = 0;
  runtimeConfigApi.plan = async (body) => {
    assert.deepEqual(body, request);
    throw new ApiError(422, "VALIDATION_ERROR", message);
  };
  runtimeConfigApi.status = async () => status;
  runtimeConfigApi.apply = async () => { submitted += 1; };
  try {
    assert.equal(render().plan, plan);
    await render().review(request);
    const hook = render();
    assert.equal(hook.plan, null);
    assert.equal(hook.requestError, message);
    assert.equal(hook.reviewing, false);
    assert.match(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationStatus, { runtime: hook }))),
      /Backend restart required for LOGGING.level/);
    assert.equal(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(RuntimeConfigurationReview, { runtime: hook }))), "");
    await hook.apply();
    assert.equal(submitted, 0);
  } finally { Object.assign(runtimeConfigApi, original); }
});
