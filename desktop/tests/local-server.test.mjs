import assert from "node:assert/strict";
import { test, after } from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const temporary = await mkdtemp(path.join(tmpdir(), "omnibci-child-tests-"));
after(() => rm(temporary, { recursive: true, force: true }));
const fixture = path.join(temporary, "server.cjs");
await writeFile(fixture, `
const { createInterface } = require('node:readline');
const { spawn } = require('node:child_process');
const mode = process.argv[2];
let descendant;
const lines = createInterface({ input: process.stdin });
const emit = (message, done) => process.stdout.write(JSON.stringify(message) + '\\n', done);
lines.on('line', line => {
  const message = JSON.parse(line);
  if (message.op === 'bootstrap') {
    if (mode === 'descendant') descendant = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    emit({ event: 'ready', launch_id: message.launch_id, instance_id: String(descendant?.pid ?? 'fixture'), url: 'http://127.0.0.1:49321' });
  } else if (mode === 'broken') process.stdout.write('invalid control output\\n');
  else {
    if (mode === 'progress' && message.op === 'prepare_shutdown') emit({ event: 'shutdown_progress', phase: 'draining_acquisition' });
    emit({ event: 'result', id: message.id, ok: true }, () => { if (message.op === 'shutdown') process.exit(0); });
  }
});
lines.on('close', () => process.exit(0));
`);
await build({ entryPoints: [fileURLToPath(new URL("../src/local-server.ts", import.meta.url))],
  outfile: path.join(temporary, "local-server.cjs"), platform: "node", format: "cjs", bundle: true,
  plugins: [{ name: "controlled-child-fixture", setup(builder) {
    builder.onResolve({ filter: /^node:child_process$/ }, () => ({ path: "child-fixture", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const { spawn: realSpawn } = require('child_process');
      export function spawn(mode, _args, options) {
        return mode === 'missing' ? realSpawn(${JSON.stringify(path.join(temporary, "missing-executable"))}, [], options)
          : realSpawn(process.execPath, [${JSON.stringify(fixture)}, mode], options);
      }`, loader: "js" }));
  } }],
});
const { LocalServer } = createRequire(import.meta.url)(path.join(temporary, "local-server.cjs"));

test("a failed spawn rejects startup and still settles ownership exit", { timeout: 5000 }, async () => {
  const server = new LocalServer();
  await assert.rejects(server.start("missing", temporary, false, () => {}), /ENOENT/);
  await server.waitForExit();
  await server.force();
});

test("final protocol replies are consumed before child close rejects pending commands", { timeout: 5000 }, async () => {
  const server = new LocalServer();
  try {
    await server.start("normal", temporary, false, () => {});
    assert.equal((await server.command("prepare_shutdown")).ok, true);
    assert.equal((await server.command("resume")).ok, true);
    assert.equal((await server.command("shutdown")).ok, true);
    await server.waitForExit();
  } finally { await server.force(); }
});

test("malformed control output after ready rejects commands and closes the parent pipe", { timeout: 5000 }, async () => {
  const server = new LocalServer();
  let failure;
  server.on("protocol-error", error => { failure = error; });
  try {
    await server.start("broken", temporary, false, () => {});
    await assert.rejects(server.command("prepare_shutdown"));
    await server.waitForExit();
    assert.ok(failure instanceof Error);
  } finally { await server.force(); }
});

test("private shutdown progress exposes a readable stage and resets when returning to the window", { timeout: 5000 }, async () => {
  const server = new LocalServer();
  try {
    await server.start("progress", temporary, false, () => {});
    assert.equal((await server.command("prepare_shutdown")).ok, true);
    assert.equal(server.shutdownStage, "Draining acquisition and processing");
    assert.equal((await server.command("resume")).ok, true);
    assert.equal(server.shutdownStage, "Waiting for server acknowledgement");
  } finally { await server.force(); }
});

test("a normal parent exit cleans up descendants in its own process group", { timeout: 5000, skip: process.platform === "win32" }, async () => {
  const server = new LocalServer();
  let pid;
  try {
    const ready = await server.start("descendant", temporary, false, () => {});
    pid = Number(ready.instance_id);
    process.kill(pid, 0);
    assert.equal((await server.command("shutdown")).ok, true);
    await server.waitForExit();
    for (let attempt = 0; attempt < 40; attempt++) {
      try { process.kill(pid, 0); } catch (error) { assert.equal(error.code, "ESRCH"); return; }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    assert.fail("Owned descendant survived server exit");
  } finally {
    await server.force();
    if (pid) { try { process.kill(pid, "SIGKILL"); } catch { /* Already stopped. */ } }
  }
});
