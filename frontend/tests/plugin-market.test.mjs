/** Extensions navigation and exact package request contracts. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const { I18nProvider } = await server.ssrLoadModule("/src/i18n.tsx");
const { PluginMarket } = await server.ssrLoadModule("/src/PluginMarket.tsx");
const { pluginMarketApi } = await server.ssrLoadModule("/src/api/plugin-market.ts");
globalThis.window = { location: { hash: "#extensions" }, localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: window.localStorage });
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test("Extensions hash opens the library without a routine configuration status bar", () => {
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(App)));
  assert.match(html, /aria-label="Extensions"[^>]*aria-current="page"/);
  assert.match(html, /<h1>Installed plugins<\/h1>/);
  assert.doesNotMatch(html, /aria-label="Configuration status"|Configuration details/);
  assert.match(html, /aria-label="Plugin library"/);
  assert.match(html, /Open Setup/);
  assert.doesNotMatch(html, /<h1>New profile<\/h1>/);
});

test("an unsettled runtime blocks installation while the library remains readable", () => {
  const html = render(PluginMarket, { runtimeBusy: true });
  assert.match(html, /Package changes are temporarily unavailable/);
  assert.match(html, /<button disabled="">[\s\S]*?Install ZIP…<\/button>/);
  assert.match(html, /aria-label="Search plugins"/);
  const ready = render(PluginMarket, { runtimeBusy: false });
  const installButton = [...ready.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
    .find(([, , body]) => body.includes("Install ZIP…"));
  assert.ok(installButton);
  assert.doesNotMatch(installButton[1], /disabled/);
});

test("package actions send exact identities and local ZIP bytes to the backend", async () => {
  const requests = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    requests.push({ path, ...options });
    return new Response(JSON.stringify({ success: true, message: "ok", data: {} }), { status: 200 });
  };
  try {
    const archive = new File(["archive-bytes"], "plugin.zip", { type: "application/zip" });
    await pluginMarketApi.upload(archive);
    await pluginMarketApi.install({ package_id: "Example.multi", version: "2.0.0", download_url: "https://untrusted.example/ignored.zip" });
    await pluginMarketApi.setEnabled({ package_id: "Example.multi", version: "2.0.0" }, true);
    assert.equal(requests[0].path, "/api/v1/plugin-packages/install");
    assert.equal(requests[0].headers["Content-Type"], "application/zip");
    assert.equal(requests[0].body, archive);
    assert.equal(requests[1].path, "/api/v1/market/install");
    assert.deepEqual(JSON.parse(requests[1].body), { package_id: "Example.multi", version: "2.0.0" });
    assert.equal(requests[2].path, "/api/v1/plugin-packages/Example.multi/2.0.0/enabled");
    assert.deepEqual(JSON.parse(requests[2].body), { enabled: true });
  } finally { globalThis.fetch = previous; }
});

test("creating a version profile carries its reviewed source revision without applying it", async () => {
  const requests = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    requests.push({ path, ...options });
    return new Response(JSON.stringify({ success: true, message: "ok", data: {} }), { status: 200 });
  };
  try {
    const body = { source_profile_id: "source-profile", target_version: "2.0.0", name: "Updated decoder", expected_source_revision: 3 };
    await pluginMarketApi.createVersionProfile(body);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/api/v1/plugin-packages/version-profiles");
    assert.deepEqual(JSON.parse(requests[0].body), body);
  } finally { globalThis.fetch = previous; }
});
