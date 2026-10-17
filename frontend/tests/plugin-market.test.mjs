/** Extensions navigation and exact package request contracts. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readFile } from "node:fs/promises";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { default: App } = await server.ssrLoadModule("/src/App.tsx");
const { I18nProvider } = await server.ssrLoadModule("/src/i18n.tsx");
const { PluginMarket } = await server.ssrLoadModule("/src/PluginMarket.tsx");
const { DecoderDeveloperGuide } = await server.ssrLoadModule("/src/DecoderDeveloperGuide.tsx");
const { pluginMarketApi } = await server.ssrLoadModule("/src/api/plugin-market.ts");
globalThis.window = { location: { hash: "#extensions" }, localStorage: { getItem: () => "en" }, navigator: { language: "en" } };
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: window.localStorage });
const render = (component, props) => renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(component, props)));

test("Extensions hash opens the library without a routine configuration status bar", () => {
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(App)));
  assert.match(html, /aria-label="Extensions"[^>]*aria-current="page"/);
  assert.match(html, /<h1>Installed plugins<\/h1>/);
  assert.doesNotMatch(html, /aria-label="Configuration status"|Configuration details/);
  assert.match(html, /aria-label="Plugin library"/);
  assert.match(html, /Open Setup/);
  assert.match(html, /Decoder developer guide/);
  assert.doesNotMatch(html, /<h1>New profile<\/h1>/);
});

test("offline Decoder guide downloads the executable starter and complete authoring guide", async () => {
  const html = render(DecoderDeveloperGuide);
  const downloads = [...html.matchAll(/href="data:text\/plain;charset=utf-8,([^"]*)" download="([^"]*)"/g)];
  const expected = new Map([
    ["manifest.toml", "../../examples/decoder_plugin/manifest.toml"],
    ["decoder.py", "../../examples/decoder_plugin/decoder.py"],
    ["default.schema.json", "../../examples/decoder_plugin/default.schema.json"],
    ["README.md", "../../examples/decoder_plugin/README.md"],
    ["decoder-plugin-development.md", "../../docs/decoder-plugin-development.md"],
  ]);
  assert.equal(downloads.length, expected.size);
  for (const [, encoded, name] of downloads) {
    assert.equal(decodeURIComponent(encoded.replaceAll("&#x27;", "'")), await readFile(new URL(expected.get(name), import.meta.url), "utf8"));
  }
  assert.match(html, /required_names/);
  assert.match(html, /context.input_signal.channels/);
  assert.match(html, /decode\(window, latest_samples\)/);
  assert.match(html, /latest_samples<\/code> contains the most recently appended samples and does not pass through those window filters/);
  assert.doesNotMatch(html, /href="https?:/);
});

test("Settings exposes Logs alongside General and retains console preferences", () => {
  const previousHash = window.location.hash;
  window.location.hash = "#general";
  try {
    const html = renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(App)));
    assert.match(html, /aria-label="Settings"[^>]*aria-current="page"/);
    assert.match(html, /<h1>General settings<\/h1>/);
    assert.match(html, />Logs<\/span>/);
    assert.match(html, /aria-label="Interface language"/);
    assert.match(html, /aria-label="Appearance"/);
    assert.doesNotMatch(html, /Advanced|Active application configuration|Edit application configuration|Save as/);
  } finally { window.location.hash = previousHash; }
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
    assert.equal(new Headers(requests[0].headers).get("Content-Type"), "application/zip");
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
