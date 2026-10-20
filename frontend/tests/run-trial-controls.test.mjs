/** Trial controls require the selected paradigm and a connected stimulation runtime. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const noop = () => {};
const revision = "accepted-stimulation";
const paradigm = { selection: { package_id: "example.visual", contribution_id: "main", version: "1.0.0" },
  kind: "paradigm", plugin_type: "visual_trials", label: "Visual trials", available: true };
const activation = { revision, bindings: [{ slot: "stimulation.paradigm", kind: "paradigm",
  selection: paradigm.selection, parameters: {} }] };
const ready = { active: true, process_alive: true, engine_connected: true, paradigm: paradigm.plugin_type };

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function render({ stimulation = ready, stimulationError = null, selectedParadigm = paradigm,
  loadError = null, runtime = {} } = {}) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  // Seed the two polls, then the accepted selection and loaded trial-control state.
  const states = [stimulation, stimulationError, null, null, activation, [], selectedParadigm,
    { cue: { type: "int", field: "trial_params", default: 2, min: 1, max: 4 } },
    loadError, { n_trials: "3", cue: "2" }, null];
  let stateIndex = 0;
  dispatcher.current = {
    useState: (initial) => [stateIndex < states.length ? states[stateIndex++]
      : typeof initial === "function" ? initial() : initial, noop],
    useContext: () => english, useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback, useEffect: noop,
  };
  try {
    return RunConsole({ engine: { data: { running: true }, error: null, refresh: noop },
      acquisition: { data: { connected: false, streaming: false }, error: null, refresh: noop },
      feed: { session: null, trials: [], acks: [] },
      runtime: { status: { accepted_revision: revision, applied_revision: revision }, blocked: false, ...runtime },
      onBusyChange: noop, onEditSetup: noop, onResults: noop });
  } finally { dispatcher.current = previous; }
}
function triggerButton(tree) {
  const button = elements(tree).find((element) => element.type === "button" && text(element) === "Trigger trial");
  assert.ok(button, "Missing Trigger trial button");
  return button;
}

test("the selected connected paradigm enables Trigger trial and sends its trial controls", () => {
  const original = api.trialStart;
  const calls = [];
  api.trialStart = async (...args) => { calls.push(args); };
  try {
    const tree = render();
    assert.equal(triggerButton(tree).props.disabled, false);
    const form = elements(tree).find((element) => element.type === "form");
    form.props.onSubmit({ preventDefault: noop });
    assert.deepEqual(calls, [[3, [2, 2, 2]]]);
  } finally { api.trialStart = original; }
});

for (const [name, overrides] of [
  ["missing reported paradigm", { stimulation: { ...ready, paradigm: null } }],
  ["different reported paradigm", { stimulation: { ...ready, paradigm: "another_paradigm" } }],
  ["disconnected engine", { stimulation: { ...ready, engine_connected: false } }],
  ["inactive stimulation", { stimulation: { ...ready, active: false } }],
  ["failed status request", { stimulationError: new Error("Stimulation is unavailable") }],
  ["unavailable selected plugin", { selectedParadigm: { ...paradigm, available: false } }],
  ["invalid controls", { loadError: "Invalid trial controls" }],
  ["unapplied selection", { runtime: { status: { accepted_revision: revision, applied_revision: "previous" } } }],
  ["runtime operation", { runtime: { blocked: true } }],
]) {
  test(`Trigger trial remains disabled with ${name}`, () => {
    assert.equal(triggerButton(render(overrides)).props.disabled, true);
  });
}
