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
      useState: (initial) => {
        const index = stateIndex++;
        if (index >= values.length) values[index] = typeof initial === "function" ? initial() : initial;
        return [values[index], (value) => { values[index] = typeof value === "function" ? value(values[index]) : value; }];
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
    assert.equal(await render().review(request), null);
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


test("review returns the immutable plan identity used to associate a later completion", async () => {
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const reviewed = { ...plan, target_revision: "source-target" };
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
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const other = { operation_id: "other-operation", plan_id: "other-plan", target_revision: "other-target", status: "applying" };
  let own;
  const polled = [];
  runtimeConfigApi.apply = async (planId, operationId) => {
    own = { operation_id: operationId, plan_id: planId, target_revision: "source-target", status: "succeeded" };
    return { ...own, status: "queued" };
  };
  runtimeConfigApi.operation = async (operationId) => {
    polled.push(operationId);
    assert.equal(operationId, own.operation_id);
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...status, operation: other });
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
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const other = { operation_id: "other-operation", plan_id: "other-plan", target_revision: "other-target", status: "succeeded" };
  let own;
  runtimeConfigApi.apply = async (planId, operationId) => {
    own = { operation_id: operationId, plan_id: planId, target_revision: "source-target", status: "succeeded" };
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...status, operation: other });
  try {
    assert.equal(await render().apply(), true);
    assert.equal(render().operation, other);
    assert.equal(render().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});

test("confirming an interrupted Apply records the original operation success without resubmitting", async () => {
  const status = { running: false, recovery_required: false, operation: null };
  const render = hookRenderer(status);
  const original = { ...runtimeConfigApi };
  const other = { operation_id: "other-operation", plan_id: "other-plan", target_revision: "other-target", status: "succeeded" };
  let own;
  let submitted = 0;
  let checked = 0;
  runtimeConfigApi.apply = async (planId, operationId) => {
    submitted += 1;
    own = { operation_id: operationId, plan_id: planId, target_revision: "source-target", status: "succeeded" };
    throw new Error("Connection interrupted");
  };
  runtimeConfigApi.operation = async (operationId) => {
    assert.equal(operationId, own.operation_id);
    checked += 1;
    if (checked === 1) throw new ApiError(404, "NOT_FOUND", "Operation not visible yet");
    return own;
  };
  runtimeConfigApi.status = async () => ({ ...status, operation: other });
  try {
    assert.equal(await render().apply(), false);
    assert.equal(render().uncertain, true);
    assert.equal(render().completedOperation, null);
    await render().retryUnconfirmed();
    assert.equal(submitted, 1);
    assert.equal(render().uncertain, false);
    assert.equal(render().operation, other);
    assert.equal(render().completedOperation, own);
  } finally { Object.assign(runtimeConfigApi, original); }
});

const { pluginConfigApi: profileApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const profileSelection = { package_id: "example.source", contribution_id: "reader", version: "1.0.0" };
const savedSource = { profile_id: "source-a", kind: "acquisition", name: "Stable source", selection: profileSelection,
  parameters: { address: "device-a", port: 4000 }, revision: 3, configuration_version: 1 };

async function withProfileWorkflow(action, options = {}) {
  const originalRuntime = { ...runtimeConfigApi };
  const originalProfiles = { ...profileApi };
  const state = { running: false, recovery_required: false, accepted_revision: "old-revision",
    applied_revision: null, generation: null, operation: null };
  const render = hookRenderer(state);
  const records = [structuredClone(savedSource)];
  const calls = { plans: [], creates: [], updates: [], applies: [], lists: 0 };
  runtimeConfigApi.status = async () => state;
  runtimeConfigApi.plan = async (body) => {
    calls.plans.push(structuredClone(body));
    return { plan_id: `profile-plan-${calls.plans.length}`, errors: [], expected_revision: body.expected_revision,
      target_revision: options.replannedRevision && calls.plans.length > 1 ? options.replannedRevision : "new-revision",
      running: false, applied_revision: null, generation: null,
      strategy: options.replannedStrategy && calls.plans.length > 1 ? options.replannedStrategy : "select_on_start",
      changed_slots: ["acquisition.source"], changed_settings: [], module: "acquisition" };
  };
  runtimeConfigApi.apply = async (planId, operationId) => {
    calls.applies.push({ planId, operationId });
    const result = { operation_id: operationId, plan_id: planId, target_revision: "new-revision", status: "succeeded" };
    state.operation = result;
    if (options.applyError) throw options.applyError;
    state.accepted_revision = result.target_revision;
    return result;
  };
  runtimeConfigApi.operation = async () => {
    if (options.operationError) throw options.operationError;
    return state.operation;
  };
  profileApi.profiles = async () => {
    calls.lists += 1;
    if (options.listUnavailableAfterSave && calls.lists > 1) throw new Error("Profile list unavailable");
    return structuredClone(records);
  };
  profileApi.create = async (kind, selection, name, parameters) => {
    calls.creates.push(structuredClone({ kind, selection, name, parameters }));
    if (options.createError) throw options.createError;
    const saved = { profile_id: `new-source-${calls.creates.length}`, kind, selection: structuredClone(selection),
      name, parameters: structuredClone(parameters), revision: 1, configuration_version: 1 };
    records.push(saved);
    if (options.createResponseError) throw options.createResponseError;
    return structuredClone(saved);
  };
  profileApi.update = async (profile, name, parameters) => {
    calls.updates.push(structuredClone({ profile, name, parameters }));
    if (options.updateError) throw options.updateError;
    const saved = { ...profile, name, parameters: structuredClone(parameters), revision: profile.revision + 1 };
    records.splice(records.findIndex((item) => item.profile_id === profile.profile_id), 1, saved);
    return structuredClone(saved);
  };
  const draft = (profile = null, parameters = { address: "device-b", port: 4000 }) => ({ kind: "acquisition",
    selection: structuredClone(profileSelection), parameters, profile: profile && structuredClone(profile), name: profile?.name ?? "New source" });
  const review = async (input = draft()) => {
    const bindings = input.profile && JSON.stringify(input.parameters) === JSON.stringify(input.profile.parameters)
      ? [{ slot: "acquisition.source", profile_id: input.profile.profile_id }]
      : [{ slot: "acquisition.source", selection: input.selection, parameters: input.parameters }];
    return render().review({ module: "acquisition", bindings, refresh_slots: ["acquisition.source"],
      expected_revision: "old-revision", include_host_settings: false }, input);
  };
  try { await action({ render, calls, records, state, draft, review, options }); }
  finally { Object.assign(runtimeConfigApi, originalRuntime); Object.assign(profileApi, originalProfiles); }
}

test("reviewing and cancelling a first source never creates a profile or applies configuration", async () => {
  await withProfileWorkflow(async ({ render, calls, review }) => {
    await review();
    assert.equal(render().profileReview.mode, "create");
    assert.ok(render().profileReview.name.trim(), "The final Review starts with an editable generated name");
    assert.equal(calls.creates.length, 0);
    assert.equal(calls.applies.length, 0);
    render().dismissPlan();
    assert.equal(render().plan, null);
    assert.equal(render().profileReview, null);
    assert.equal(calls.creates.length, 0);
  });
});

test("confirming the first source saves a profile then applies its reviewed binding", async () => {
  await withProfileWorkflow(async ({ render, calls, records, review }) => {
    const reviewed = await review();
    render().setProfileReviewName("Lab device");
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.creates[0].name, "Lab device");
    assert.equal(calls.updates.length, 0);
    assert.deepEqual(calls.plans[1].bindings, [{ slot: "acquisition.source", profile_id: "new-source-1" }]);
    assert.equal(calls.applies.length, 1);
    assert.equal(calls.applies[0].planId, "profile-plan-2");
    assert.equal(render().savedProfile.reviewed_plan_id, reviewed.plan_id);
    assert.equal(render().savedProfile.profile.profile_id, "new-source-1");
    assert.equal(render().completedOperation.reviewed_plan_id, reviewed.plan_id);
    assert.equal(render().completedOperation.profile.profile_id, "new-source-1");
    assert.deepEqual(records[0], savedSource);
  });
});

test("an unchanged existing source applies without creating or revising any profile", async () => {
  await withProfileWorkflow(async ({ render, calls, draft, review }) => {
    await review(draft(savedSource, structuredClone(savedSource.parameters)));
    assert.equal(render().profileReview.mode, "existing");
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 0);
    assert.equal(calls.updates.length, 0);
    assert.equal(calls.applies.length, 1);
  });
});

test("changed saved source parameters default to a new profile and retain the original", async () => {
  await withProfileWorkflow(async ({ render, calls, records, draft, review }) => {
    await review(draft(savedSource));
    assert.equal(render().profileReview.mode, "create");
    assert.notEqual(render().profileReview.name, savedSource.name);
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.updates.length, 0);
    assert.deepEqual(records[0], savedSource);
    assert.equal(records[1].parameters.address, "device-b");
  });
});

test("explicit update preserves the selected profile identity and applies its new revision", async () => {
  await withProfileWorkflow(async ({ render, calls, records, draft, review }) => {
    await review(draft(savedSource));
    render().setProfileReviewMode("update");
    assert.equal(render().profileReview.mode, "update");
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 0);
    assert.equal(calls.updates.length, 1);
    assert.equal(calls.updates[0].profile.revision, 3, "Update retains the optimistic revision guard");
    assert.equal(records[0].revision, 4);
    assert.equal(records[0].parameters.address, "device-b");
    assert.deepEqual(calls.plans[1].bindings, [{ slot: "acquisition.source", profile_id: "source-a" }]);
  });
});

test("a rejected profile save leaves the review open and never applies an unsaved profile", async () => {
  await withProfileWorkflow(async ({ render, calls, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    assert.equal(calls.applies.length, 0);
    assert.ok(render().plan);
    assert.match(render().requestError, /Profile name rejected/);
    assert.equal(render().submitting, false);
  }, { createError: new ApiError(422, "VALIDATION_ERROR", "Profile name rejected") });
});

test("updating a concurrently edited profile requires fresh review and cannot apply stale values", async () => {
  await withProfileWorkflow(async ({ render, calls, records, draft, review }) => {
    await review(draft(savedSource));
    render().setProfileReviewMode("update");
    assert.equal(await render().apply(), false);
    assert.equal(calls.updates.length, 1);
    assert.equal(calls.applies.length, 0);
    assert.deepEqual(records[0], savedSource);
    assert.match(render().requestError, /profile was changed elsewhere/);
  }, { updateError: new ApiError(409, "CONFLICT", "Profile changed elsewhere") });
});

test("changed runtime impact after saving requires another confirmation and reuses the saved profile", async () => {
  await withProfileWorkflow(async ({ render, calls, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 0);
    assert.equal(render().plan.strategy, "rebuild");
    assert.ok(render().profileReview.savedProfile);
    assert.ok(render().profileSaveNotice, "The partial save is disclosed while application is unconfirmed");
    await render().apply();
    assert.equal(calls.creates.length, 1, "Confirming the refreshed plan must not create a second profile");
  }, { replannedStrategy: "rebuild" });
});

test("an ambiguous successful profile create is reconciled before applying without duplication", async () => {
  await withProfileWorkflow(async ({ render, calls, records, review }) => {
    await review();
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 1);
    assert.equal(records.length, 2);
    assert.equal(calls.applies.length, 1);
    assert.equal(render().savedProfile.profile.profile_id, "new-source-1");
  }, { createResponseError: new Error("Profile response interrupted") });
});

test("a failed Apply retains the saved profile and reports that saving did not apply it", async () => {
  await withProfileWorkflow(async ({ render, calls, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    assert.equal(calls.creates.length, 1);
    assert.ok(render().savedProfile);
    assert.ok(render().profileSaveNotice);
    assert.match(render().requestError, /Configuration stale/);
    assert.equal(render().completedOperation, null);
  }, { applyError: new ApiError(409, "CONFLICT", "Configuration stale") });
});


test("a changed target after saving closes Review so unreviewed profile values cannot be applied", async () => {
  await withProfileWorkflow(async ({ render, calls, draft, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    assert.equal(render().plan, null);
    assert.ok(render().savedProfile);
    assert.equal(calls.applies.length, 0);
    const saved = render().savedProfile.profile;
    await review(draft(saved, saved.parameters));
    assert.equal(render().profileReview.mode, "existing");
    await render().apply();
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 1);
  }, { replannedRevision: "different-target" });
});

test("a runtime revision changed since Review prevents the profile write", async () => {
  await withProfileWorkflow(async ({ render, calls, state, review }) => {
    await review();
    state.accepted_revision = "another-client";
    assert.equal(await render().apply(), false);
    assert.equal(calls.creates.length, 0);
    assert.equal(calls.updates.length, 0);
    assert.equal(calls.applies.length, 0);
  });
});

test("rapid confirmations cannot create two profiles from the same Review", async () => {
  await withProfileWorkflow(async ({ render, calls, review }) => {
    await review();
    const runtime = render();
    await Promise.all([runtime.apply(), runtime.apply()]);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 1);
  });
});

test("an interrupted save checks the existing write before retrying and never repeats the POST", async () => {
  await withProfileWorkflow(async ({ render, calls, options, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    assert.equal(render().profileReview.saveUncertain, true);
    assert.equal(render().blocked, true);
    await render().apply();
    await render().retryProfileSave();
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 0);
    options.listUnavailableAfterSave = false;
    await render().retryProfileSave();
    assert.equal(render().profileReview.saveUncertain, false);
    assert.equal(render().profileReview.savedProfile.profile_id, "new-source-1");
    assert.equal(calls.applies.length, 0, "Checking a save does not authorize Apply");
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 1);
  }, { createResponseError: new Error("Profile response interrupted"), listUnavailableAfterSave: true });
});

test("returning to editing after an unresolved save never writes or deletes a profile", async () => {
  await withProfileWorkflow(async ({ render, calls, records, options, review }) => {
    await review();
    await render().apply();
    assert.equal(render().profileReview.saveUncertain, true);
    render().abandonProfileSave();
    assert.equal(render().plan, null);
    assert.equal(render().profileReview, null);
    assert.equal(render().blocked, false);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 0);
    assert.equal(records.length, 2, "The escape must not remove a possibly saved profile");
    options.listUnavailableAfterSave = false;
    await review();
    assert.ok(render().plan);
    assert.equal(calls.creates.length, 1, "Fresh review still waits for explicit final confirmation");
  }, { createResponseError: new Error("Profile response interrupted"), listUnavailableAfterSave: true });
});

test("a saved profile is reused after Apply fails and the user reviews the same draft again", async () => {
  await withProfileWorkflow(async ({ render, calls, draft, options, review }) => {
    await review();
    assert.equal(await render().apply(), false);
    const saved = render().savedProfile.profile;
    assert.equal(render().savedProfile.applied, false);
    options.applyError = null;
    await review(draft(saved, saved.parameters));
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.updates.length, 0);
    assert.equal(calls.applies.length, 2);
    assert.equal(render().completedOperation.profile.profile_id, saved.profile_id);
  }, { applyError: new ApiError(409, "CONFLICT", "Configuration stale") });
});

test("a lost Apply response recovers its original saved-profile association without another create", async () => {
  await withProfileWorkflow(async ({ render, calls, options, review }) => {
    const reviewed = await review();
    assert.equal(await render().apply(), false);
    assert.equal(render().uncertain, true);
    options.operationError = null;
    await render().retryUnconfirmed();
    assert.equal(render().uncertain, false);
    assert.equal(render().completedOperation.reviewed_plan_id, reviewed.plan_id);
    assert.equal(render().completedOperation.profile.profile_id, "new-source-1");
    assert.equal(render().savedProfile.applied, true);
    assert.equal(calls.creates.length, 1);
    assert.equal(calls.applies.length, 1);
  }, { applyError: new Error("Apply response interrupted"), operationError: new Error("Operation temporarily unavailable") });
});


test("unchanged profile application carries the latest name and revision without rewriting it", async () => {
  await withProfileWorkflow(async ({ render, calls, records, draft, review }) => {
    await review(draft(savedSource, structuredClone(savedSource.parameters)));
    records[0].name = "Renamed elsewhere";
    records[0].revision = 4;
    assert.equal(await render().apply(), true);
    assert.equal(calls.creates.length, 0);
    assert.equal(calls.updates.length, 0);
    assert.equal(render().completedOperation.profile.name, "Renamed elsewhere");
    assert.equal(render().completedOperation.profile.revision, 4);
  });
});

for (const conflict of ["changed", "deleted"]) {
  test(`an unchanged selected profile that is ${conflict} elsewhere cannot apply an unseen configuration`, async () => {
    await withProfileWorkflow(async ({ render, calls, records, draft, review }) => {
      await review(draft(savedSource, structuredClone(savedSource.parameters)));
      if (conflict === "changed") records[0].parameters.address = "unreviewed-device";
      else records.splice(0, 1);
      assert.equal(await render().apply(), false);
      assert.equal(calls.creates.length, 0);
      assert.equal(calls.updates.length, 0);
      assert.equal(calls.applies.length, 0);
      assert.match(render().requestError, /profile was changed elsewhere/);
    });
  });
}

for (const conflict of ["changed", "deleted", "changed during planning"]) {
  test(`saved-profile review refuses a profile ${conflict} after the editor loaded it`, async () => {
    await withProfileWorkflow(async ({ render, calls, records }) => {
      const expected = structuredClone(savedSource);
      const request = { module: "acquisition", bindings: [{ slot: "acquisition.source", profile_id: expected.profile_id }],
        refresh_slots: ["acquisition.source"], expected_revision: "old-revision", include_host_settings: false };
      let finishPlan;
      if (conflict === "changed during planning") {
        const originalPlan = runtimeConfigApi.plan;
        runtimeConfigApi.plan = async (body) => {
          const result = await originalPlan(body);
          await new Promise((resolve) => { finishPlan = resolve; });
          return result;
        };
      } else if (conflict === "deleted") records.splice(0, 1);
      else { records[0].revision += 1; records[0].parameters.address = "unseen-device"; }
      const reviewing = render().review(request, undefined, [expected]);
      if (conflict === "changed during planning") {
        for (let index = 0; index < 5; index += 1) await Promise.resolve();
        assert.equal(render().plan, null, "A pending plan is never exposed for Apply");
        records[0].revision += 1;
        records[0].parameters.address = "unseen-device";
        finishPlan();
      }
      assert.equal(await reviewing, null);
      assert.equal(render().plan, null);
      assert.match(render().requestError, /profile was changed elsewhere/);
      assert.equal(await render().apply(), false);
      assert.equal(calls.plans.length, 1);
      assert.equal(calls.creates.length + calls.updates.length + calls.applies.length, 0);
    });
  });
}

test("saved-profile review exposes its pinned plan only after the displayed revisions are verified", async () => {
  await withProfileWorkflow(async ({ render, calls, records }) => {
    let finishProfiles;
    const listedKinds = [];
    profileApi.profiles = async (kind) => {
      listedKinds.push(kind);
      assert.equal(calls.plans.length, 1, "Create the immutable plan before checking the current profile revision");
      await new Promise((resolve) => { finishProfiles = resolve; });
      return structuredClone(records);
    };
    const request = { module: "acquisition", bindings: [{ slot: "acquisition.source", profile_id: savedSource.profile_id }],
      refresh_slots: ["acquisition.source"], expected_revision: "old-revision", include_host_settings: false };
    const reviewing = render().review(request, undefined, [structuredClone(savedSource)]);
    for (let index = 0; index < 5; index += 1) await Promise.resolve();
    assert.deepEqual(listedKinds, ["acquisition"]);
    assert.equal(render().plan, null);
    assert.equal(render().reviewing, true);
    assert.equal(calls.creates.length + calls.updates.length + calls.applies.length, 0);
    finishProfiles();
    const target = await reviewing;
    assert.equal(target.plan_id, "profile-plan-1");
    assert.equal(render().plan, target);
    assert.equal(render().profileReview, null, "Saved-profile review never enters the automatic-save workflow");
    assert.equal(render().requestError, null);
    assert.equal(await render().apply(), true);
    assert.equal(calls.plans.length, 1);
    assert.equal(calls.creates.length + calls.updates.length, 0);
    assert.equal(calls.applies.length, 1);
    assert.equal(calls.applies[0].planId, target.plan_id);
  });
});
