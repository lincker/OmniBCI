/** Trial controls require the selected paradigm and a connected stimulation runtime. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { RunConsole } = await server.ssrLoadModule("/src/RunConsole.tsx");
const { api } = await server.ssrLoadModule("/src/api/client.ts");
const { pluginConfigApi } = await server.ssrLoadModule("/src/api/plugin-config.ts");
const { signalConfigApi } = await server.ssrLoadModule("/src/api/signal-config.ts");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const noop = () => {};
const revision = "accepted-stimulation";
const paradigm = { selection: { package_id: "example.visual", contribution_id: "main", version: "1.0.0" },
  kind: "paradigm", plugin_type: "visual_trials", label: "Visual trials", available: true };
const activation = { revision, bindings: [{ slot: "stimulation.paradigm", kind: "paradigm",
  selection: paradigm.selection, parameters: {} }] };
const sourceActivation = { ...activation, bindings: [...activation.bindings, {
  slot: "acquisition.source", kind: "acquisition", profile_id: "source-1", profile_revision: 2,
  selection: { package_id: "example.source", contribution_id: "main", version: "1.2.0" }, parameters: {},
}] };
const completeActivation = { revision, bindings: [
  ["acquisition.source", "acquisition", "source", "source", 2],
  ["processing.filter.z-last", "filter", "shared", "shared-filter", 3],
  ["processing.epoch_detector", "epoch_detector", "detector", "detector", 4],
  ["processing.decoder", "decoder", "decoder", "decoder", 5],
  ["stimulation.paradigm", "paradigm", "paradigm", "paradigm", 6],
  ["processing.filter.a-first", "filter", "middle", "middle-filter", 7],
  ["stimulation.trigger", "trigger", "trigger", "trigger", 8],
  ["actuation.effector", "effector", "effector", "effector", 9],
  ["processing.filter.repeat", "filter", "shared", "shared-filter", 3],
].map(([slot, kind, contribution, profile, profileRevision]) => ({
  slot, kind, profile_id: profile, profile_revision: profileRevision, parameters: {},
  selection: { package_id: `pinned.${kind}`, contribution_id: contribution, version: "1.2.3" },
})) };
const latestProfiles = [...new Map(completeActivation.bindings.map((binding) => [binding.profile_id, {
  profile_id: binding.profile_id, name: `Saved ${binding.profile_id}`, revision: 99,
  selection: { package_id: "latest.package", contribution_id: "new-contribution", version: "9.9.9" },
}])).values()];
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
  profiles = [], refresh = noop, configurationLoad,
  acquisitionPending = false, acquisitionActions = {}, actionErrors = {},
  controls = legacyControls, controlValues = { n_trials: "3", cue: "2" }, trialError = null,
  onStateChange = noop, onEffect = noop, onEditProcessing = noop } = {}) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  // Seed the two polls, then the accepted selection and loaded trial-control state.
  const states = [stimulation, stimulationError, signal, signalError, selection, profiles, selectedParadigm,
    controls, loadError, controlValues, trialError,
    ...["start", "stop", "launch", "stopStim", "trigger"].flatMap((name) => [false, actionErrors[name] ?? null]),
    configurationLoad ?? { status: loadError && (typeof loadError === "string"
      || !["run.invalidTrialControls", "run.trialControlsUnavailable"].includes(loadError.key)) ? "failed" : selection ? "ready" : "loading",
      revision: selection?.revision ?? runtime.status?.accepted_revision ?? revision }, 0];
  let stateIndex = 0;
  dispatcher.current = {
    useState: (initial) => {
      const index = stateIndex++;
      return [index < states.length ? states[index] : typeof initial === "function" ? initial() : initial,
        (value) => onStateChange(({ 9: "controlValues", 10: "trialError" })[index] ?? index, value)];
    },
    useContext: () => english, useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback, useEffect: onEffect,
  };
  try {
    return RunConsole({ engine: { data: engine, error: engineError, refresh },
      acquisition: { data: acquisition, error: acquisitionError, refresh, pending: acquisitionPending,
        ...Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) => [name,
          { run: acquisitionActions[name] ?? noop, pending: acquisitionPending, error: actionErrors[name] ?? null }])) },
      feed: { generation: null, trials: [], acks: [] },
      runtime: { status: { accepted_revision: revision, applied_revision: revision }, blocked: false, refresh, ...runtime },
      onBusyChange: noop, onEditSetup: noop, onEditProcessing, onResults: noop });
  } finally { dispatcher.current = previous; }
}
function triggerButton(tree) {
  const button = elements(tree).find((element) => element.type === "button" && text(element) === "Trigger trial");
  assert.ok(button, "Missing Trigger trial button");
  return button;
}
function actionButton(tree, label) {
  const button = elements(tree).find((element) => element.type === "button" && text(element).trim() === label);
  assert.ok(button, `Missing ${label} button`);
  return button;
}
function card(tree, title) {
  return elements(tree).find((element) => element.type === "section" && element.props.className === "card"
    && text(element.props.children[0]) === title);
}
function description(tree, button) {
  const id = button.props["aria-describedby"];
  assert.ok(id, "Disabled action must reference its visible explanation");
  const hint = elements(tree).find((element) => element.props.id === id.split(" ")[0]);
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

test("connection and reading remain separate visible controls in the acquisition card", () => {
  const tree = render({ selection: sourceActivation, acquisition: { connected: false, streaming: false } });
  for (const label of ["Connect", "Disconnect", "Start reading", "Stop reading"]) {
    assert.ok(elements(card(tree, "Acquisition")).includes(actionButton(tree, label)), label);
  }
  assert.equal(actionButton(tree, "Connect").props.disabled, false);
  assert.equal(actionButton(tree, "Start reading").props.disabled, true);
  assert.match(description(tree, actionButton(tree, "Start reading")), /No source is connected/);
});

test("Connect is available from a stopped engine without a stimulation paradigm", () => {
  const tree = render({ selection: sourceActivation, selectedParadigm: null, engine: { running: false },
    stimulation: null, runtime: { status: { accepted_revision: revision, applied_revision: null } } });
  assert.equal(actionButton(tree, "Connect").props.disabled, false);
  assert.equal(actionButton(tree, "Start reading").props.disabled, true);
  const nodes = elements(tree);
  const engineControls = nodes.find((element) => element.type === "section"
    && element.props["aria-label"] === "Engine controls");
  const startup = nodes.find((element) => element.props["aria-label"] === "Startup configuration");
  assert.ok(engineControls, "Engine controls are always visible on Run");
  assert.ok(nodes.indexOf(engineControls) < nodes.indexOf(startup));
  for (const label of ["Start engine", "Stop engine"]) {
    const button = actionButton(tree, label);
    assert.ok(elements(engineControls).includes(button));
    assert.equal(nodes.filter((element) => element.type === "button" && text(element).trim() === label).length, 1);
    assert.ok(nodes.filter((element) => element.type === "details").every((details) => !elements(details).includes(button)));
  }
  assert.equal(actionButton(tree, "Start engine").props.disabled, false);
  assert.equal(actionButton(tree, "Stop engine").props.disabled, true);
});

test("a connected idle source offers Start reading and explicit inactive status", () => {
  const tree = render({ selection: sourceActivation, acquisition: { connected: true, streaming: false } });
  assert.equal(actionButton(tree, "Start reading").props.disabled, false);
  assert.ok(elements(card(tree, "Acquisition")).some((element) => element.props.label === "Not reading"));
  assert.ok(elements(card(render(), "Acquisition")).some((element) => element.props.label === "Not connected"));
});

test("missing epoch detector blocks reading with a direct processing setup action", () => {
  let edits = 0;
  const selection = { ...sourceActivation, bindings: [...sourceActivation.bindings,
    { slot: "processing.decoder", selection: { package_id: "decoder" }, parameters: {} }] };
  const tree = render({ selection, acquisition: { connected: true, streaming: false }, onEditProcessing: () => { edits++; } });
  const button = actionButton(tree, "Start reading");
  assert.equal(button.props.disabled, true);
  assert.match(description(tree, button), /epoch detector/i);
  const preparation = elements(tree).find((element) => element.props["aria-label"] === "Active decoder input");
  assert.match(text(preparation), /Epoch detector missing/);
  assert.doesNotMatch(text(preparation), /Validated before reading starts|Starting reading validates/);
  actionButton(tree, "Configure processing").props.onClick();
  assert.equal(edits, 1);
});

test("external stimulation explains its mode and requires an external peer without changing settings", () => {
  const selection = { ...sourceActivation, settings: { "STIMULATION.auto_spawn": false } };
  const tree = render({ selection, stimulation: { ...ready, active: false, engine_connected: false, process_alive: false } });
  const connect = actionButton(tree, "Enable stimulation");
  assert.equal(connect.props.disabled, true);
  assert.match(description(tree, connect), /external stimulation process/i);
  assert.match(text(card(tree, "Stimulation")), /External stimulation mode/);
  assert.match(text(card(tree, "Stimulation")), /backend host configuration/);
  assert.match(description(tree, actionButton(tree, "Trigger trial")), /Enable stimulation first/);
  assert.equal(selection.settings["STIMULATION.auto_spawn"], false);
  const connected = render({ selection, stimulation: { ...ready, active: false, process_alive: false } });
  assert.equal(actionButton(connected, "Enable stimulation").props.disabled, false);
});

test("each action error stays beside its controls without masking another group", () => {
  const tree = render({ actionErrors: { start: "Old engine failure", launch: "Renderer failed", stopRead: "Stop failed", trigger: "Trial failed" } });
  assert.match(text(card(tree, "Stimulation")), /Renderer failed/);
  assert.match(text(card(tree, "Stimulation")), /Trial failed/);
  assert.match(text(card(tree, "Acquisition")), /Stop failed/);
  assert.doesNotMatch(text(card(tree, "Stimulation")), /Old engine failure/);
  const engineControls = elements(tree).find((element) => element.type === "section"
    && element.props["aria-label"] === "Engine controls");
  assert.ok(engineControls);
  assert.match(text(engineControls), /Old engine failure/);
  assert.doesNotMatch(text(engineControls), /Renderer failed|Trial failed|Stop failed/);
});

test("Connect refuses an absent source and a stale applied configuration", () => {
  for (const options of [{ selection: activation }, { selection: sourceActivation,
    runtime: { status: { accepted_revision: revision, applied_revision: "old" } } }]) {
    const tree = render(options);
    const button = actionButton(tree, "Connect");
    assert.equal(button.props.disabled, true);
    assert.ok(description(tree, button));
  }
});

test("startup summary displays pinned versions rather than the latest saved revision", () => {
  const tree = render({ selection: sourceActivation, profiles: [{ profile_id: "source-1", name: "Lab source",
    revision: 9, selection: { version: "9.9.9" } }] });
  const summary = elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  assert.match(text(summary), /Lab source/);
  assert.match(text(summary), /example\.source\/main/);
  assert.match(text(summary), /Plugin 1\.2\.0/);
  assert.match(text(summary), /Profile revision 2/);
  assert.doesNotMatch(text(summary), /9\.9\.9|Profile revision 9/);
});

test("startup summary includes every configured role and the pinned identity behind saved names", () => {
  const tree = render({ selection: completeActivation, profiles: latestProfiles });
  const summary = elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  for (const key of ["profiles.slotSource", "profiles.slotEpochDetector", "profiles.slotFilters",
    "run.decoder", "run.paradigm", "profiles.slotTrigger", "profiles.slotEffector"]) {
    const label = translate("en", key);
    assert.ok(elements(summary).some((element) => text(element) === label), `Missing startup role: ${label}`);
  }
  for (const binding of completeActivation.bindings) {
    assert.ok(text(summary).includes(`Saved ${binding.profile_id}`));
    assert.ok(text(summary).includes(`${binding.selection.package_id}/${binding.selection.contribution_id}`));
    assert.ok(text(summary).includes(`Profile revision ${binding.profile_revision}`));
  }
  assert.match(text(summary), /Plugin 1\.2\.3/);
  assert.doesNotMatch(text(summary), /latest\.package|new-contribution|9\.9\.9|Profile revision 99/);
});

test("startup summary groups plugins into the four configuration modules", () => {
  const tree = render({ selection: completeActivation, profiles: latestProfiles });
  const summary = elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  const groups = elements(summary).filter((element) => element !== summary
    && (element.type === "section" || element.props.role === "region"));
  const labels = groups.map((group) => group.props["aria-label"]
    ?? text(elements(summary).find((element) => element.props.id === group.props["aria-labelledby"])));
  assert.deepEqual(labels, ["Acquisition", "Signal processing", "Stimulation", "Actuator"]);
  ["acquisition", "processing", "stimulation", "actuation"].forEach((module, index) => {
    for (const binding of completeActivation.bindings) {
      const identity = `${binding.selection.package_id}/${binding.selection.contribution_id}`;
      assert.equal(text(groups[index]).includes(identity), binding.slot.startsWith(`${module}.`),
        `${labels[index]} must contain only its own plugins: ${identity}`);
    }
  });
});

test("startup filters preserve activation order and repeated use of the same profile", () => {
  const tree = render({ selection: completeActivation, profiles: latestProfiles });
  const summary = elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  const lists = elements(summary).filter((element) => element.type === "ol");
  assert.equal(lists.length, 1, "The filter chain must communicate its order with an ordered list");
  const items = elements(lists[0]).filter((element) => element.type === "li");
  const filters = completeActivation.bindings.filter((binding) => binding.slot.startsWith("processing.filter."));
  assert.equal(items.length, 3, "Repeated profiles represent separate filter instances");
  filters.forEach((binding, index) => {
    assert.ok(text(items[index]).includes(`Saved ${binding.profile_id}`));
    assert.ok(text(items[index]).includes(`${binding.selection.package_id}/${binding.selection.contribution_id}`));
    assert.ok(text(items[index]).includes("Plugin 1.2.3"));
    assert.ok(text(items[index]).includes(`Profile revision ${binding.profile_revision}`));
    assert.doesNotMatch(text(items[index]), /9\.9\.9|Profile revision 99/);
  });
});

for (const [name, overrides, expected] of [
  ["loading", { selection: null }, /Loading/],
  ["stale", { selection: { ...completeActivation, revision: "outdated" } }, /Loading/],
  ["failed", { loadError: "Activation unavailable" }, /Unavailable/],
]) test(`startup summary suppresses old binding details while ${name}`, () => {
  const tree = render({ selection: completeActivation, profiles: latestProfiles, ...overrides });
  const summary = elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  assert.match(text(summary), expected);
  assert.doesNotMatch(text(summary), /Saved |pinned\.|latest\.package|Plugin 1\.2\.3|Profile revision/);
  assert.equal(elements(summary).filter((element) => element.type === "li").length, 0);
});

test("invalid paradigm controls do not prevent source-only acquisition", () => {
  const tree = render({ selection: sourceActivation, selectedParadigm: null,
    loadError: { key: "run.invalidTrialControls", params: { detail: "Invalid schema" } } });
  assert.equal(actionButton(tree, "Connect").props.disabled, false);
  assert.equal(triggerButton(tree).props.disabled, true);
});

test("a failed paradigm catalog does not block loading or connecting an accepted source", async () => {
  const original = { ...pluginConfigApi };
  const calls = [], updates = new Map(), effects = [];
  const options = { selection: sourceActivation, engine: { running: false },
    runtime: { status: { accepted_revision: revision, applied_revision: null } },
    acquisitionActions: { connect: async (selectedRevision) => calls.push(["connect", selectedRevision]) } };
  pluginConfigApi.activation = async () => sourceActivation;
  pluginConfigApi.catalog = async () => { throw new Error("Paradigm catalog unavailable"); };
  pluginConfigApi.profiles = async () => { throw new Error("Profile names unavailable"); };
  try {
    render({ ...options, selection: null, profiles: [], selectedParadigm: null,
      onStateChange: (state, value) => updates.set(state, value),
      onEffect: (effect, dependencies) => effects.push([effect, dependencies]) });
    const loadSelection = effects.find(([, dependencies]) => dependencies?.[0] === revision);
    assert.deepEqual(loadSelection[1], [revision, null, 0]);
    const cleanup = loadSelection[0]();
    await new Promise(setImmediate);
    const loaded = render({ ...options, selection: updates.get(4), profiles: updates.get(5),
      selectedParadigm: updates.get(6), controls: updates.get(7), loadError: updates.get(8), configurationLoad: updates.get(21) });
    assert.equal(actionButton(loaded, "Connect").props.disabled, false);
    assert.equal(triggerButton(loaded).props.disabled, true);
    assert.ok(elements(loaded).some((element) => element.props.role === "alert"
      && text(element).includes("Paradigm catalog unavailable")));
    const summary = elements(loaded).find((element) => element.props["aria-label"] === "Startup configuration");
    assert.match(text(summary), /example\.source\/main/);
    await actionButton(loaded, "Connect").props.onClick();
    assert.deepEqual(calls, [["connect", revision]]);
    cleanup();
  } finally { Object.assign(pluginConfigApi, original); }
});

test("Run routes each explicit source action to the shared controller without combining actions", async () => {
  const calls = [];
  const acquisitionActions = Object.fromEntries(["connect", "disconnect", "read", "stopRead"].map((name) =>
    [name, async (...args) => calls.push([name, ...args])]));
  const originalSignal = signalConfigApi.get;
  signalConfigApi.get = async () => null;
  try {
    const disconnected = render({ selection: sourceActivation, acquisitionActions });
    await actionButton(disconnected, "Connect").props.onClick();
    await actionButton(disconnected, "Start reading").props.onClick();
    assert.deepEqual(calls, [["connect", revision]]);
    const connected = render({ selection: sourceActivation, acquisitionActions, acquisition: { connected: true, streaming: false } });
    assert.equal(actionButton(connected, "Connect").props.disabled, true);
    await actionButton(connected, "Connect").props.onClick();
    await actionButton(connected, "Start reading").props.onClick();
    await actionButton(connected, "Disconnect").props.onClick();
    const reading = render({ selection: sourceActivation, acquisitionActions, acquisition: { connected: true, streaming: true } });
    await actionButton(reading, "Disconnect").props.onClick();
    await actionButton(reading, "Start reading").props.onClick();
    await actionButton(reading, "Stop reading").props.onClick();
    assert.deepEqual(calls, [["connect", revision], ["read", revision], ["disconnect"], ["stopRead"]]);
  } finally { signalConfigApi.get = originalSignal; }
});

test("a shared pending source command blocks all source actions and navigation", () => {
  const effects = [], calls = [];
  const tree = render({ selection: sourceActivation, acquisitionPending: true,
    acquisitionActions: { connect: () => calls.push("connect"), read: () => calls.push("read") },
    onEffect: (effect, dependencies) => effects.push([effect, dependencies]) });
  for (const label of ["Connect", "Disconnect", "Start reading", "Stop reading", "Change setup"]) {
    assert.equal(actionButton(tree, label).props.disabled, true, label);
  }
  actionButton(tree, "Connect").props.onClick();
  actionButton(tree, "Start reading").props.onClick();
  assert.deepEqual(calls, []);
  assert.ok(effects.some(([, dependencies]) => dependencies?.[0] === true));
});

test("a failed stimulation launch refreshes status and displays its error next to the action", async () => {
  const original = [{ ...api }, { ...signalConfigApi }];
  const updates = new Map();
  let refreshes = 0;
  api.stimStart = async () => { throw new Error("Renderer initialization failed"); };
  api.stimulationStatus = async () => ({ ...ready, active: false, process_alive: false });
  signalConfigApi.get = async () => null;
  try {
    const options = { stimulation: { ...ready, active: false, process_alive: false },
      refresh: async () => { refreshes++; }, onStateChange: (state, value) => updates.set(state, value) };
    const tree = render(options);
    assert.equal(actionButton(tree, "Launch stimulation").props.disabled, false);
    actionButton(tree, "Launch stimulation").props.onClick();
    await new Promise(setImmediate);
    assert.equal(refreshes, 1, "The shared acquisition controller refreshes source, engine, and runtime together");
    assert.equal(updates.get(0).active, false);
    const result = render({ ...options, actionErrors: { start: "Old engine failure", launch: updates.get(16) } });
    const stim = card(result, "Stimulation");
    assert.ok(elements(stim).some((element) => element.props.role === "alert" && text(element).includes("Renderer initialization failed")));
    assert.doesNotMatch(text(stim), /Old engine failure/);
  } finally { Object.assign(api, original[0]); Object.assign(signalConfigApi, original[1]); }
});

test("shared source errors are visible in the acquisition card while connection status stays authoritative", () => {
  const tree = render({ selection: sourceActivation, acquisition: { connected: true, streaming: false },
    actionErrors: { connect: "Previous connection failed", read: "Decoder input invalid", disconnect: "Connection busy" } });
  const source = card(tree, "Acquisition");
  assert.match(text(source), /Previous connection failed/);
  assert.match(text(source), /Decoder input invalid/);
  assert.match(text(source), /Connection busy/);
  assert.ok(elements(source).some((element) => element.props.label === "Source connected"));
  assert.equal(actionButton(tree, "Connect").props.disabled, true);
  assert.equal(actionButton(tree, "Start reading").props.disabled, false);
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
  ["Start engine", { engine: { running: false }, selection: null }, /Loading the startup configuration/],
  ["Disconnect", { acquisition: { connected: true, streaming: true } }, /Stop reading before disconnecting/],
  ["Connect", {}, /Choose a source profile/],
  ["Connect", { acquisitionError: new Error("Unavailable") }, /Acquisition status is unavailable/],
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
  const summary = (tree) => elements(tree).find((element) => element.props["aria-label"] === "Startup configuration");
  assert.doesNotMatch(text(summary(render({ selection: null, loadError: "Request failed" }))), /Loading/);
  assert.match(text(summary(render({ selection: null, loadError: "Request failed" }))), /Unavailable/);
  assert.match(text(summary(render({ runtime: { status: { accepted_revision: "next-revision" } } }))), /Loading/);
  assert.match(text(summary(render({ selection: { revision, bindings: [] } }))), /None/);
});

test("signal preparation describes only a configured decoder", () => {
  const signal = { readiness: { ready: false }, active_input: null };
  const decoder = { slot: "processing.decoder", kind: "decoder",
    selection: { package_id: "example.decoder", contribution_id: "main", version: "1.0.0" }, parameters: {} };
  for (const [selection, expected, validationHelp] of [
    [sourceActivation, "Signal processing not enabled", false],
    [{ ...sourceActivation, bindings: [...sourceActivation.bindings, decoder,
      { slot: "processing.epoch_detector", selection: { package_id: "example.detector" }, parameters: {} }] }, "Validated before reading starts", true],
    [null, "Loading…", false],
    [{ ...sourceActivation, revision: "outdated" }, "Loading…", false],
  ]) {
    const tree = render({ selection, signal });
    const summary = elements(tree).find((element) => element.props["aria-label"] === "Active decoder input");
    assert.ok(summary);
    const preparation = elements(summary).find((element) => element.type === "div"
      && element.props.children?.[0]?.props.children === "Signal preparation");
    assert.equal(text(preparation), `Signal preparation${expected}`);
    assert.equal(text(summary).includes(translate("en", "run.prepareSignalHelp")), validationHelp);
  }
});

test("active decoder input does not present cached readiness as current after a failed poll", () => {
  const signal = { readiness: { ready: true }, active_input: { outputs: [{ name: "Cached channel" }] } };
  for (const overrides of [{ engineError: new Error("Unavailable") }, { signalError: new Error("Unavailable") }]) {
    const tree = render({ signal, selection: completeActivation, ...overrides });
    const summary = elements(tree).find((element) => element.props["aria-label"] === "Active decoder input");
    assert.match(text(summary), /Unavailable/);
    assert.doesNotMatch(text(summary), /Cached channel|Ready/);
  }
});

test("recovery explains blocked actions while retaining usable stop controls", () => {
  const tree = render({ acquisition: { connected: true, streaming: true },
    runtime: { blocked: true, recoveryRequired: true, inProgress: false } });
  for (const label of ["Start engine", "Disconnect", "Launch stimulation", "Trigger trial"]) {
    const button = actionButton(tree, label);
    assert.equal(button.props.disabled, true);
    assert.match(description(tree, button), /Recovery is required/);
  }
  for (const label of ["Stop engine", "Stop reading", "Stop stimulation"]) {
    const button = actionButton(tree, label);
    assert.equal(button.props.disabled, false);
    assert.equal(button.props["aria-describedby"], label === "Stop reading" ? "acquisition-read-help" : undefined);
  }
});

test("a ready trial can run without acquisition streaming or an unavailable-action hint", () => {
  const tree = render({ acquisition: { connected: false, streaming: false } });
  assert.equal(triggerButton(tree).props.disabled, false);
  assert.equal(triggerButton(tree).props["aria-describedby"], undefined);
  assert.equal(elements(tree).some((element) => element.props.id === "trial-action-reasons"), false);
});
