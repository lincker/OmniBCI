import assert from "node:assert/strict";
import { test, after } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

const temporary = await mkdtemp(path.join(tmpdir(), "omnibci-desktop-tests-"));
after(() => rm(temporary, { recursive: true, force: true }));
await build({ entryPoints: [fileURLToPath(new URL("../src/connection.ts", import.meta.url))],
  outfile: path.join(temporary, "connection.cjs"), platform: "node", format: "cjs", bundle: true });
const { BackendConnection, backendOrigin, apiPath, handleApiRequest, CONNECTION_HEADER } = createRequire(import.meta.url)(path.join(temporary, "connection.cjs"));
const info = (instance = "backend-1") => Response.json({ success: true, data: {
  desktop_api_version: 1, app_version: "1.0.0", instance_id: instance, auth_enabled: true,
} });

test("backend and proxy paths cannot choose another target or carry credentials", () => {
  assert.equal(backendOrigin("http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  for (const url of ["file:///etc/passwd", "http://user:secret@host", "https://host/api", "https://host/?token=x"]) {
    assert.throws(() => backendOrigin(url));
  }
  assert.equal(apiPath("/api/v1/sessions?format=json"), "/api/v1/sessions?format=json");
  for (const value of ["//evil/api/v1/data", "https://evil/api/v1/data", "/api/v1/%2e%2e/config", "/etc/passwd"]) {
    assert.throws(() => apiPath(value));
  }
});

test("handshake requires authentication and the desktop protocol", async () => {
  const connection = new BackendConnection(async () => Response.json({ success: true, data: { desktop_api_version: 1, auth_enabled: false } }));
  await assert.rejects(connection.connect("remote", "http://localhost:8000", "private-test-credential"), /upgraded/);
  assert.equal(connection.info.status, "failed");
  assert.equal(connection.info.id, "");
  assert.equal(JSON.stringify(connection.info).includes("private-test-credential"), false);
});

test("local handshake matches the exact launched backend", async () => {
  const connection = new BackendConnection(async () => info("another-process"));
  await assert.rejects(connection.connect("local", "http://127.0.0.1:8000", "secret", "my-process"), /identity/);
});

test("proxy owns authentication, preserves raw uploads and never follows redirects", async () => {
  let forwarded;
  const connection = new BackendConnection(async (url, init) => {
    if (url.endsWith("/system/info")) return info();
    forwarded = { url, init };
    return Response.json({ success: true });
  });
  await connection.connect("remote", "http://backend:8000", "private-token");
  const body = new Uint8Array([80, 75, 3, 4]);
  const response = await connection.request("/api/v1/plugin-packages/install", {
    method: "POST", body, headers: { "Content-Type": "application/zip", Authorization: "bad", Cookie: "secret=bad" },
  });
  await response.json();
  assert.equal(forwarded.url, "http://backend:8000/api/v1/plugin-packages/install");
  assert.equal(forwarded.init.headers.get("Authorization"), "Bearer private-token");
  assert.equal(forwarded.init.headers.get("Cookie"), null);
  assert.equal(forwarded.init.body, body);
  assert.equal(forwarded.init.redirect, "error");
  assert.equal(forwarded.init.credentials, "omit");
});

test("switching cancels old requests and does not redirect a delayed write to the new backend", async () => {
  let release;
  let oldSignal;
  const calls = [];
  const connection = new BackendConnection(async (url, init) => {
    calls.push(url);
    if (url.endsWith("/system/info")) return info();
    oldSignal = init.signal;
    return new Promise(resolve => { release = () => resolve(Response.json({ old: true })); });
  });
  await connection.connect("remote", "http://first:8000", "one");
  const firstId = connection.info.id;
  const pending = connection.request("/api/v1/engine/start", { method: "POST" }, firstId);
  await connection.connect("remote", "http://second:8000", "two");
  assert.equal(oldSignal.aborted, true);
  release();
  assert.equal((await pending).status, 409);
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" }, firstId)).status, 409);
  assert.equal(calls.filter(url => url.endsWith("/engine/start")).length, 1);
});

test("response streaming remains guarded after headers have arrived", async () => {
  let finish;
  const connection = new BackendConnection(async url => url.endsWith("/system/info") ? info() : new Response(new ReadableStream({
    start(controller) { finish = () => { controller.enqueue(new TextEncoder().encode("old result")); controller.close(); }; },
  })));
  await connection.connect("remote", "http://first:8000", "one");
  const response = await connection.request("/api/v1/sessions/export");
  const text = response.text();
  await connection.connect("remote", "http://second:8000", "two");
  finish();
  await assert.rejects(text, /Connection changed/);
});

test("preparing shutdown allows reads but rejects new mutation calls", async () => {
  let requests = 0;
  const connection = new BackendConnection(async url => {
    if (url.endsWith("/system/info")) return info();
    requests++; return Response.json({ success: true });
  });
  await connection.connect("local", "http://127.0.0.1:8000", "secret");
  connection.stopping(true);
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" })).status, 409);
  await (await connection.request("/api/v1/sessions")).json();
  assert.equal(requests, 1);
});

test("failed mutation is not retried and reports an unknown backend state", async () => {
  let attempts = 0;
  const connection = new BackendConnection(async url => {
    if (url.endsWith("/system/info")) return info();
    attempts++; throw new Error("socket closed");
  });
  await connection.connect("remote", "http://localhost:8000", "secret");
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" })).status, 503);
  assert.equal(attempts, 1);
  assert.match(connection.info.error, /unknown/);
});

test("development CORS preflight is origin-bound and never reaches the backend", async () => {
  const connection = new BackendConnection(async () => assert.fail("Preflight must not contact the backend"));
  const request = Object.assign(new Request("app://omnibci/api/v1/config", { method: "OPTIONS", headers: {
    "Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": `content-type, ${CONNECTION_HEADER}`,
  } }), { initiatorOrigin: "http://127.0.0.1:5173" });
  const response = await handleApiRequest(connection, request, "http://127.0.0.1:5173");
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:5173");
  assert.match(response.headers.get("Access-Control-Allow-Headers"), /X-OmniBCI-Connection-Id/);
  request.initiatorOrigin = "https://untrusted.example";
  request.headers.set("Origin", "http://127.0.0.1:5173");
  assert.equal((await handleApiRequest(connection, request, "http://127.0.0.1:5173")).status, 403);
  request.initiatorOrigin = "http://127.0.0.1:5173";
  request.headers.set("Access-Control-Request-Headers", "authorization");
  assert.equal((await handleApiRequest(connection, request, "http://127.0.0.1:5173")).status, 403);
});

test("the development request uses the same selected backend and injects CORS on errors", async () => {
  const calls = [];
  const connection = new BackendConnection(async (url, init) => {
    if (url.endsWith("/system/info")) return info();
    calls.push({ url, init }); return Response.json({ success: false }, { status: 422 });
  });
  await connection.connect("remote", "https://laboratory.example", "secret");
  const request = Object.assign(new Request("app://omnibci/api/v1/config", { method: "PATCH", headers: {
    [CONNECTION_HEADER]: connection.info.id, "Content-Type": "application/json",
  }, body: "{}" }), { initiatorOrigin: "http://localhost:5173" });
  const response = await handleApiRequest(connection, request, "http://localhost:5173");
  assert.equal(response.status, 422);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), "http://localhost:5173");
  await response.json();
  assert.equal(calls[0].url, "https://laboratory.example/api/v1/config");
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer secret");
});

test("a failed HTTP command closes the live results socket so the renderer can reconnect", async () => {
  const server = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await new Promise(resolve => server.once("listening", resolve));
  const connection = new BackendConnection(async url => {
    if (url.endsWith("/system/info")) return info();
    throw new Error("HTTP connection reset");
  });
  let unsubscribe;
  try {
    await connection.connect("remote", `http://127.0.0.1:${server.address().port}`, "secret");
    const events = [];
    let opened, closed;
    const open = new Promise(resolve => { opened = resolve; });
    const close = new Promise(resolve => { closed = resolve; });
    unsubscribe = connection.subscribe(connection.info.id, event => {
      events.push(event.type);
      if (event.type === "open") opened();
      if (event.type === "closed") closed();
    });
    await open;
    assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" })).status, 503);
    await close;
    assert.deepEqual(events, ["open", "closed"]);
    assert.equal(connection.info.status, "failed");
    unsubscribe();
    const recovered = new Promise(resolve => {
      unsubscribe = connection.subscribe(connection.info.id, event => { if (event.type === "open") resolve(); });
    });
    await recovered;
    assert.equal(connection.info.status, "ready");
  } finally {
    unsubscribe?.(); connection.disconnect();
    for (const client of server.clients) client.terminate();
    await new Promise(resolve => server.close(resolve));
  }
});
