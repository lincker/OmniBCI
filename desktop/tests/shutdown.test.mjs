/** Exercise production shutdown and export code with native dialogs controlled. */
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
const hooks = { responses: [], messages: [], save: { canceled: true }, listError: false };
globalThis.__desktopDialogs = hooks;
const record = { session_id: "session-1", trial_count: 3, state: "ended", ended_at: "2026-10-04T12:00:00Z" };
hooks.fetch = async url => {
  if (url.endsWith("/system/info")) return Response.json({ success: true, data: {
    desktop_api_version: 1, app_version: "1.0.0", instance_id: "backend", auth_enabled: true,
  } });
  if (url.endsWith("/sessions")) return hooks.listError ? new Response(null, { status: 503 })
    : Response.json({ success: true, data: { sessions: [record] } });
  return Response.json({ sessions: [record] });
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
        async showSaveDialog() { return hooks.save; },
      };
      export const net = { fetch: (...args) => hooks.fetch(...args) };
      export const BrowserWindow = class {};
      export const ipcMain = {};
      export const session = {};
      export const shell = {};
    `, loader: "js" }));
    builder.onLoad({ filter: /[\\/]src[\\/]main\.ts$/ }, async args => ({
      contents: await readFile(args.path, "utf8") + `
        export const fixture = { connection, disconnect, saveResults,
          reset(owned) { backend = owned; window = {}; editor = { dirty: false, busy: false }; exported.clear(); },
          getBackend() { return backend; },
        };
      `, loader: "ts",
    }));
  } }],
});
const { fixture } = createRequire(import.meta.url)(path.join(temporary, "main.cjs"));
let owned;
beforeEach(async () => {
  hooks.responses = []; hooks.messages = []; hooks.save = { canceled: true }; hooks.listError = false;
  owned = {
    calls: [], exited: false,
    async command(op) { this.calls.push(op); return { ok: true, ...(op === "prepare_shutdown" ? { sessions: [record] } : {}) }; },
    async waitForExit() { this.exited = true; },
    async force() { this.calls.push("force"); this.exited = true; },
  };
  fixture.reset(owned);
  await fixture.connection.connect("local", "http://127.0.0.1:49201", "test-credential");
});

test("canceling the native export dialog restores the stopped backend without shutting it down", async () => {
  hooks.responses.push(1, 0);
  assert.equal(await fixture.disconnect(), false);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "resume"]);
  assert.equal(fixture.connection.info.status, "ready");
  assert.equal(fixture.getBackend(), owned);
  assert.equal(owned.exited, false);
});

test("a failed save returns to the stopped backend and does not mark results exported", async () => {
  hooks.responses.push(1, 0);
  hooks.save = { canceled: false, filePath: path.join(temporary, "missing-directory", "results.json") };
  await assert.rejects(fixture.disconnect(), /ENOENT/);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "resume"]);
  assert.equal(fixture.connection.info.status, "ready");
  hooks.responses.push(1, 2);
  assert.equal(await fixture.disconnect(), false);
  assert.match(hooks.messages.at(-1).message, /1 session.*unexported/);
});

test("a session-list failure after stopping restores read and control admission", async () => {
  hooks.responses.push(1);
  hooks.listError = true;
  owned.command = async op => { owned.calls.push(op); return { ok: true }; };
  await assert.rejects(fixture.disconnect(), /Could not read session/);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "resume"]);
  assert.equal(fixture.connection.info.status, "ready");
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

test("discarding final results waits for the owned backend to exit before disconnecting", async () => {
  hooks.responses.push(1, 1);
  assert.equal(await fixture.disconnect(), true);
  assert.deepEqual(owned.calls, ["prepare_shutdown", "shutdown"]);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});

test("final shutdown errors are reported after exit and release stale backend ownership", async () => {
  hooks.responses.push(1, 1);
  owned.command = async op => { owned.calls.push(op); return op === "shutdown"
    ? { ok: false, errors: ["Device stop not confirmed"] } : { ok: true, sessions: [record] }; };
  await assert.rejects(fixture.disconnect(), /Device stop not confirmed/);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});

test("an interrupted final reply still waits for exit and releases backend ownership", async () => {
  hooks.responses.push(1, 1);
  owned.command = async op => {
    owned.calls.push(op);
    if (op === "shutdown") throw new Error("Control pipe closed");
    return { ok: true, sessions: [record] };
  };
  await assert.rejects(fixture.disconnect(), /Control pipe closed/);
  assert.equal(owned.exited, true);
  assert.equal(fixture.getBackend(), undefined);
  assert.equal(fixture.connection.info.status, "disconnected");
});
