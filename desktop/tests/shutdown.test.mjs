/** Exercise production shutdown code with native dialogs controlled. */
import assert from "node:assert/strict";
import { test, after, beforeEach } from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const temporary = await mkdtemp(path.join(tmpdir(), "omnibci-shutdown-tests-"));
after(async () => { delete globalThis.__desktopDialogs; await rm(temporary, { recursive: true, force: true }); });
const hooks = { responses: [], messages: [], };
globalThis.__desktopDialogs = hooks;
hooks.fetch = async url => {
  if (url.endsWith("/system/info")) return Response.json({ success: true, data: {
    desktop_api_version: 1, app_version: "1.0.0", instance_id: "backend", auth_enabled: true,
  } });
  assert.fail(`Unexpected backend request: ${url}`);
};
await build({ entryPoints: [fileURLToPath(new URL("../src/main.ts", import.meta.url))],
  outfile: path.join(temporary, "main.cjs"), platform: "node", format: "cjs", bundle: true,
  plugins: [{ name: "native-dialog-fixture", setup(builder) {
    builder.onResolve({ filter: /^electron$/ }, () => ({ path: "electron", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const hooks = globalThis.__desktopDialogs;
      export const app = { setName() {}, setPath() {}, requestSingleInstanceLock() { return false; }, quit() {} };
      export const protocol = { registerSchemesAsPrivileged() {} };
      export const dialog = {
        async showMessageBox(_window, options) {
          hooks.messages.push(options);
          if (!hooks.responses.length) throw new Error('Unexpected confirmation: ' + options.message);
          return { response: hooks.responses.shift() };
        },
      };
      export const net = { fetch: (...args) => hooks.fetch(...args) };
      export const BrowserWindow = class {};
      export const ipcMain = {};
      export const session = {};
      export const shell = {};
    `, loader: "js" }));
    builder.onLoad({ filter: /[\\/]src[\\/]main\.ts$/ }, async args => ({
      contents: await readFile(args.path, "utf8") + `
        export const fixture = { connection, disconnect,
          setLanguage(value) { language = value; },
          reset(owned) { language = "en"; backend = owned; window = {}; editor = { dirty: false, busy: false }; },
          getBackend() { return backend; },
        };
      `, loader: "ts",
    }));
  } }],
});
const { fixture } = createRequire(import.meta.url)(path.join(temporary, "main.cjs"));
let owned;
beforeEach(async () => {
  hooks.responses = []; hooks.messages = [];
  owned = {
    calls: [], exited: false,
    async command(op) { this.calls.push(op); return { ok: true }; },
    async waitForExit() { this.exited = true; },
    async force() { this.calls.push("force"); this.exited = true; },
  };
  fixture.reset(owned);
  await fixture.connection.connect("local", "http://127.0.0.1:49201", "test-credential");
});

test("keeping the window after an incomplete stop reports errors and permits another attempt", async () => {
  hooks.responses.push(1, 0);
  owned.command = async op => { owned.calls.push(op); return op === "prepare_shutdown"
    ? { ok: false, errors: ["Stimulation acknowledgement was not received"] } : { ok: true }; };
  assert.equal(await fixture.disconnect(), false);
  assert.match(hooks.messages.at(-1).detail, /Stimulation acknowledgement/);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "resume"]);
  assert.equal(fixture.connection.info.status, "ready");
});

test("confirmed shutdown waits for the owned backend to exit before disconnecting", async () => {
  hooks.responses.push(1);
  assert.equal(await fixture.disconnect(), true);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "shutdown"]);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});

test("final shutdown errors are reported after exit and release stale backend ownership", async () => {
  hooks.responses.push(1);
  owned.command = async op => { owned.calls.push(op); return op === "shutdown"
    ? { ok: false, errors: ["Device stop not confirmed"] } : { ok: true }; };
  await assert.rejects(fixture.disconnect(), /Device stop not confirmed/);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});

test("an interrupted final reply still waits for exit and releases backend ownership", async () => {
  hooks.responses.push(1);
  owned.command = async op => {
    owned.calls.push(op);
    if (op === "shutdown") throw new Error("Control pipe closed");
    return { ok: true };
  };
  await assert.rejects(fixture.disconnect(), /Control pipe closed/);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});


test("native shutdown confirmations follow Chinese without changing cancellation behavior", async () => {
  fixture.setLanguage("zh");
  hooks.responses.push(0);
  assert.equal(await fixture.disconnect(), false);
  assert.equal(hooks.messages[0].message, "停止本地后端？");
  assert.deepEqual(hooks.messages[0].buttons, ["取消", "停止并继续"]);
  assert.deepEqual(owned.calls, []);
  assert.equal(fixture.connection.info.status, "ready");
});

test("successful shutdown needs no result export or extra API request", async () => {
  hooks.responses.push(1);
  assert.equal(await fixture.disconnect(), true);
  assert.equal(hooks.messages.length, 1);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "shutdown"]);
});
