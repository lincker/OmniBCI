/** Logs stay within Settings and render backend content as inert text. */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { BackendLogs } = await server.ssrLoadModule("/src/BackendLogs.tsx");
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const { I18nProvider, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const originalWindow = globalThis.window;
afterEach(() => { globalThis.window = originalWindow; });

function renderLogs(data, error = null) {
  const dispatcher = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
  const previous = dispatcher.current;
  const values = [data, error];
  dispatcher.current = {
    useContext: () => ({ t: (key) => translate("en", key) }),
    useState: () => [values.shift(), () => {}],
    useRef: (value) => ({ current: value }),
    useCallback: (callback) => callback,
    useEffect: () => {},
  };
  let tree;
  try { tree = BackendLogs({ navigation: null }); }
  finally { dispatcher.current = previous; }
  return renderToStaticMarkup(tree);
}

test("a direct Logs URL selects Settings and exposes navigation back to General", () => {
  globalThis.window = {
    location: { hash: "#logs" }, navigator: { language: "en" },
    localStorage: { getItem: () => "en" },
  };
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(App)));
  assert.match(html, /aria-label="Settings"[^>]*aria-current="page"/);
  assert.match(html, /aria-label="Settings pages"/);
  assert.match(html, />General<\/span>/);
  assert.match(html, /<h1>Logs<\/h1>/);
  assert.match(html, /Refresh logs/);
});

test("log output escapes markup, preserves multiline text, and identifies truncated output", () => {
  const html = renderLogs({ filename: "backend.log", lines: ["<script>alert(1)</script>", "ERROR second line"], truncated: true });
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;\nERROR second line/);
  assert.match(html, /Earlier content is omitted/);
  assert.match(html, /backend.log/);
  assert.match(html, /tabindex="0" role="region" aria-label="Backend log output"/);
});

test("empty logs, loading, and a failed refresh have distinct states", () => {
  const empty = renderLogs({ filename: "backend.log", lines: [], truncated: false });
  assert.match(empty, /No log entries yet/);
  assert.doesNotMatch(empty, /Earlier content is omitted|Loading logs/);
  assert.match(renderLogs(null), /Loading logs/);
  const failed = renderLogs(null, new Error("unavailable"));
  assert.match(failed, /role="alert"/);
  assert.match(failed, /Logs could not be refreshed/);
  assert.match(failed, /Refresh logs/);
  assert.doesNotMatch(failed, /Loading logs|No log entries yet/);
});
