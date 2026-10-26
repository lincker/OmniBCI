/** Results filters apply only to the retained buffer and keep source values inspectable. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { TrialResults } = await server.ssrLoadModule("/src/TrialResults.tsx");
const { translate } = await server.ssrLoadModule("/src/i18n.tsx");
const english = { language: "en", t: (key, params) => translate("en", key, params) };
const trial = (trial_number, changes = {}) => ({ generation: "engine-1", trial_number,
  target_id: 2, event_id: 8, confidence: null, forced: false,
  produced_at: "2026-01-01T00:00:00Z", config_revision: "revision-1", ...changes });

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  return React.isValidElement(tree) ? [tree, ...elements(tree.props.children)] : [];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (React.isValidElement(tree)) return text(tree.props.children);
  return tree == null || typeof tree === "boolean" ? "" : String(tree);
}
function rows(tree) {
  return elements(elements(tree).find((item) => item.type === "tbody")).filter((item) => item.type === "tr");
}
function harness(props) {
  const hooks = [];
  let cursor = 0, tree;
  const dispatcher = {
    useContext: () => english,
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (value) => { hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value; }];
    },
  };
  return {
    render(next = {}) {
      Object.assign(props, next);
      const current = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
      const previous = current.current;
      current.current = dispatcher; cursor = 0;
      try { tree = TrialResults(props); } finally { current.current = previous; }
      return tree;
    },
    change(label, value) {
      const input = elements(tree).find((item) => item.props["aria-label"] === label);
      assert.ok(input, `Missing ${label}`); input.props.onChange({ target: { value } });
      return this.render();
    },
    clear() {
      const button = elements(tree).find((item) => item.type === "button" && text(item) === "Clear filters");
      assert.ok(button); button.props.onClick(); return this.render();
    },
  };
}

test("confidence preserves missing, zero and source precision instead of rounding", () => {
  const h = harness({ trials: [trial(1), trial(2, { confidence: 0 }), trial(3, { confidence: 0.00004 })] });
  const rendered = rows(h.render());
  assert.equal(rendered.length, 3);
  const confidence = (row) => elements(row).filter((item) => item.type === "td")[3];
  assert.equal(text(confidence(rendered[0])), "0.00004");
  assert.equal(text(confidence(rendered[1])), "0");
  assert.equal(text(confidence(rendered[2])), "—");
});

test("time is converted to UTC with the original timestamp available", () => {
  const original = "2026-01-01T08:30:01.123456+08:00";
  const h = harness({ trials: [trial(1, { produced_at: original })] });
  const tree = h.render();
  assert.match(text(tree), /2026-01-01 00:30:01\.123456/);
  assert.ok(elements(tree).some((item) => item.props.title === original));
  assert.doesNotMatch(text(h.render({ trials: [trial(1, { produced_at: "invalid" })] })), /Invalid Date/);
  assert.doesNotMatch(text(h.render({ trials: [trial(1, { produced_at: "2026-01-01T08:30:01" })] })), /2026-01-01/,
    "A timestamp without a timezone must not be presented as UTC");
});

test("scope explains the retained buffer, generation and newest-first ordering", () => {
  const h = harness({ trials: [trial(1), trial(2)], generation: "engine-1" });
  const tree = h.render();
  assert.match(text(tree), /latest 200/);
  assert.match(text(tree), /not a complete history/);
  assert.match(text(tree), /Generation: engine-1/);
  assert.match(text(tree), /Showing 2 of 2 retained results/);
  assert.deepEqual(rows(tree).map((row) => row.key), ["engine-1:2", "engine-1:1"]);
  const region = elements(tree).find((item) => item.props.role === "region");
  assert.equal(region.props.tabIndex, 0);
});

test("text and forced filters combine, count only visible rows and clear without changing the buffer", () => {
  const data = [trial(1), trial(2, { config_revision: "revision-2", forced: true }), trial(3, { target_id: 4, forced: true })];
  const h = harness({ trials: data });
  h.render();
  assert.equal(rows(h.change("Search retained results", "revision-2")).length, 1);
  assert.equal(rows(h.change("Forced result filter", "unforced")).length, 0);
  assert.match(text(h.render()), /No retained results match these filters/);
  assert.doesNotMatch(text(h.render()), /No trial results yet/);
  const restored = h.clear();
  assert.equal(rows(restored).length, 3);
  assert.equal(data.length, 3);
  assert.equal(rows(h.change("Search retained results", "   ")).length, 3);
});

test("empty and failed refresh states do not imply historical completeness or fresh data", () => {
  const h = harness({ trials: [], generation: null });
  assert.match(text(h.render()), /No trial results yet/);
  assert.match(text(h.render()), /latest 200/);
  const tree = h.render({ trials: [trial(1)], generation: "engine-1", stale: true });
  assert.match(text(tree), /last successful/);
  assert.equal(rows(tree).length, 1);
});
