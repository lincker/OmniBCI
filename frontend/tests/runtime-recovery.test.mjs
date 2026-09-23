/** Exercise hook admission decisions with React's dispatcher isolated from the DOM. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { useRuntimeConfiguration } = await server.ssrLoadModule("/src/hooks/useRuntimeConfiguration.ts");
const { runtimeConfigApi } = await server.ssrLoadModule("/src/api/runtime-config.ts");
const history = { status: "recovery_required", operation_id: "failed-operation", error: "Unknown startup" };
const plan = { plan_id: "new-plan", errors: [] };

function hookWith(status) {
  const values = [status, history, plan, false, false, false, null, null];
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  dispatcher.current = {
    useState: () => [values.shift(), () => {}],
    useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback,
    useEffect: () => {},
  };
  try { return useRuntimeConfiguration(); }
  finally { dispatcher.current = previous; }
}

test("a confirmed cleared quarantine permits Apply despite failed-operation history", async () => {
  const status = { running: false, recovery_required: false, operation: history };
  const hook = hookWith(status);
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

for (const [label, status] of [
  ["not ready but quarantined", { running: false, recovery_required: true, operation: history }],
  ["missing status", null],
  ["missing recovery acknowledgement", { running: false, operation: history }],
]) {
  test(`${label} remains blocked`, async () => {
    const hook = hookWith(status);
    assert.equal(hook.blocked, true);
    const original = runtimeConfigApi.apply;
    let submitted = 0;
    runtimeConfigApi.apply = async () => { submitted += 1; return { status: "succeeded" }; };
    try { await hook.apply(); assert.equal(submitted, 0); }
    finally { runtimeConfigApi.apply = original; }
  });
}
