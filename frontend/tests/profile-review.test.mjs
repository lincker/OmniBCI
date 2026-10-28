/** Final Review is the sole acquisition naming/save decision and shows the exact parameter changes. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const { RuntimeConfigurationReview } = await server.ssrLoadModule("/src/RuntimeConfiguration.tsx");
const { profileParameterChanges, formatProfileValue } = await server.ssrLoadModule("/src/profileReviewDiff.ts");
const { initialProfileReview } = await server.ssrLoadModule("/src/profileReview.ts");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const selection = { package_id: "source.example", contribution_id: "reader", version: "1.0.0" };
const original = { kind: "acquisition", profile_id: "source-a", selection, name: "Stable source",
  parameters: { address: "old-device", port: 4000 }, revision: 3, configuration_version: 1 };
const schema = { type: "object", properties: { address: { type: "string", title: "Device address" },
  port: { type: "integer", title: "Port" } } };
function runtimeFor(profile = null, parameters = { address: "new-device", port: 4000 }) {
  const unchanged = profile && JSON.stringify(profile.parameters) === JSON.stringify(parameters);
  const runtime = {
    plan: { plan_id: "review-a", strategy: "select_on_start", expected_revision: "before", target_revision: "after",
      running: false, applied_revision: null, generation: null, changed_slots: ["acquisition.source"], changed_settings: [], errors: [] },
    status: { accepted_revision: "before", running: false, applied_revision: null, generation: null },
    profileReview: { draft: { kind: "acquisition", selection, profile, parameters, name: profile?.name ?? "New reader", schema },
      mode: unchanged ? "existing" : "create", name: profile ? "Stable source copy" : "New reader", savedProfile: null, saveUncertain: false },
    blocked: false, submitting: false, profileSaveNotice: null, requestError: null,
    calls: { apply: 0, cancel: 0, check: 0 },
    setProfileReviewMode(mode) { runtime.profileReview.mode = mode; },
    setProfileReviewName(name) { runtime.profileReview.name = name; },
    dismissPlan() { runtime.calls.cancel += 1; },
    apply() { runtime.calls.apply += 1; },
    retryProfileSave() { runtime.calls.check += 1; },
    abandonProfileSave() { runtime.returnedToEditor = true; },
  };
  return runtime;
}
function render(runtime) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  dispatcher.current = { useContext: () => english, useRef: (value) => ({ current: value }), useEffect: () => {} };
  try { return RuntimeConfigurationReview({ runtime }); } finally { dispatcher.current = previous; }
}
function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  return React.isValidElement(tree) ? text(tree.props.children) : tree == null || typeof tree === "boolean" ? "" : String(tree);
}
const buttons = (tree) => elements(tree).filter((item) => item.type === "button");
const primary = (tree) => buttons(tree).find((item) => item.props.className === "primary");
const nameInput = (tree) => elements(tree).find((item) => item.type === "input" && item.props.maxLength === 64);
const radios = (tree) => elements(tree).filter((item) => item.type === "input" && item.props.type === "radio");

test("first-source Review supplies the generated name and saves only when its final action is chosen", () => {
  const runtime = runtimeFor();
  const tree = render(runtime);
  assert.equal(nameInput(tree).props.value, "New reader");
  assert.match(text(tree), /Profile name/);
  assert.equal(radios(tree).length, 0);
  assert.equal(text(primary(tree)), "Save profile and apply");
  nameInput(tree).props.onChange({ target: { value: "Office device" } });
  assert.equal(runtime.profileReview.name, "Office device");
  buttons(tree).find((item) => text(item) === "Cancel").props.onClick();
  assert.deepEqual(runtime.calls, { apply: 0, cancel: 1, check: 0 });
  primary(render(runtime)).props.onClick();
  assert.equal(runtime.calls.apply, 1);
});

test("changed-profile Review displays old and new values and defaults to preserving the original", () => {
  const runtime = runtimeFor(original);
  let tree = render(runtime);
  assert.deepEqual(radios(tree).map((item) => [item.props.value, item.props.checked]), [["create", true], ["update", false]]);
  assert.equal(text(primary(tree)), "Save as new profile and apply");
  const table = elements(tree).find((item) => item.type === "table");
  assert.match(text(table), /Parameter changes/);
  assert.match(text(table), /Device address/);
  assert.match(text(table), /old-device/);
  assert.match(text(table), /new-device/);
  assert.doesNotMatch(text(table), /Port/);
  radios(tree).find((item) => item.props.value === "update").props.onChange();
  tree = render(runtime);
  assert.equal(text(primary(tree)), "Update profile and apply");
  assert.deepEqual(radios(tree).map((item) => item.props.checked), [false, true]);
});

test("an unchanged profile needs no copy choice or new profile name", () => {
  const runtime = runtimeFor(original, structuredClone(original.parameters));
  const tree = render(runtime);
  assert.equal(radios(tree).length, 0);
  assert.equal(nameInput(tree), undefined);
  assert.equal(elements(tree).filter((item) => item.type === "table").length, 0);
  assert.equal(text(primary(tree)), "Apply configuration");
});

test("Review after a partial save reuses schema defaults without hiding actual parameter changes", () => {
  const parameters = { address: "new-device" };
  const draft = { kind: "acquisition", selection, profile: null, parameters, name: "Office device",
    schema: { ...schema, properties: { ...schema.properties, port: { ...schema.properties.port, default: 4000 } } } };
  assert.equal(initialProfileReview(draft, "Office device copy").mode, "create");
  // Saving fills the omitted default, but failed Apply leaves the submitted editor values intact.
  const saved = { ...original, name: draft.name, parameters: { ...parameters, port: 4000 } };
  const retryDraft = { ...draft, profile: saved };
  const review = initialProfileReview(retryDraft, "Office device copy");
  assert.equal(review.mode, "existing");
  assert.deepEqual(review.draft.parameters, parameters, "Review must not rewrite the editor draft");
  const runtime = runtimeFor(saved, parameters);
  runtime.profileReview = review;
  const tree = render(runtime);
  assert.equal(radios(tree).length, 0);
  assert.equal(nameInput(tree), undefined);
  assert.equal(text(primary(tree)), "Apply configuration");
  const changed = initialProfileReview({ ...retryDraft, parameters: { ...parameters, port: 5000 } }, "Office device copy");
  assert.equal(changed.mode, "create");
  assert.equal(changed.name, "Office device copy");
  const implicitProfile = { ...saved, parameters };
  const explicitDraft = { ...retryDraft, profile: implicitProfile, parameters: { ...parameters, port: 4000 } };
  assert.equal(initialProfileReview(explicitDraft, "Office device copy").mode, "existing", "Normalize both sides of the comparison");
  assert.deepEqual(draft.parameters, { address: "new-device" });
  assert.deepEqual(saved.parameters, { address: "new-device", port: 4000 });
});

test("blank final names keep the save-and-apply action disabled with an accessible field error", () => {
  const runtime = runtimeFor();
  runtime.profileReview.name = "   ";
  const tree = render(runtime);
  assert.equal(primary(tree).props.disabled, true);
  assert.equal(nameInput(tree).props["aria-invalid"], true);
  assert.ok(nameInput(tree).props["aria-describedby"]);
});

test("an unresolved save disables resubmission and offers checking the existing save", () => {
  const runtime = runtimeFor(original);
  runtime.profileReview.saveUncertain = true;
  runtime.blocked = true;
  const tree = render(runtime);
  assert.equal(primary(tree).props.disabled, true);
  assert.equal(nameInput(tree).props.disabled, true);
  assert.equal(elements(tree).find((item) => item.type === "fieldset").props.disabled, true);
  buttons(tree).find((item) => /Check profile save/.test(text(item))).props.onClick();
  assert.deepEqual(runtime.calls, { apply: 0, cancel: 0, check: 1 });
  buttons(tree).find((item) => text(item) === "Return to editor").props.onClick();
  assert.equal(runtime.returnedToEditor, true);
  assert.equal(runtime.calls.apply, 0);
});

test("a saved but unapplied profile is disclosed and cannot be renamed into a duplicate on retry", () => {
  const runtime = runtimeFor(original);
  runtime.profileReview.savedProfile = { ...original, profile_id: "copy-a" };
  runtime.profileSaveNotice = "Profile saved; configuration has not been applied.";
  const tree = render(runtime);
  assert.equal(nameInput(tree), undefined);
  assert.equal(radios(tree).length, 0);
  assert.match(text(tree), /Profile saved; configuration has not been applied/);
  assert.equal(text(primary(tree)), "Apply saved profile");
});

test("parameter comparison ignores object key ordering but retains array order, removals and nulls", () => {
  assert.deepEqual(profileParameterChanges({ a: 1, b: { x: 2, y: 3 } }, { b: { y: 3, x: 2 }, a: 1 }), []);
  const changes = profileParameterChanges({ ordered: ["C3", "C4"], removed: 42 }, { ordered: ["C4", "C3"], empty: null });
  assert.equal(changes.length, 3);
  assert.deepEqual(changes.find((item) => item.label === "ordered").after, ["C4", "C3"]);
  assert.equal(changes.find((item) => item.label === "removed").afterPresent, false);
  assert.equal(changes.find((item) => item.label === "empty").beforePresent, false);
  assert.equal(changes.find((item) => item.label === "empty").after, null);
  assert.equal(formatProfileValue(""), '\"\"');
  assert.equal(formatProfileValue(null), "null");
  assert.equal(formatProfileValue([]), "[]");
});

test("added nested secrets and secrets inside arrays are redacted in both sides of the displayed diff", () => {
  const runtime = runtimeFor({ ...original, parameters: { endpoints: [{ password: "old-secret" }] } },
    { credentials: { password: "new-secret", user: "operator" }, endpoints: [{ password: "updated-secret" }] });
  runtime.profileReview.draft.schema = { type: "object", properties: {
    credentials: { type: "object", title: "Credentials", properties: {
      password: { type: "string", title: "Password", "x-scope": "secret" }, user: { type: "string", title: "User" } } },
    endpoints: { type: "array", title: "Endpoints", items: { type: "object", properties: {
      password: { type: "string", "x-scope": "secret" } } } },
  } };
  const changes = profileParameterChanges(runtime.profileReview.draft.profile.parameters,
    runtime.profileReview.draft.parameters, runtime.profileReview.draft.schema);
  assert.equal(changes.find((item) => item.label === "Credentials › Password").secret, true);
  assert.equal(changes.find((item) => item.label === "Endpoints").secret, true);
  const displayed = text(render(runtime));
  assert.doesNotMatch(displayed, /old-secret|new-secret|updated-secret/);
  assert.match(displayed, /operator/);
  assert.match(displayed, /Credentials › Password/);
});
