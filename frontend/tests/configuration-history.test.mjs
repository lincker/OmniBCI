import { runtimeController, currentController } from "./module-fixtures.mjs";
/** Histories identify applications by entry ID and never apply a profile restore. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";
const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { ConfigurationHistory } = await server.ssrLoadModule("/src/ConfigurationHistory.tsx");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { t: (key, params) => translate("en", key, params) };
const elements = (tree) => Array.isArray(tree) ? tree.flatMap(elements) : React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
const text = (tree) => Array.isArray(tree) ? tree.map(text).join("") : React.isValidElement(tree) ? text(tree.props.children) : tree == null || typeof tree === "boolean" ? "" : String(tree);
function harness(props) {
  const hooks = []; let cursor = 0, changed = true, effects = [], tree;
  const same = (left, right) => left?.length === right?.length && left.every((value, index) => Object.is(value, right[index]));
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++; hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (next) => { hooks[index].value = typeof next === "function" ? next(hooks[index].value) : next; changed = true; }];
    },
    useEffect(effect, deps) {
      const index = cursor++, previous = hooks[index];
      if (!previous || !same(previous.deps, deps)) {
        hooks[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { previous?.cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  return {
    get tree() { return tree; },
    async settle() {
      for (let round = 0; round < 40; round++) {
        if (changed) {
          const internal = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = internal.current; internal.current = dispatcher; cursor = 0; changed = false;
          try { tree = ConfigurationHistory({ ...props, runtime: runtimeController(props.runtime) }); } finally { internal.current = previous; }
          const pending = effects; effects = []; pending.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(changed, false);
    },
    async click(label, index = 0) {
      const button = elements(tree).filter((item) => item.type === "button" && text(item) === label)[index];
      assert.ok(button, label); assert.ok(!button.props.disabled, label); await button.props.onClick(); await this.settle();
    },
    dispose() { hooks.forEach((hook) => hook.cleanup?.()); },
  };
}
const profile = { profile_id: "profile-1", revision: 3, name: "Saved decoder", parameters: { gain: 3 } };
const entries = ["A", "B", "A"].map((revision, index) => ({ entry_id: `entry-${index}`, revision, saved_at: `2026-11-15T00:00:0${index}Z`, binding_summary: ["processing.decoder"] }));
async function withHistory(action, overrides = {}) {
  const original = { ...pluginConfigApi }, calls = [];
  const runtime = { status: { accepted: { processing: "A" } }, blocked: false,
    reviewPlan: (...args) => calls.push(["review", ...args]) };
  pluginConfigApi.moduleHistory = async () => structuredClone(entries);
  pluginConfigApi.profileHistory = async () => [1, 2, 3].map((revision) => ({ revision, profile_id: profile.profile_id, saved_at: "2026-11-15T00:00:00Z", profile: { ...profile, revision } }));
  pluginConfigApi.moduleRevision = async (module, entry) => { calls.push(["view", module, entry]); return { module, revision: "A", settings: {}, bindings: [] }; };
  pluginConfigApi.moduleRevertPlan = async (module, entry) => { calls.push(["plan", module, entry]); return { plan_id: `plan-${entry}`, module, errors: [] }; };
  pluginConfigApi.profileRevert = async (saved, revision) => { calls.push(["restore", saved.profile_id, saved.revision, revision]); return { ...saved, revision: saved.revision + 1, parameters: { gain: revision } }; };
  Object.assign(pluginConfigApi, overrides);
  const props = { module: "processing", profile, runtime, onProfileRestored: (saved) => { calls.push(["profile", saved]); props.profile = saved; } };
  const component = harness(props);
  try { await component.settle(); await component.click("Configuration history"); await action(component, calls, props); }
  finally { component.dispose(); Object.assign(pluginConfigApi, original); }
}
test("A to B to A remains three entries and View/Revert use the selected entry ID", async () => {
  await withHistory(async (component, calls) => {
    const rows = elements(component.tree).filter((item) => item.type === "li" && String(item.key).startsWith("entry-"));
    assert.deepEqual(rows.map((item) => item.key), ["entry-0", "entry-1", "entry-2"]);
    await component.click("View", 2);
    await component.click("Review revert…", 2);
    assert.deepEqual(calls.slice(0, 2), [["view", "processing", "entry-2"], ["plan", "processing", "entry-2"]]);
    assert.deepEqual(calls[2], ["review", { plan_id: "plan-entry-2", module: "processing", errors: [] }, "processing"]);
  });
});
test("restoring a saved profile uses current CAS and leaves module application explicit", async () => {
  await withHistory(async (component, calls, props) => {
    await component.click("Restore this version");
    assert.deepEqual(calls[0], ["restore", "profile-1", 3, 1]);
    assert.equal(props.profile.revision, 4);
    assert.equal(calls.some(([name]) => ["review", "plan"].includes(name)), false);
    assert.match(text(component.tree), /Current values and running modules stay unchanged/);
  });
});
test("failed profile restores display the error without replacing the saved profile", async () => {
  await withHistory(async (component, calls, props) => {
    await component.click("Restore this version");
    assert.match(text(component.tree), /Profile changed/);
    assert.equal(props.profile.revision, 3); assert.deepEqual(calls, []);
  }, { profileRevert: async () => { throw new Error("Profile changed; refresh before restoring"); } });
});
