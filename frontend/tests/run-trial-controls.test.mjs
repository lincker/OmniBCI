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
const legacyControls = { cue: { type: "int", field: "trial_params", default: 2, min: 1, max: 4 } };
const sequenceControls = {
  trials: { type: "int", field: "n_trials", default: 1, min: 1 },
  cue: { type: "int", field: "trial_params", default: 1, min: 0, max: 40, default_sequence: "1-40" },
};

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
  loadError = null, runtime = {}, acquisition = { connected: false, streaming: false },
  selection = activation, engine = { running: true }, engineError = null, acquisitionError = null,
  signal = null, signalError = null,
  controls = legacyControls, controlValues = { n_trials: "3", cue: "2" }, trialError = null,
  onStateChange = noop } = {}) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  // Seed the two polls, then the accepted selection and loaded trial-control state.
  const states = [stimulation, stimulationError, signal, signalError, selection, [], selectedParadigm,
    controls, loadError, controlValues, trialError];
  let stateIndex = 0;
  dispatcher.current = {
    useState: (initial) => {
      const index = stateIndex++;
      return [index < states.length ? states[index] : typeof initial === "function" ? initial() : initial,
        (value) => onStateChange(index === 9 ? "controlValues" : index === 10 ? "trialError" : index, value)];
    },
    useContext: () => english, useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback, useEffect: noop,
  };
  try {
    return RunConsole({ engine: { data: engine, error: engineError, refresh: noop },
      acquisition: { data: acquisition, error: acquisitionError, refresh: noop },
      feed: { generation: null, trials: [], acks: [] },
      runtime: { status: { accepted_revision: revision, applied_revision: revision }, blocked: false, ...runtime },
      onBusyChange: noop, onEditSetup: noop, onResults: noop });
  } finally { dispatcher.current = previous; }
}
function triggerButton(tree) {
  const button = elements(tree).find((element) => element.type === "button" && text(element) === "Trigger trial");
  assert.ok(button, "Missing Trigger trial button");
  return button;
}
function actionButton(tree, label) {
  const button = elements(tree).find((element) => element.type === "button" && text(element) === label);
  assert.ok(button, `Missing ${label} button`);
  return button;
}
function description(tree, button) {
  const id = button.props["aria-describedby"];
  assert.ok(id, "Disabled action must reference its visible explanation");
  const hint = elements(tree).find((element) => element.props.id === id);
  assert.ok(hint, "Explanation must be rendered alongside the action");
  assert.notEqual(hint.props.role, "alert", "An unavailable action is guidance, not an error alert");
  return text(hint);
}
function submit(tree) {
  const form = elements(tree).find((element) => element.type === "form");
  assert.ok(form, "Missing trial controls form");
  form.props.onSubmit({ preventDefault: noop });
}
function withTrialCalls(run) {
  const original = api.trialStart;
  const calls = [];
  api.trialStart = async (...args) => { calls.push(args); };
  try { run(calls); } finally { api.trialStart = original; }
}

test("the selected connected paradigm enables Trigger trial and sends its trial controls", () => {
  withTrialCalls((calls) => {
    const tree = render();
    assert.equal(triggerButton(tree).props.disabled, false);
    assert.equal(elements(tree).some((element) => element.type === "input" && element.props.type === "text"), false);
    submit(tree);
    assert.deepEqual(calls, [[3, [2, 2, 2]]]);
  });
});

test("the default sequence submits each of the 40 targets once in a single request", () => {
  withTrialCalls((calls) => {
    const tree = render({ controls: sequenceControls, controlValues: { trials: "1" } });
    const input = elements(tree).find((element) => element.type === "input" && element.props.type === "text");
    assert.equal(input.props.value, "1-40");
    assert.match(text(tree), /Target sequence/);
    assert.match(text(tree), /Sequence repetitions/);
    assert.match(text(tree), /40 targets per sequence × 1 repetitions = 40 trials\./);
    submit(tree);
    assert.deepEqual(calls, [[40, Array.from({ length: 40 }, (_, index) => index + 1)]]);
  });
});

for (const [name, sequence, repetitions, expected] of [
  ["custom sequence repeated roundwise", "1-3,5", "2", [1, 2, 3, 5, 1, 2, 3, 5]],
  ["one target repeated", "5", "3", [5, 5, 5]],
  ["uncued target zero", "0", "1", [0]],
  ["user order and duplicate targets", "5,1,5", "2", [5, 1, 5, 5, 1, 5]],
]) {
  test(`editable target text submits ${name}`, () => {
    withTrialCalls((calls) => {
      submit(render({ controls: sequenceControls, controlValues: { trials: repetitions, cue: sequence } }));
      assert.deepEqual(calls, [[expected.length, expected]]);
    });
  });
}

test("sequence repetitions also work without a plugin trial-count control", () => {
  withTrialCalls((calls) => {
    submit(render({ controls: { cue: sequenceControls.cue }, controlValues: { n_trials: "2", cue: "1,3" } }));
    assert.deepEqual(calls, [[4, [1, 3, 1, 3]]]);
  });
});

for (const [name, sequence, repetitions] of [
  ["empty sequence", "", "1"],
  ["malformed range", "1-x", "1"],
  ["descending range", "3-1", "1"],
  ["fractional target", "1.5", "1"],
  ["out-of-range target", "41", "1"],
  ["out-of-range expansion", "1-41", "1"],
  ["empty repetitions", "1-40", ""],
  ["zero repetitions", "1-40", "0"],
  ["fractional repetitions", "1-40", "1.5"],
  ["total exceeding the trial limit", "1-40", "251"],
]) {
  test(`invalid ${name} blocks submission and shows an error`, () => {
    withTrialCalls((calls) => {
      let error;
      const options = { controls: sequenceControls, controlValues: { trials: repetitions, cue: sequence } };
      submit(render({ ...options, onStateChange: (state, value) => { if (state === "trialError") error = value; } }));
      assert.deepEqual(calls, []);
      assert.ok(error, "Invalid input should set an error");
      const alert = elements(render({ ...options, trialError: error })).find((element) => element.props.role === "alert");
      assert.ok(alert, "Invalid input should show an inline error");
      assert.notEqual(text(alert), "");
    });
  });
}

test("Reset sequence restores the plugin default and clears the error while keeping repetitions", () => {
  const changes = new Map();
  const tree = render({ controls: sequenceControls, controlValues: { trials: "2", cue: "invalid" },
    trialError: "sequence", onStateChange: (state, value) => changes.set(state, value) });
  const button = elements(tree).find((element) => element.type === "button" && text(element) === "Reset sequence");
  assert.ok(button);
  assert.equal(button.props.type, "button");
  button.props.onClick();
  assert.deepEqual(changes.get("controlValues"), { trials: "2", cue: "1-40" });
  assert.equal(changes.get("trialError"), null);
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
  test(`Trigger trial remains disabled and rejects submission with ${name}`, () => {
    withTrialCalls((calls) => {
      const tree = render({ controls: sequenceControls, controlValues: { trials: "1", cue: "1-40" }, ...overrides });
      assert.equal(triggerButton(tree).props.disabled, true);
      assert.ok(description(tree, triggerButton(tree)).length > "Trigger trial: ".length);
      submit(tree);
      assert.deepEqual(calls, []);
    });
  });
}

for (const [label, overrides, reason] of [
  ["Start engine", { engine: null }, /Engine status is unavailable/],
  ["Start engine", { engine: { running: false }, selection: null }, /Load a valid configuration in Setup/],
  ["Connect", { engine: { running: false } }, /Start the engine first/],
  ["Disconnect", { acquisition: { connected: true, streaming: true } }, /Stop reading before disconnecting/],
  ["Start reading", {}, /Connect the source first/],
  ["Start reading", { acquisitionError: new Error("Unavailable") }, /Acquisition status is unavailable/],
  ["Trigger trial", { stimulation: { ...ready, engine_connected: false } }, /Wait for stimulation to connect/],
  ["Trigger trial", { stimulation: { ...ready, paradigm: "other" } }, /running paradigm differs/],
  ["Trigger trial", { runtime: { status: { accepted_revision: revision, applied_revision: "previous" } } }, /selected configuration is not active/],
]) {
  test(`${label} explains the unmet prerequisite: ${reason.source}`, () => {
    const tree = render(overrides);
    const button = actionButton(tree, label);
    assert.equal(button.props.disabled, true);
    assert.match(description(tree, button), reason);
  });
}

test("a shared configuration blocker is shown once per action group", () => {
  const tree = render({ runtime: { blocked: true, inProgress: true } });
  const start = actionButton(tree, "Start engine");
  const stop = actionButton(tree, "Stop engine");
  assert.equal(start.props["aria-describedby"], stop.props["aria-describedby"]);
  const hint = description(tree, start);
  assert.match(hint, /Start engine \/ Stop engine:/);
  assert.equal(hint.match(/Applying configuration\. Wait for the operation to finish\./g).length, 1);
});

test("failed status polls do not display cached active indicators or source measurements", () => {
  const tree = render({ engine: { running: true, acquisition_alive: true, processor_alive: true },
    engineError: new Error("Engine request failed"),
    acquisition: { connected: true, streaming: true, source: "Cached source", sample_rate: 12345, channel_count: 99 },
    acquisitionError: new Error("Acquisition request failed"), stimulationError: new Error("Stimulation request failed") });
  const dots = elements(tree).filter((element) => element.type.name === "Dot");
  assert.equal(dots.length, 7);
  assert.ok(dots.every((element) => element.props.on === undefined));
  assert.doesNotMatch(text(tree), /Cached source|12345 Hz|99 channels/);
  const status = elements(tree).find((element) => element.props.className?.startsWith("status-tag"));
  assert.equal(text(status), "Engine unavailable");
  assert.doesNotMatch(status.props.className, /success/);
});

test("selected setup distinguishes failed and changed configuration from an empty slot", () => {
  const summary = (tree) => elements(tree).find((element) => element.props["aria-label"] === "Selected setup");
  assert.doesNotMatch(text(summary(render({ selection: null, loadError: "Request failed" }))), /Loading/);
  assert.match(text(summary(render({ selection: null, loadError: "Request failed" }))), /Unavailable/);
  assert.match(text(summary(render({ runtime: { status: { accepted_revision: "next-revision" } } }))), /Loading/);
  assert.match(text(summary(render({ selection: { revision, bindings: [] } }))), /None/);
});

test("active decoder input does not present cached readiness as current after a failed poll", () => {
  const signal = { readiness: { ready: true }, active_input: { outputs: [{ name: "Cached channel" }] } };
  for (const overrides of [{ engineError: new Error("Unavailable") }, { signalError: new Error("Unavailable") }]) {
    const tree = render({ signal, ...overrides });
    const summary = elements(tree).find((element) => element.props["aria-label"] === "Active decoder input");
    assert.match(text(summary), /Unavailable/);
    assert.doesNotMatch(text(summary), /Cached channel|Ready/);
  }
});

test("recovery explains blocked actions while retaining usable stop controls", () => {
  const tree = render({ acquisition: { connected: true, streaming: true },
    runtime: { blocked: true, recoveryRequired: true, inProgress: false } });
  for (const label of ["Start engine", "Connect", "Disconnect", "Start reading", "Launch stimulation", "Trigger trial"]) {
    const button = actionButton(tree, label);
    assert.equal(button.props.disabled, true);
    assert.match(description(tree, button), /Recovery is required/);
  }
  for (const label of ["Stop engine", "Stop reading", "Stop stimulation"]) {
    const button = actionButton(tree, label);
    assert.equal(button.props.disabled, false);
    assert.equal(button.props["aria-describedby"], undefined);
  }
});

test("a ready trial can run without acquisition streaming or an unavailable-action hint", () => {
  const tree = render({ acquisition: { connected: false, streaming: false } });
  assert.equal(triggerButton(tree).props.disabled, false);
  assert.equal(triggerButton(tree).props["aria-describedby"], undefined);
  assert.equal(elements(tree).some((element) => element.props.id === "trial-action-reasons"), false);
});

for (const [label, method, streaming] of [
  ["Start reading", "acqStreamStart", false], ["Stop reading", "acqStreamStop", true],
]) {
  test(`${label} directly controls the stream without a session`, () => {
    const original = api[method];
    let calls = 0;
    api[method] = async () => { calls += 1; };
    try {
      const tree = render({ acquisition: { connected: true, streaming } });
      const button = elements(tree).find((element) => element.type === "button" && text(element) === label);
      assert.ok(button);
      assert.equal(button.props.disabled, false);
      button.props.onClick();
      assert.equal(calls, 1);
    } finally { api[method] = original; }
  });
}
