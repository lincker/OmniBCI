import { app, BrowserWindow, dialog, ipcMain, net, protocol, session, shell, type IpcMainEvent, type IpcMainInvokeEvent } from "electron";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { BackendConnection, backendOrigin, handleApiRequest } from "./connection.js";
import { LocalBackend, type ControlResult } from "./local-backend.js";
import type { ConnectionTarget } from "./contracts.js";
import { desktopText } from "./i18n.js";

protocol.registerSchemesAsPrivileged([{ scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
app.setName("OmniBCI");
if (process.env.OMNIBCI_DESKTOP_USER_DATA) {
  if (!path.isAbsolute(process.env.OMNIBCI_DESKTOP_USER_DATA)) throw new Error("OMNIBCI_DESKTOP_USER_DATA must be absolute.");
  app.setPath("userData", process.env.OMNIBCI_DESKTOP_USER_DATA);
}

const connection = new BackendConnection((url, init) => net.fetch(url, init));
const subscriptions = new Map<string, () => void>();
let window: BrowserWindow;
let backend: LocalBackend | undefined;
let quitAllowed = false;
let transition = false;
let editor = { dirty: false, busy: false };
let logDirectory: string;
let preferencesPath: string;
let writeLog: (message: string) => void = () => {};
let devOrigin: string | undefined;
let language: "en" | "zh" = "en";
const t = (message: Parameters<typeof desktopText>[1], params?: Record<string, string | number>) => desktopText(language, message, params);

function trusted(event: IpcMainEvent | IpcMainInvokeEvent): void {
  const frame = event.senderFrame;
  const url = frame && new URL(frame.url);
  if (event.sender !== window.webContents || frame !== window.webContents.mainFrame || !url
    || !(url.protocol === "app:" && url.host === "omnibci" || devOrigin && url.origin === devOrigin)) {
    throw new Error("Untrusted desktop request.");
  }
}

function subscribe(channel: string, handler: (value: any) => Promise<any>): void {
  ipcMain.handle(channel, async (event, value) => { trusted(event); return handler(value); });
}

async function exclusive<T>(action: () => Promise<T>): Promise<T> {
  if (transition) throw new Error(t("A connection or shutdown operation is already in progress."));
  transition = true;
  try { return await action(); } finally { transition = false; }
}

function clearSubscriptions(): void {
  for (const stop of subscriptions.values()) stop();
  subscriptions.clear();
}

async function awaitStop(operation: Promise<ControlResult | void>, owned: LocalBackend): Promise<ControlResult | void> {
  const settled = operation.then(value => ({ value }), error => ({ error: error as Error }));
  while (true) {
    let timer: ReturnType<typeof setTimeout>;
    const result = await Promise.race([settled, new Promise<"timeout">(resolve => { timer = setTimeout(() => resolve("timeout"), 30_000); })]);
    clearTimeout(timer!);
    if (result !== "timeout") {
      if ("error" in result) throw result.error;
      return result.value;
    }
    const answer = await dialog.showMessageBox(window, {
      type: "warning", message: t("The local backend is still stopping."),
      detail: `${owned.shutdownStage}.\n${t("Continue waiting, or force this app's processes to exit. Forced exit can lose results and does not confirm that physical devices have stopped.")}`,
      buttons: [t("Continue waiting"), t("Force exit")], defaultId: 0, cancelId: 0,
    });
    if (answer.response === 1) { await owned.force(); return { ok: false, forced: true, errors: [t("Backend was forcibly terminated.")] }; }
  }
}

async function resumeStoppedBackend(owned: LocalBackend): Promise<void> {
  let result: ControlResult | void = undefined;
  try { result = await awaitStop(owned.command("resume"), owned); }
  catch (error) {
    connection.failed(t("The local backend is unavailable; its stop state could not be confirmed."));
    throw error;
  }
  if (!result?.ok) {
    connection.failed(t("Backend remains stopped; reconnect before sending commands."));
    throw new Error((result?.errors ?? [t("Could not return to the stopped backend.")]).join("\n"));
  }
  // This opens API admission only. It never restarts acquisition or the engine.
  connection.stopping(false);
}

async function disconnect(): Promise<boolean> {
  if (editor.dirty) {
    const answer = await dialog.showMessageBox(window, { type: "warning", message: t("Discard unsaved configuration changes?"),
      detail: t("Return to the editor to save changes. Nothing will be saved or applied automatically."),
      buttons: [t("Return to editor"), t("Discard changes")], defaultId: 0, cancelId: 0 });
    if (answer.response === 0) return false;
  }
  if (!backend) {
    if (connection.info.id && connection.info.kind === "remote") {
      const answer = await dialog.showMessageBox(window, { type: "question", message: t("Disconnect from the remote backend?"),
        detail: t("The remote experiment and service will continue. Results remain in its memory and are lost if that service restarts."),
        buttons: [t("Cancel"), t("Disconnect")], defaultId: 0, cancelId: 0 });
      if (answer.response === 0) return false;
    }
    clearSubscriptions(); connection.disconnect(); return true;
  }
  const owned = backend;
  const confirm = await dialog.showMessageBox(window, { type: "question", message: t("Stop the local backend?"),
    detail: t("Acquisition, stimulation and outputs will stop. Live results will be cleared."),
    buttons: [t("Cancel"), t("Stop and continue")], defaultId: 0, cancelId: 0 });
  if (confirm.response === 0) return false;
  connection.stopping(true);
  let prepared: ControlResult | void;
  try { prepared = await awaitStop(owned.command("prepare_shutdown"), owned); }
  catch (error) { prepared = { ok: false, errors: [(error as Error).message] }; }
  if (prepared?.forced) {
    backend = undefined; clearSubscriptions(); connection.disconnect(); return true;
  }
  if (!prepared?.ok) {
    const answer = await dialog.showMessageBox(window, { type: "error", message: t("Normal shutdown could not be confirmed."),
      detail: (prepared?.errors ?? [t("Unknown shutdown result.")]).join("\n"),
      buttons: [t("Keep window open"), t("Force exit")], defaultId: 0, cancelId: 0 });
    if (answer.response === 0) { await resumeStoppedBackend(owned); return false; }
    await owned.force(); backend = undefined;
    clearSubscriptions(); connection.disconnect(); return true;
  }
  clearSubscriptions();
  let result: ControlResult | void = undefined;
  let shutdownError: unknown;
  try { result = await awaitStop(owned.command("shutdown"), owned); }
  catch (error) { shutdownError = error; }
  await awaitStop(owned.waitForExit(), owned);
  backend = undefined; connection.disconnect();
  if (shutdownError) throw shutdownError;
  if (result && !result.ok && !result.forced) throw new Error((result.errors ?? [t("Shutdown failed.")]).join("\n"));
  return true;
}

async function connect(target: ConnectionTarget): Promise<void> {
  if (!target || !["local", "remote"].includes(target.kind)) throw new Error(t("Choose a connection mode."));
  if (!await disconnect()) return;
  editor = { dirty: false, busy: false };
  if (target.kind === "remote") {
    await connection.connect("remote", target.url, target.token);
  } else {
    let python: string;
    if (app.isPackaged) {
      const manifest = JSON.parse(await readFile(path.join(process.resourcesPath, "runtime-manifest.json"), "utf8"));
      const expected = `${process.platform}-${process.arch}`;
      if (manifest.target !== expected || manifest.schemaVersion !== 1) throw new Error(t("Bundled Python target does not match this app."));
      python = path.join(process.resourcesPath, "python", process.platform === "win32" ? "python.exe" : "bin/python3");
    } else {
      python = process.env.OMNIBCI_DESKTOP_PYTHON ?? "";
      if (!path.isAbsolute(python)) throw new Error("Set OMNIBCI_DESKTOP_PYTHON to an absolute Python 3.14 executable path.");
    }
    const owned = new LocalBackend();
    backend = owned;
    connection.launchingLocal();
    owned.on("exit", (error: Error) => {
      if (backend === owned && connection.info.status !== "stopping") { clearSubscriptions(); connection.failed(error.message); }
    });
    owned.on("protocol-error", (error: Error) => {
      if (backend === owned) { clearSubscriptions(); connection.failed(error.message); }
    });
    try {
      const ready = await owned.start(python, app.isPackaged ? app.getPath("userData") : path.resolve(__dirname, "../.."), app.isPackaged, writeLog);
      await connection.connect("local", ready.url, owned.token, ready.instance_id);
    } catch (error) { await owned.force(); backend = undefined; connection.failed((error as Error).message); throw error; }
  }
  const value = { kind: target.kind, url: target.kind === "remote" ? connection.info.url : undefined };
  const temporary = preferencesPath + ".tmp";
  await writeFile(temporary, JSON.stringify(value), { mode: 0o600 });
  await rename(temporary, preferencesPath);
}

async function requestQuit(): Promise<void> {
  await exclusive(async () => {
    if (await disconnect()) { quitAllowed = true; app.quit(); }
  });
}

function displayError(error: unknown): void {
  const message = error instanceof Error ? error.message : t("Desktop operation failed.");
  writeLog(message + "\n");
  if (window && !window.isDestroyed()) void dialog.showMessageBox(window, { type: "error", message });
}

async function start(): Promise<void> {
  language = app.getLocale().toLowerCase().startsWith("zh") ? "zh" : "en";
  const dataDirectory = path.join(app.getPath("userData"), "desktop");
  logDirectory = path.join(dataDirectory, "logs");
  preferencesPath = path.join(dataDirectory, "connection.json");
  await mkdir(logDirectory, { recursive: true });
  const log = createWriteStream(path.join(logDirectory, `desktop-${new Date().toISOString().slice(0, 10)}.log`), { flags: "a", mode: 0o600 });
  log.on("error", error => process.stderr.write(`Desktop log unavailable: ${error.message}\n`));
  writeLog = text => log.write(text);
  try {
    const saved = JSON.parse(await readFile(preferencesPath, "utf8"));
    if (saved.kind === "local") connection.info = { ...connection.info, kind: "local" };
    else if (saved.kind === "remote" && typeof saved.url === "string") {
      connection.info = { ...connection.info, kind: "remote", url: backendOrigin(saved.url) };
    }
  } catch { /* First launch or unreadable preferences: show local mode. */ }

  if (!app.isPackaged && process.env.OMNIBCI_DESKTOP_DEV_URL) {
    const address = new URL(process.env.OMNIBCI_DESKTOP_DEV_URL);
    if (address.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(address.hostname)) throw new Error("The development frontend must be on localhost.");
    devOrigin = address.origin;
  }
  const desktopSession = session.fromPartition("persist:omnibci");
  desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const renderer = path.join(__dirname, "renderer");
  desktopSession.protocol.handle("app", async request => {
    try {
      const url = new URL(request.url);
      if (url.host !== "omnibci") return new Response("Not found", { status: 404 });
      if (url.pathname.startsWith("/api/")) {
        return handleApiRequest(connection, request as Request & { initiatorOrigin?: string }, devOrigin);
      }
      if (!["GET", "HEAD"].includes(request.method)) return new Response(null, { status: 405 });
      const filename = path.resolve(renderer, "." + decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname));
      if (!filename.startsWith(renderer + path.sep)) return new Response(null, { status: 403 });
      const response = await net.fetch(pathToFileURL(filename).toString());
      const headers = new Headers(response.headers);
      headers.set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'");
      return new Response(response.body, { status: response.status, headers });
    } catch { return new Response("Resource unavailable", { status: 404 }); }
  });

  window = new BrowserWindow({ width: 1380, height: 900, minWidth: 960, minHeight: 640, title: "OmniBCI", show: false,
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), session: desktopSession, nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true, spellcheck: false } });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", event => event.preventDefault());
  window.webContents.on("render-process-gone", (_event, details) => {
    writeLog(`Renderer exited: ${details.reason}\n`);
    clearSubscriptions();
    if (!transition) void requestQuit().catch(displayError);
  });
  window.on("close", event => { if (!quitAllowed) { event.preventDefault(); if (!transition) void requestQuit().catch(displayError); } });
  window.on("query-session-end", event => { event.preventDefault(); if (!transition) void requestQuit().catch(displayError); });
  let previousId = connection.info.id;
  connection.on("change", state => {
    if (state.id !== previousId) { clearSubscriptions(); previousId = state.id; }
    if (!window.isDestroyed()) window.webContents.send("connection:changed", state);
  });

  subscribe("connection:get", async () => ({ ...connection.info }));
  subscribe("connection:connect", target => exclusive(async () => { await connect(target); return { ...connection.info }; }));
  subscribe("connection:disconnect", () => exclusive(async () => { await disconnect(); }));
  subscribe("recording:choose-file", async () => {
    if (connection.info.kind !== "local" || connection.info.status !== "ready") throw new Error(t("Choose files on the backend machine in remote mode."));
    const owner = connection.info.id;
    const result = await dialog.showOpenDialog(window, { title: t("Choose recording"), buttonLabel: t("Open"), properties: ["openFile"], filters: [{ name: t("CNT recording"), extensions: ["cnt"] }] });
    if (connection.info.id !== owner) throw new Error(t("Connection changed while choosing a file."));
    return result.canceled ? null : result.filePaths[0];
  });
  subscribe("logs:open", async () => { const error = await shell.openPath(logDirectory); if (error) throw new Error(error); });
  subscribe("app:quit", requestQuit);
  ipcMain.on("app:language", (event, value) => {
    try { trusted(event); if (value === "en" || value === "zh") language = value; } catch { /* Reject untrusted sender. */ }
  });
  ipcMain.on("editor:state", (event, value) => {
    try { trusted(event); editor = { dirty: value?.dirty === true, busy: value?.busy === true }; } catch { /* Reject untrusted sender. */ }
  });
  ipcMain.on("results:subscribe", (event, value) => {
    try {
      trusted(event);
      if (typeof value?.subscription !== "string" || value.subscription.length > 64 || value.connectionId !== connection.info.id) return;
      subscriptions.get(value.subscription)?.();
      if (subscriptions.size >= 8) return;
      subscriptions.set(value.subscription, connection.subscribe(value.connectionId, result => {
        if (!window.isDestroyed()) window.webContents.send("results:event", { subscription: value.subscription, event: result });
      }));
    } catch { /* Reject untrusted sender. */ }
  });
  ipcMain.on("results:unsubscribe", (event, value) => {
    try { trusted(event); subscriptions.get(value)?.(); subscriptions.delete(value); } catch { /* Reject untrusted sender. */ }
  });
  window.once("ready-to-show", () => window.show());
  await window.loadURL(devOrigin ? process.env.OMNIBCI_DESKTOP_DEV_URL! : "app://omnibci/");
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } });
  app.on("before-quit", event => { if (!quitAllowed && window) { event.preventDefault(); if (!transition) void requestQuit().catch(displayError); } });
  void app.whenReady().then(start).catch(error => { displayError(error); quitAllowed = true; app.quit(); });
}
