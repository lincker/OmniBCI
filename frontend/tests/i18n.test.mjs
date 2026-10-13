/** Locale selection, persistence, and interpolation are shared by web and desktop. */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
after(() => server.close());
const { I18nProvider, useI18n, translate } = await server.ssrLoadModule("/src/i18n.tsx");
const originalWindow = globalThis.window, originalDocument = globalThis.document;
afterEach(() => { globalThis.window = originalWindow; globalThis.document = originalDocument; });

function Probe() {
  const { language, t } = useI18n();
  return React.createElement("p", { lang: language }, t("app.settings"));
}
function renderLanguage(stored, browserLanguage = "en-US") {
  globalThis.window = { localStorage: { getItem: () => stored }, navigator: { language: browserLanguage } };
  return renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(Probe)));
}

test("saved language wins, otherwise Chinese locales use Chinese and other locales use English", () => {
  assert.match(renderLanguage("en", "zh-CN"), /lang="en">Settings/);
  assert.match(renderLanguage("zh", "en-US"), /lang="zh">设置/);
  for (const locale of ["zh-CN", "zh-TW", "zh-HK", "ZH"]) {
    assert.match(renderLanguage(null, locale), /lang="zh">设置/);
  }
  for (const stored of [null, "fr", "", "__proto__"]) {
    assert.match(renderLanguage(stored, "fr-FR"), /lang="en">Settings/);
  }
});

test("server rendering and inaccessible storage still have a usable locale", () => {
  delete globalThis.window;
  assert.match(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(Probe))), /Settings/);
  globalThis.window = { get localStorage() { throw new Error("Denied"); }, navigator: { language: "zh-CN" } };
  assert.match(renderToStaticMarkup(React.createElement(I18nProvider, null, React.createElement(Probe))), /设置/);
});

function providerHarness() {
  const hooks = [], effects = [];
  let cursor = 0;
  const memo = (factory, deps) => {
    const index = cursor++;
    if (!hooks[index] || deps.some((value, i) => !Object.is(hooks[index].deps[i], value))) {
      hooks[index] = { value: factory(), deps };
    }
    return hooks[index].value;
  };
  const dispatcher = {
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (value) => { hooks[index].value = value; }];
    },
    useCallback: (callback, deps) => memo(() => callback, deps),
    useMemo: memo,
    useEffect: (effect, deps) => memo(() => { effects.push(effect); }, deps),
  };
  return () => {
    const internal = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher;
    const previous = internal.current;
    internal.current = dispatcher; cursor = 0;
    let tree;
    try { tree = I18nProvider({ children: null }); } finally { internal.current = previous; }
    effects.splice(0).forEach((effect) => effect());
    return tree.props.value;
  };
}

for (const blockedStorage of [false, true]) {
  test(`switching updates text, page metadata, and native dialogs with storage blocked=${blockedStorage}`, () => {
    const saved = new Map(), nativeLanguages = [];
    globalThis.window = { navigator: { language: "en-US" },
      localStorage: {
        getItem: (key) => saved.get(key),
        setItem(key, value) { if (blockedStorage) throw new Error("Denied"); saved.set(key, value); },
      },
      omnibciDesktop: { setLanguage: (language) => nativeLanguages.push(language) },
    };
    globalThis.document = { documentElement: { lang: "" }, title: "" };
    const render = providerHarness();
    const first = render();
    assert.equal(first.t("app.settings"), "Settings");
    first.setLanguage("zh");
    const chinese = render();
    assert.equal(chinese.t("app.settings"), "设置");
    assert.equal(chinese.t("app.channels", { count: 8 }), "8 个通道");
    assert.equal(document.documentElement.lang, "zh-CN");
    assert.equal(document.title, "OmniBCI 控制台");
    assert.deepEqual(nativeLanguages, ["en", "zh"]);
    if (!blockedStorage) {
      assert.equal(saved.get("omnibci.language"), "zh");
      assert.equal(providerHarness()().language, "zh", "A fresh mount restores the selection");
    }
    chinese.setLanguage("en");
    assert.equal(render().t("app.channels", { count: 8 }), "8 channels");
    assert.equal(document.documentElement.lang, "en");
    assert.equal(document.title, "OmniBCI Console");
  });
}

test("each locale has exactly the same keys and interpolation parameters", async () => {
  const seen = new Set();
  for (const group of ["shell", "profiles", "runtime", "setup", "guide"]) {
    const { en, zh } = await server.ssrLoadModule(`/src/locales/${group}.ts`);
    assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort(), group);
    for (const [key, english] of Object.entries(en)) {
      assert.equal(seen.has(key), false, `Duplicate translation key: ${key}`); seen.add(key);
      assert.ok(english.trim() && zh[key].trim(), key);
      const parameters = (value) => [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
      assert.deepEqual(parameters(zh[key]), parameters(english), key);
      const values = Object.fromEntries(parameters(english).map((name) => [name, "example"]));
      assert.doesNotMatch(translate("zh", key, values), /\{\w+\}/, key);
    }
  }
});

test("known plugin metadata is translated while unknown and user-defined names are preserved", async () => {
  const { translatePluginName, translateControlLabel } = await server.ssrLoadModule("/src/i18n.tsx");
  const chinese = (key, params) => translate("zh", key, params);
  assert.equal(translatePluginName("file_reader", "File Playback", chinese), "文件回放");
  assert.equal(translateControlLabel("n_trials", "Trial count", chinese), "试次数");
  for (const identifier of ["custom_plugin", "constructor", "toString", "__proto__"]) {
    assert.equal(translatePluginName(identifier, "My lab plugin", chinese), "My lab plugin");
    assert.equal(translateControlLabel(identifier, "My custom control", chinese), "My custom control");
  }
});

test("schema labels and invalid input guidance use the selected language", async () => {
  const { schemaErrors, SchemaForm } = await server.ssrLoadModule("/src/SchemaForm.tsx");
  const schema = { type: "object", required: ["sample_rate"], properties: {
    sample_rate: { type: "integer", minimum: 1, title: "Sample rate (Hz)" },
  } };
  const chinese = (key, params) => translate("zh", key, params);
  const errors = schemaErrors(schema, { sample_rate: 0 }, undefined, chinese);
  assert.match(errors.join(" "), /采样率/);
  assert.doesNotMatch(errors.join(" "), /must|minimum|Parameters|Sample rate/);
  renderLanguage("zh");
  const html = renderToStaticMarkup(React.createElement(I18nProvider, null,
    React.createElement(SchemaForm, { schema, value: { sample_rate: 250 }, onChange: () => {} })));
  assert.match(html, /采样率/);
  assert.doesNotMatch(html, /Sample rate/);
});
