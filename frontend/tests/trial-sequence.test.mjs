import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { MAX_TRIALS, parseTrialSequence, readTrialControls } = await server.ssrLoadModule("/src/plugin-controls.ts");
const targetControl = { field: "trial_params", type: "int", min: 0, max: 40, default: 1 };

test("the default full-target range includes each target once and excludes uncued target zero", () => {
  assert.deepEqual(parseTrialSequence("1-40", targetControl), Array.from({ length: 40 }, (_, index) => index + 1));
});

test("sequences preserve order and duplicates with comma or whitespace separators", () => {
  assert.deepEqual(parseTrialSequence(" 3, 1 - 3\n8\t2,2  0 ", targetControl), [3, 1, 2, 3, 8, 2, 2, 0]);
  assert.deepEqual(parseTrialSequence("40-40", targetControl), [40]);
});

test("zero is accepted only when the control bounds permit it", () => {
  assert.deepEqual(parseTrialSequence("0,1", targetControl), [0, 1]);
  assert.throws(() => parseTrialSequence("0,1", { ...targetControl, min: 1 }));
  assert.throws(() => parseTrialSequence("40-41", targetControl));
  assert.throws(() => parseTrialSequence("41", targetControl));
});

test("malformed syntax, fractions, descending ranges and unsafe integers are rejected", () => {
  for (const sequence of ["", "   ", ",1", "1,", "1,,2", "1, ,2", "1;2", "1.5", "1e2", "+1", "-1",
    "3-1", "1-", "1--3", "1-2-3", "a1", "1a", "9007199254740992", "1-9007199254740992"]) {
    assert.throws(() => parseTrialSequence(sequence, { field: "trial_params", type: "int" }), sequence);
  }
});

test("expanded sequences are bounded before expanding large ranges", () => {
  const unbounded = { field: "trial_params", type: "int" };
  assert.equal(MAX_TRIALS, 10000);
  assert.equal(parseTrialSequence("1-10000", unbounded).length, MAX_TRIALS);
  assert.throws(() => parseTrialSequence("0-10000", unbounded));
  assert.throws(() => parseTrialSequence("0-9007199254740991", unbounded));
  assert.throws(() => parseTrialSequence("1-9999,1-2", unbounded));
  assert.throws(() => parseTrialSequence(Array(MAX_TRIALS + 1).fill("1").join(","), unbounded));
  assert.deepEqual(parseTrialSequence("9007199254740991", unbounded), [Number.MAX_SAFE_INTEGER]);
});

test("default sequence metadata must describe a valid integer trial parameter sequence", () => {
  const controls = { target: { ...targetControl, default_sequence: "1-40" } };
  const parsed = readTrialControls(controls);
  assert.deepEqual(parsed, controls);
  assert.notEqual(parsed.target, controls.target);
  for (const changes of [
    { default_sequence: 1 }, { default_sequence: "" }, { default_sequence: "1-41" },
    { default_sequence: "1,,2" }, { default_sequence: "1-10001", max: 10001 },
    { field: "n_trials" }, { type: "float" }, { type: undefined },
  ]) {
    assert.throws(() => readTrialControls({ target: { ...controls.target, ...changes } }));
  }
});

test("legacy numeric controls remain valid without default sequence metadata", () => {
  assert.deepEqual(readTrialControls(undefined), {});
  const controls = {
    count: { field: "n_trials", type: "int", default: 1, min: 1 },
    target: targetControl,
  };
  assert.deepEqual(readTrialControls(controls), controls);
  assert.deepEqual(readTrialControls({ value: { field: "trial_params", type: "float", min: 0.5, max: 2.5 } }),
    { value: { field: "trial_params", type: "float", min: 0.5, max: 2.5 } });
});
