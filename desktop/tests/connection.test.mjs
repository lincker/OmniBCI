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
const { ServerConnection, serverOrigin, apiPath, handleApiRequest, CONNECTION_HEADER } = createRequire(import.meta.url)(path.join(temporary, "connection.cjs"));
const info = (instance = "server-1", authenticated = true) => Response.json({ success: true, data: {
  desktop_api_version: 1, app_version: "1.0.0", instance_id: instance, auth_enabled: authenticated,
} });

test("server and proxy paths cannot choose another target or carry credentials", () => {
  assert.equal(serverOrigin("http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  for (const url of ["file:///etc/passwd", "http://user:secret@host", "https://host/api", "https://host/?token=x"]) {
    assert.throws(() => serverOrigin(url));
  }
  assert.equal(apiPath("/api/v1/results?example=value"), "/api/v1/results?example=value");
  for (const value of ["//evil/api/v1/data", "https://evil/api/v1/data", "/api/v1/%2e%2e/config", "/etc/passwd"]) {
    assert.throws(() => apiPath(value));
  }
});

test("handshake requires a complete desktop protocol response", async () => {
  const connection = new ServerConnection(async () => Response.json({ success: true, data: { desktop_api_version: 1, auth_enabled: false } }));
  await assert.rejects(connection.connect("existing", "http://localhost:8000", "private-test-credential"), /protocol 1/);
  assert.equal(connection.info.status, "failed");
  assert.equal(connection.info.id, "");
  assert.equal(JSON.stringify(connection.info).includes("private-test-credential"), false);
});

test("handshake uses the Server endpoint and protocol version 1", async () => {
  const calls = [];
  const connection = new ServerConnection(async url => {
    calls.push(url);
    return Response.json({ success: true, data: {
      desktop_api_version: 1, app_version: "1.0.0", instance_id: "server", auth_enabled: true,
    } });
  });
  await connection.connect("existing", "http://localhost:8000", "secret");
  assert.deepEqual(calls, ["http://localhost:8000/api/v1/server/info"]);
  assert.equal(connection.info.status, "ready");
  connection.disconnect();
});

test("handshake rejects an unsupported desktop protocol without a fallback", async () => {
  const calls = [];
  const connection = new ServerConnection(async url => {
    calls.push(url);
    return Response.json({ success: true, data: {
      desktop_api_version: 2, app_version: "1.0.0", instance_id: "server", auth_enabled: true,
    } });
  });
  await assert.rejects(connection.connect("existing", "http://localhost:8000", "secret"), /protocol 1/);
  assert.deepEqual(calls, ["http://localhost:8000/api/v1/server/info"]);
  assert.equal(connection.info.status, "failed");
});

test("a missing Server endpoint fails without probing old endpoints", async () => {
  const calls = [];
  const connection = new ServerConnection(async url => {
    calls.push(url);
    return new Response(null, { status: 404 });
  });
  await assert.rejects(connection.connect("existing", "http://localhost:8000", "secret"), /HTTP 404/);
  assert.deepEqual(calls, ["http://localhost:8000/api/v1/server/info"]);
  assert.equal(connection.info.status, "failed");
});

test("local handshake matches the exact launched server", async () => {
  const connection = new ServerConnection(async () => info("another-process"));
  await assert.rejects(connection.connect("owned", "http://127.0.0.1:8000", "secret", "my-process"), /identity/);
});

test("proxy owns authentication, preserves raw uploads and never follows redirects", async () => {
  let forwarded;
  const connection = new ServerConnection(async (url, init) => {
    if (url.endsWith("/server/info")) return info();
    forwarded = { url, init };
    return Response.json({ success: true });
  });
  await connection.connect("existing", "http://server:8000", "private-token");
  const body = new Uint8Array([80, 75, 3, 4]);
  const response = await connection.request("/api/v1/plugin-packages/install", {
    method: "POST", body, headers: { "Content-Type": "application/zip", Authorization: "bad", Cookie: "secret=bad" },
  });
  await response.json();
  assert.equal(forwarded.url, "http://server:8000/api/v1/plugin-packages/install");
  assert.equal(forwarded.init.headers.get("Authorization"), "Bearer private-token");
  assert.equal(forwarded.init.headers.get("Cookie"), null);
  assert.equal(forwarded.init.body, body);
  assert.equal(forwarded.init.redirect, "error");
  assert.equal(forwarded.init.credentials, "omit");
});

test("switching cancels old requests and does not redirect a delayed write to the new server", async () => {
  let release;
  let oldSignal;
  const calls = [];
  const connection = new ServerConnection(async (url, init) => {
    calls.push(url);
    if (url.endsWith("/server/info")) return info();
    oldSignal = init.signal;
    return new Promise(resolve => { release = () => resolve(Response.json({ old: true })); });
  });
  await connection.connect("existing", "http://first:8000", "one");
  const firstId = connection.info.id;
  const pending = connection.request("/api/v1/engine/start", { method: "POST" }, firstId);
  await connection.connect("existing", "http://second:8000", "two");
  assert.equal(oldSignal.aborted, true);
  release();
  assert.equal((await pending).status, 409);
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" }, firstId)).status, 409);
  assert.equal(calls.filter(url => url.endsWith("/engine/start")).length, 1);
});

test("response streaming remains guarded after headers have arrived", async () => {
  let finish;
  const connection = new ServerConnection(async url => url.endsWith("/server/info") ? info() : new Response(new ReadableStream({
    start(controller) { finish = () => { controller.enqueue(new TextEncoder().encode("old result")); controller.close(); }; },
  })));
  await connection.connect("existing", "http://first:8000", "one");
  const response = await connection.request("/api/v1/results");
  const text = response.text();
  await connection.connect("existing", "http://second:8000", "two");
  finish();
  await assert.rejects(text, /Connection changed/);
});

test("preparing shutdown allows reads but rejects new mutation calls", async () => {
  let requests = 0;
  const connection = new ServerConnection(async url => {
    if (url.endsWith("/server/info")) return info();
    requests++; return Response.json({ success: true });
  });
  await connection.connect("owned", "http://127.0.0.1:8000", "secret");
  connection.stopping(true);
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" })).status, 409);
  await (await connection.request("/api/v1/results")).json();
  assert.equal(requests, 1);
});

test("failed mutation is not retried and reports an unknown server state", async () => {
  let attempts = 0;
  const connection = new ServerConnection(async url => {
    if (url.endsWith("/server/info")) return info();
    attempts++; throw new Error("socket closed");
  });
  await connection.connect("existing", "http://localhost:8000", "secret");
  assert.equal((await connection.request("/api/v1/engine/start", { method: "POST" })).status, 503);
  assert.equal(attempts, 1);
  assert.match(connection.info.error, /unknown/);
});

test("development CORS preflight is origin-bound and never reaches the server", async () => {
  const connection = new ServerConnection(async () => assert.fail("Preflight must not contact the server"));
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

test("the development request uses the same selected server and injects CORS on errors", async () => {
  const calls = [];
  const connection = new ServerConnection(async (url, init) => {
    if (url.endsWith("/server/info")) return info();
    calls.push({ url, init }); return Response.json({ success: false }, { status: 422 });
  });
  await connection.connect("existing", "https://laboratory.example", "secret");
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
  const connection = new ServerConnection(async url => {
    if (url.endsWith("/server/info")) return info();
    throw new Error("HTTP connection reset");
  });
  let unsubscribe;
  try {
    await connection.connect("existing", `http://127.0.0.1:${server.address().port}`, "secret");
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


test("existing loopback server connects without a token and forwards no credentials", async () => {
  const calls = [];
  const connection = new ServerConnection(async (url, init) => {
    calls.push({ url, init });
    return url.endsWith("/server/info") ? info("shared", false) : Response.json({ success: true });
  });
  for (const host of ["127.0.0.1", "localhost", "[::1]"]) {
    await connection.connect("existing", `http://${host}:8000`, "");
    assert.equal(connection.info.status, "ready");
    assert.equal(connection.info.canChooseRecordingFile, true);
    await (await connection.request("/api/v1/results", { headers: { Authorization: "attacker", Cookie: "session=attacker" } })).json();
  }
  for (const { init } of calls) {
    assert.equal(new Headers(init.headers).get("Authorization"), null);
    assert.equal(new Headers(init.headers).get("Cookie"), null);
    assert.equal(init.credentials, "omit");
  }
  connection.disconnect();
  assert.equal(connection.info.canChooseRecordingFile, undefined);
});

test("non-loopback and owned servers require authentication", async () => {
  const connection = new ServerConnection(async () => info("shared", false));
  await assert.rejects(connection.connect("existing", "http://lab.example:8000", ""), /token/i);
  await assert.rejects(connection.connect("existing", "http://lab.example:8000", "ignored"), /authentication/i);
  await assert.rejects(connection.connect("owned", "http://127.0.0.1:8000", ""), /token/i);
  await assert.rejects(connection.connect("owned", "http://127.0.0.1:8000", "ignored"), /authentication/i);
  await assert.rejects(connection.connect("existing", "http://localhost.evil:8000", ""), /token/i);
});

test("enabled authentication rejects missing or incorrect tokens", async () => {
  const connection = new ServerConnection(async (_url, init) => new Headers(init.headers).get("Authorization") === "Bearer correct"
    ? info() : new Response(null, { status: 401 }));
  for (const token of ["", "wrong"]) {
    await assert.rejects(connection.connect("existing", "http://127.0.0.1:8000", token), /HTTP 401/);
  }
  await connection.connect("existing", "http://127.0.0.1:8000", "correct");
  assert.equal(connection.info.status, "ready");
  connection.disconnect();
});

test("authenticated non-loopback server cannot choose desktop files", async () => {
  const connection = new ServerConnection(async () => info());
  await connection.connect("existing", "https://lab.example", "secret");
  assert.equal(connection.info.canChooseRecordingFile, false);
  connection.disconnect();
});

test("existing loopback results sockets use the same optional authentication as HTTP", async () => {
  const server = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await new Promise(resolve => server.once("listening", resolve));
  const headers = [];
  server.on("connection", (_socket, request) => { headers.push(request.headers); });
  const connection = new ServerConnection(async (_url, init) => info("shared", !!new Headers(init.headers).get("Authorization")));
  let unsubscribe;
  try {
    for (const token of ["", "secret"]) {
      await connection.connect("existing", `http://127.0.0.1:${server.address().port}`, token);
      await new Promise(resolve => {
        unsubscribe = connection.subscribe(connection.info.id, event => { if (event.type === "open") resolve(); });
      });
      unsubscribe();
    }
    assert.deepEqual(headers.map(value => value.authorization), [undefined, "Bearer secret"]);
    assert.ok(headers.every(value => value.cookie === undefined));
  } finally {
    unsubscribe?.(); connection.disconnect();
    for (const client of server.clients) client.terminate();
    await new Promise(resolve => server.close(resolve));
  }
});


test("owned results reconnect never attaches to another server instance", async () => {
  let instance = "launched-instance";
  const connection = new ServerConnection(async () => info(instance));
  await connection.connect("owned", "http://127.0.0.1:8000", "secret", instance);
  const originalId = connection.info.id;
  instance = "different-instance";
  let unsubscribe;
  await new Promise(resolve => {
    unsubscribe = connection.subscribe(originalId, event => { if (event.type === "closed") resolve(); });
  });
  assert.equal(connection.info.id, originalId);
  assert.equal(connection.info.status, "failed");
  unsubscribe();
  connection.disconnect();
});

test("obsolete connection mode names are rejected", async () => {
  const connection = new ServerConnection(async () => assert.fail("Obsolete modes must not contact a server"));
  for (const kind of ["local", "remote"]) {
    await assert.rejects(connection.connect(kind, "http://127.0.0.1:8000", "secret"), /connection mode/);
  }
});
