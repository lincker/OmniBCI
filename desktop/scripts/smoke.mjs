/** Exercise the actual Electron window, preload, network proxy and Python child. */
import assert from "node:assert/strict";
import { _electron } from "playwright";
import { mkdtemp, mkdir, readFile, readdir, access, writeFile, copyFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { build } from "esbuild";

const root = fileURLToPath(new URL("../", import.meta.url));
const options = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, values) => {
  if (value.startsWith("--")) pairs.push([value.slice(2), values[index + 1]]);
  return pairs;
}, []));
const packaged = Boolean(options.packaged);
let executable = options.executable && path.resolve(options.executable);
let python = options.python && path.resolve(options.python);
if (options.packaged) {
  const base = path.resolve(options.packaged);
  const candidates = process.platform === "darwin"
    ? (process.arch === "arm64" ? ["mac-arm64/OmniBCI.app/Contents/MacOS/OmniBCI"] : ["mac/OmniBCI.app/Contents/MacOS/OmniBCI", "mac-x64/OmniBCI.app/Contents/MacOS/OmniBCI"])
    : process.platform === "win32" ? ["win-unpacked/OmniBCI.exe"]
      : ["linux-unpacked/omnibci-desktop", "linux-unpacked/omnibci", "linux-unpacked/OmniBCI"];
  for (const candidate of candidates) {
    try { await access(path.join(base, candidate)); executable = path.join(base, candidate); break; } catch { /* Next target. */ }
  }
  if (!executable) throw new Error(`No packaged executable found under ${base}`);
  const resources = process.platform === "darwin" ? path.resolve(executable, "../../Resources") : path.join(path.dirname(executable), "resources");
  python = path.join(resources, "python", process.platform === "win32" ? "python.exe" : "bin/python3");
}
if (!python) throw new Error("Supply --python for development, or --packaged for a built app.");
const directory = await mkdtemp(path.join(tmpdir(), "OmniBCI smoke 数据 "));
const serverLogs = [];
for (const mode of ["local", "remote"]) {
  const configuration = path.join(directory, mode, "config");
  const logfile = path.join(directory, mode, "server.log");
  await mkdir(configuration, { recursive: true });
  await writeFile(path.join(configuration, "settings.toml"), `[LOGGING]\nfile_path = ${JSON.stringify(logfile)}\n`, "utf8");
  serverLogs.push(logfile);
}
const output = path.resolve(options.output ?? path.join(root, "artifacts/smoke"));
await mkdir(output, { recursive: true });
await build({ entryPoints: [path.join(root, "src/local-server.ts")], outfile: path.join(directory, "server.cjs"), bundle: true, platform: "node", format: "cjs" });
const { LocalServer } = createRequire(import.meta.url)(path.join(directory, "server.cjs"));
const remote = new LocalServer();
let application;
let stage = "launch";
const diagnostics = [];
const processOverrides = ["OMNIBCI_CONFIG_DIR", "OMNIBCI_PLUGIN_DIR"];
const originalEnvironment = Object.fromEntries(processOverrides.map(name => [name, process.env[name]]));
const timedFetch = (url, options = {}) => fetch(url, { ...options, signal: AbortSignal.timeout(10_000) });
async function bounded(operation, label, timeout = 45_000) {
  let timer;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeout / 1000} seconds`)), timeout);
    })]);
  } finally { clearTimeout(timer); }
}
// waitForFunction treats an async predicate's Promise as immediately truthy.
// Await observations in the driver and only accept an actual boolean true.
async function pollUntil(label, observe, accept, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  let observation;
  do {
    observation = await bounded(observe(), label, Math.min(15_000, Math.max(1, deadline - Date.now())));
    if (accept(observation) === true) return observation;
    await new Promise(resolve => setTimeout(resolve, 500));
  } while (Date.now() < deadline);
  throw new Error(`${label} timed out; last observation: ${JSON.stringify(observation)}`);
}
const localEnv = {
  ...process.env, OMNIBCI_DESKTOP_PYTHON: python,
  OMNIBCI_CONFIG_DIR: path.join(directory, "local/config"),
  OMNIBCI_PLUGIN_DIR: path.join(directory, "local/plugins"),
  OMNIBCI_DESKTOP_USER_DATA: path.join(directory, "local/desktop"),
};
delete localEnv.ELECTRON_RUN_AS_NODE;
delete localEnv.OMNIBCI_API_TOKEN;
delete localEnv.OMNIBCI_DESKTOP_DEV_URL;

try {
  application = await _electron.launch({
    ...(executable ? { executablePath: executable } : {}), args: packaged ? [] : [root],
    env: localEnv, cwd: directory, timeout: 60_000,
    chromiumSandbox: true,
  });
  application.process().stderr?.on("data", data => diagnostics.push(data.toString()));
  const page = await application.firstWindow();
  page.setDefaultTimeout(30_000);
  page.on("pageerror", error => diagnostics.push(error.stack ?? error.message));
  await page.getByRole("combobox").selectOption("en");
  await page.getByRole("heading", { name: "Connect your workspace" }).waitFor();
  const initial = await page.evaluate(() => window.omnibciDesktop.getConnection());
  assert.equal(initial.id, "");
  assert.equal(initial.status, "disconnected");
  assert.equal(await page.evaluate(() => typeof window.require), "undefined");
  const prefs = await application.evaluate(({ BrowserWindow }) => {
    const config = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences();
    return { sandbox: config.sandbox, contextIsolation: config.contextIsolation, nodeIntegration: config.nodeIntegration, webSecurity: config.webSecurity };
  });
  assert.deepEqual(prefs, { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true });
  await page.screenshot({ path: path.join(output, "connection.png") });

  // Native prompts are stubbed only by the test driver, never by the shipped app.
  await application.evaluate(({ dialog }) => {
    dialog.showMessageBox = async (_window, options) => ({ response: ["Discard changes", "Stop and continue", "Disconnect"]
      .map(label => options.buttons?.indexOf(label) ?? -1).find(index => index >= 0) ?? 0, checkboxChecked: false });
  });
  stage = "local connection";
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await page.getByRole("heading", { name: "Acquisition source" }).waitFor({ timeout: 60_000 });
  const local = await page.evaluate(() => window.omnibciDesktop.getConnection());
  assert.equal(local.kind, "local");
  const api = async (route, method = "GET", body) => page.evaluate(async ({ route, method, body }) => {
    const connection = await window.omnibciDesktop.getConnection();
    const response = await fetch("app://omnibci/api/v1" + route, {
      method, headers: { "X-OmniBCI-Connection-Id": connection.id, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(10_000),
    });
    return { status: response.status, body: await response.json() };
  }, { route, method, body });
  const serverInfo = await api("/server/info");
  assert.equal(serverInfo.status, 200);
  assert.equal(serverInfo.body.data.auth_enabled, true);
  assert.equal(serverInfo.body.data.desktop_api_version, 1);
  assert.equal((await api("/engine/status")).body.data.running, false);
  assert.equal((await timedFetch(local.url + "/api/v1/server/info")).status, 401);
  for (const route of ["/system/info", "/system/logs", "/backend/info", "/backend/logs", "/config/system", "/config/activation"]) {
    assert.equal((await api(route)).status, 404, route);
  }
  await page.waitForFunction(() => document.querySelector(".connection-open"), null, { timeout: 20_000 });
  await page.screenshot({ path: path.join(output, "local-workspace.png") });

  stage = "independent Engine startup";
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await page.getByRole("button", { name: "Start engine", exact: true }).click();
  await page.getByText("Engine running", { exact: true }).first().waitFor({ timeout: 60_000 });
  assert.deepEqual((await api("/engine/status")).body.data, {
    running: true, acquisition_alive: false, processor_alive: false,
    stimulation_connected: false, actuation_alive: false, actuation_connected: false,
  });

  stage = "file selection and current configuration";
  await page.getByRole("button", { name: "Setup", exact: true }).click();
  await page.getByRole("heading", { name: "Acquisition source", exact: true }).waitFor();
  // Input is supplied by the test, outside the installation. No fixture is shipped.
  const recording = path.join(directory, "subject 数据.cnt");
  await copyFile(path.resolve(root, "../tests/fixture/train.cnt"), recording);
  const recordingInput = page.getByLabel("Recording path", { exact: true });
  assert.equal(await recordingInput.inputValue(), "");
  await application.evaluate(({ dialog }, filePath) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [filePath] });
  }, recording);
  await page.getByRole("button", { name: "Browse…", exact: true }).click();
  await page.waitForFunction(({ id, value }) => document.getElementById(id)?.value === value,
    { id: await recordingInput.getAttribute("id"), value: recording });
  assert.equal((await api("/config/modules/acquisition")).body.data.revision, null);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await pollUntil("Current recording configuration", async () => {
    const response = await api("/config/modules/acquisition");
    return { status: response.status, path: response.body.data?.bindings.find(binding => binding.slot === "acquisition.source")?.parameters.data_file_path };
  }, current => current.status === 200 && current.path === recording);
  await pollUntil("Connected recording", async () => (await api("/acquisition/status")).body.data,
    current => current.connected === true && current.streaming === false);
  assert.equal((await api("/engine/status")).body.data.processor_alive, false);
  assert.deepEqual((await api("/config/plugin-profiles")).body.data, []);

  stage = "current Processing configuration";
  await page.getByRole("button", { name: "Signal Processing", exact: true }).click();
  await page.getByRole("heading", { name: "Decoder", exact: true }).waitFor();
  const decoder = (await api("/config/plugin-catalog/decoder")).body.data
    .find(item => item.plugin_type === "THUBCI_SSVEP_classic40CCA");
  assert.ok(decoder?.available, "The bundled CCA decoder must be available");
  await page.getByRole("combobox", { name: /^Plugin and version/ }).selectOption(JSON.stringify([
    decoder.selection.package_id, decoder.selection.contribution_id, decoder.selection.version,
  ]));
  await page.getByRole("button", { name: "Epoching", exact: true }).click();
  await page.getByRole("heading", { name: "Epoch boundary detector", exact: true }).waitFor();
  await page.getByRole("combobox", { name: /^Plugin and version/ }).locator("option:checked").filter({ hasText: "Default Epoch Detector" }).waitFor({ state: "attached" });

  stage = "real file replay";
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await page.getByRole("button", { name: "Start processing", exact: true }).click();
  await pollUntil("Independent Processing startup", async () => (await api("/engine/status")).body.data,
    current => current.processor_alive === true);
  const workers = (await api("/engine/status")).body.data;
  assert.equal(workers.acquisition_alive, true);
  assert.equal(workers.processor_alive, true);
  assert.equal(workers.stimulation_connected, false);
  assert.equal(workers.actuation_alive, false);
  assert.equal((await api("/acquisition/status")).body.data.streaming, false);
  assert.deepEqual((await api("/config/plugin-profiles")).body.data, []);
  await page.getByRole("button", { name: "Start reading", exact: true }).click();
  const recorded = await pollUntil("First decoded file trial", async () => {
    const response = await api("/results");
    return { status: response.status, trials: response.body.data?.trials };
  }, current => current.status === 200 && current.trials?.length > 0, 90_000);
  assert.ok(recorded.trials.length > 0);
  await page.getByRole("button", { name: "Stop reading", exact: true }).click();
  await pollUntil("Stopped reading", async () => (await api("/acquisition/status")).body.data,
    current => current.connected === true && current.streaming === false);
  assert.equal((await api("/engine/status")).body.data.processor_alive, true);
  await page.getByRole("button", { name: "Stop processing", exact: true }).click();
  await pollUntil("Independent Processing stop", async () => (await api("/engine/status")).body.data,
    current => current.processor_alive === false && current.acquisition_alive === true && current.running === true);
  await page.getByRole("button", { name: "Results", exact: true }).click();
  await page.getByRole("heading", { name: "Trial results", exact: true }).waitFor();
  await page.getByRole("table", { name: "Trial results", exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, "replay-results.png") });
  const currentResults = (await api("/results")).body.data;
  assert.ok(currentResults.trials.length > 0);
  assert.ok(currentResults.trials.every(trial => trial.generation === currentResults.generation));
  assert.equal((await api("/acquisition/status")).body.data.streaming, false);

  stage = "local disconnect";
  await bounded(page.evaluate(() => window.omnibciDesktop.disconnect()), "Local disconnect", 90_000);
  await page.getByRole("heading", { name: "Connect your workspace" }).waitFor();
  await assert.rejects(timedFetch(local.url + "/healthz"), error => error.cause?.code === "ECONNREFUSED");

  stage = "remote connection";
  process.env.OMNIBCI_CONFIG_DIR = path.join(directory, "remote/config");
  process.env.OMNIBCI_PLUGIN_DIR = path.join(directory, "remote/plugins");
  const ready = await remote.start(python, packaged ? directory : path.resolve(root, ".."), packaged, message => diagnostics.push(message));
  for (const name of processOverrides) {
    if (originalEnvironment[name] === undefined) delete process.env[name];
    else process.env[name] = originalEnvironment[name];
  }
  await page.getByRole("radio", { name: "Remote server", exact: true }).check();
  await page.getByLabel("Server address", { exact: true }).fill(ready.url);
  await page.getByLabel("Access token", { exact: true }).fill("incorrect-token");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await pollUntil("Rejected remote token", () => page.evaluate(() => window.omnibciDesktop.getConnection()),
    connection => connection.status === "failed" && connection.id === "");
  assert.equal(await page.getByLabel("Access token", { exact: true }).inputValue(), "");
  await page.getByLabel("Access token", { exact: true }).fill(remote.token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await page.getByRole("button", { name: "Setup", exact: true }).click();
  await page.getByRole("heading", { name: "Acquisition source" }).waitFor();
  assert.equal(await page.getByLabel("Recording path", { exact: true }).inputValue(), "");
  assert.equal(await page.getByRole("button", { name: "Browse…", exact: true }).count(), 0);
  const remoteConnection = await page.evaluate(() => window.omnibciDesktop.getConnection());
  assert.equal(remoteConnection.kind, "remote");
  assert.notEqual(remoteConnection.id, local.id);
  assert.equal((await api("/server/info")).body.data.instance_id, ready.instance_id);
  assert.equal((await api("/engine/status")).body.data.running, false);
  await page.waitForFunction(() => document.querySelector(".connection-open"), null, { timeout: 20_000 });
  await page.screenshot({ path: path.join(output, "remote-workspace.png") });
  await page.evaluate(() => window.omnibciDesktop.disconnect());
  await page.getByRole("heading", { name: "Connect your workspace" }).waitFor();
  assert.equal((await timedFetch(ready.url + "/api/v1/server/info", { headers: { Authorization: `Bearer ${remote.token}` } })).status, 200);
  stage = "app exit";
  await bounded(application.close(), "App exit");
  application = undefined;
  assert.equal((await bounded(remote.command("shutdown"), "Remote server shutdown")).ok, true);
  await bounded(remote.waitForExit(), "Remote server exit");
  await writeFile(path.join(output, "result.json"), JSON.stringify({ ok: true, packaged, directory, prefs }, null, 2));
  console.log(`Desktop smoke passed (${packaged ? "packaged" : "development"}); artifacts: ${output}`);
} catch (error) {
  const page = application?.windows()[0];
  if (page) {
    await page.screenshot({ path: path.join(output, "failure.png"), timeout: 10_000 }).catch(() => {});
    diagnostics.push(await page.locator("body").innerText({ timeout: 5000 }).catch(() => ""));
  }
  await writeFile(path.join(output, "result.json"), JSON.stringify({ ok: false, stage, error: error.stack, directory }, null, 2));
  throw error;
} finally {
  for (const name of processOverrides) {
    if (originalEnvironment[name] === undefined) delete process.env[name];
    else process.env[name] = originalEnvironment[name];
  }
  const cleanup = [];
  cleanup.push(bounded(remote.force(), "Remote server cleanup", 10_000));
  if (application) cleanup.push((async () => {
    // On failure, cleanup must not leave a dirty editor confirmation blocking quit.
    await bounded(application.evaluate(({ dialog }) => {
      dialog.showMessageBox = async (_window, options) => ({ response: ["Discard changes", "Stop and continue", "Disconnect", "Discard results", "Force exit"]
        .map(label => options.buttons?.indexOf(label) ?? -1).find(index => index >= 0) ?? 0, checkboxChecked: false });
    }), "Cleanup dialog setup", 5000).catch(() => {});
    await bounded(application.close(), "App cleanup").catch(error => {
      diagnostics.push(error.message + "\n");
      if (application.process().exitCode === null) application.process().kill("SIGKILL");
    });
  })());
  const outcomes = await Promise.allSettled(cleanup);
  for (const outcome of outcomes) if (outcome.status === "rejected") diagnostics.push(`Cleanup failed: ${outcome.reason}\n`);
  for (const logfile of serverLogs) {
    try {
      // The server suffixes its configured filename with its process ID.
      const entries = await readdir(path.dirname(logfile));
      for (const entry of entries.filter(name => /^server-server-\d+\.log$/.test(name))) {
        diagnostics.push(`\nServer log (${entry}):\n${await readFile(path.join(path.dirname(logfile), entry), "utf8")}`);
      }
    }
    catch { /* The server may have failed before opening its log. */ }
  }
  await writeFile(path.join(output, "diagnostics.log"), diagnostics.join(""));
}
