/** Settings load and save without the retired Acquisition profile API. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { ConfigCard } = await server.ssrLoadModule("/src/ConfigCard.tsx");
const sameDependencies = (a, b) => !!a && !!b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));

function harness(props) {
  const hooks = [];
  let cursor = 0, pending = true, tree;
  let effects = [];
  const dispatcher = {
    useContext: () => ({ t: (key) => key }),
    useState(initial) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = { value: typeof initial === "function" ? initial() : initial };
      return [hooks[i].value, (next) => {
        const value = typeof next === "function" ? next(hooks[i].value) : next;
        if (!Object.is(value, hooks[i].value)) { hooks[i].value = value; pending = true; }
      }];
    },
    useRef(initial) { return hooks[cursor++] ??= { current: initial }; },
    useCallback(callback, deps) {
      const i = cursor++;
      if (!sameDependencies(hooks[i]?.deps, deps)) hooks[i] = { value: callback, deps };
      return hooks[i].value;
    },
    useEffect(effect, deps) {
      const i = cursor++;
      if (!sameDependencies(hooks[i]?.deps, deps)) {
        const previous = hooks[i];
        hooks[i] = { deps };
        effects.push(() => { previous?.cleanup?.(); hooks[i].cleanup = effect(); });
      }
    },
  };
  return {
    get tree() { return tree; },
    async settle() {
      for (let round = 0; round < 40; round += 1) {
        if (pending) {
          const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
          const previous = current.current;
          cursor = 0; pending = false; current.current = dispatcher;
          try { tree = ConfigCard(props); } finally { current.current = previous; }
          const queued = effects; effects = []; queued.forEach((effect) => effect());
        }
        await Promise.resolve();
      }
      assert.equal(pending, false, "Settings component did not settle");
    },
    dispose() { hooks.forEach((hook) => hook?.cleanup?.()); },
  };
}

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function button(tree, label) {
  const found = elements(tree).find((item) => item.type === "button" && text(item) === label);
  assert.ok(found, `Missing button: ${label}`);
  return found;
}

for (const view of ["general", "advanced"]) for (const running of [false, true]) {
  test(`${view} settings load without legacy profiles while engine running=${running}`, async () => {
    const cfg = { active_profile: null, entries: [
      { key: "NEUROENGINE.queue_size", type: "int", default_value: 32, effective_value: 32, description: "Queue capacity" },
      { key: "ACQUISITION.sample_rate", type: "int", default_value: 1000, effective_value: 1000, description: "Sample rate" },
      { key: "ALGORITHM.decoder", type: "str", default_value: "decoder", effective_value: "decoder", description: "Decoder" },
    ] };
    const calls = [], reviews = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (path, options = {}) => {
      const method = options.method ?? "GET";
      const body = options.body ? JSON.parse(options.body) : undefined;
      calls.push({ path, method, body });
      let data;
      if (path === "/api/v1/config" && method === "GET") data = cfg;
      else if (path === "/api/v1/config/profiles") data = { profiles: [], active_profile: null };
      else if (path.startsWith("/api/v1/plugins/")) data = { paradigms: [{ plugin_type: "decoder", display_name: "Decoder" }] };
      else if (path === "/api/v1/config/activation") data = { revision: "accepted", bindings: [{ slot: "acquisition.source", profile_id: "saved" }] };
      else if (path === "/api/v1/config" && method === "PATCH") {
        cfg.entries[0].default_value = body.updates["NEUROENGINE.queue_size"];
        data = {};
      } else return { ok: false, status: 404, json: async () => ({ code: "NOT_FOUND", message: "Endpoint retired" }) };
      return { ok: true, json: async () => ({ success: true, data: structuredClone(data) }) };
    };
    const component = harness({ view, engineRunning: running, navigation: null, onDirtyChange() {}, onBusyChange() {},
      runtime: { blocked: false, review: async (request) => { reviews.push(request); } } });
    try {
      await component.settle();
      const input = elements(component.tree).find((item) => item.type === "input" && item.props.id === "setting-NEUROENGINE.queue_size");
      assert.ok(input, "Settings must remain available when the retired endpoint returns 404");
      assert.equal(input.props.disabled, false);
      assert.doesNotMatch(text(component.tree), /Profile managed|selected acquisition profile manages/);
      if (view === "advanced") {
        const source = elements(component.tree).find((item) => item.type === "input" && item.props.id === "setting-ACQUISITION.sample_rate");
        assert.equal(source.props.disabled, running, "The existing engine-running lock remains intact");
        assert.ok(elements(component.tree).some((item) => item.type === "datalist" && item.props.id === "options-ALGORITHM.decoder"));
      }
      input.props.onChange({ target: { value: "64" } });
      await component.settle();
      assert.equal(button(component.tree, "Review and apply host settings").props.disabled, true);
      button(component.tree, "Save settings").props.onClick();
      await component.settle();
      assert.deepEqual(calls.filter((call) => call.method === "PATCH").map((call) => call.body), [
        { updates: { "NEUROENGINE.queue_size": 64 }, scope: "default" },
      ]);
      assert.equal(reviews.length, 0, "Saving settings must not apply them");
      assert.equal(button(component.tree, "Review and apply host settings").props.disabled, false);
      button(component.tree, "Review and apply host settings").props.onClick();
      await component.settle();
      assert.deepEqual(reviews, [{ bindings: [{ slot: "acquisition.source", profile_id: "saved" }],
        expected_revision: "accepted", refresh_slots: [], include_host_settings: true }]);
      assert.equal(calls.some((call) => call.path.includes("/config/modules/")), false);
    } finally { component.dispose(); globalThis.fetch = originalFetch; }
  });
}
