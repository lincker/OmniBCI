import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import WebSocket from "ws";
import type { ConnectionInfo, ResultsEvent } from "./contracts.js";

type Fetch = (url: string, init?: RequestInit) => Promise<Response>;
interface Target { url: string; token: string; instance: string }
export const CONNECTION_HEADER = "X-OmniBCI-Connection-Id";

export function backendOrigin(value: string): string {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password
    || url.search || url.hash || url.pathname !== "/") {
    throw new Error("Enter an HTTP(S) backend origin without a path or credentials.");
  }
  return url.origin;
}

export function apiPath(value: string): string {
  const url = new URL(value, "app://omnibci");
  const decoded = decodeURIComponent(url.pathname);
  if (url.origin !== "null" && url.origin !== "app://omnibci") throw new Error("Invalid API origin");
  if (url.protocol !== "app:" || url.host !== "omnibci" || !decoded.startsWith("/api/v1/")
    || decoded.includes("\\") || decoded.split("/").some(part => part === ".." || part === ".")) {
    throw new Error("Only OmniBCI API paths can be forwarded.");
  }
  return url.pathname + url.search;
}

export function proxyError(status: number, message: string): Response {
  return Response.json({ success: false, code: "DESKTOP_CONNECTION_ERROR", message }, { status });
}

/** Owns one selected backend. Every asynchronous operation captures its generation. */
export class BackendConnection extends EventEmitter {
  info: ConnectionInfo = { id: "", kind: null, status: "disconnected" };
  private target?: Target;
  private controllers = new Set<AbortController>();
  private sockets = new Set<WebSocket>();
  constructor(private readonly fetcher: Fetch) { super(); }

  private publish(update: Partial<ConnectionInfo>): void {
    this.info = { ...this.info, ...update };
    this.emit("change", { ...this.info });
  }

  private async identify(url: string, token: string, signal?: AbortSignal): Promise<string> {
    const response = await this.fetcher(url + "/api/v1/system/info", {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      redirect: "error", credentials: "omit", signal: signal ?? AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new Error(`Backend handshake failed (HTTP ${response.status}).`);
    const envelope = await response.json();
    const value = envelope?.data;
    if (envelope?.success !== true || value?.desktop_api_version !== 1 || value?.auth_enabled !== true
      || typeof value?.instance_id !== "string" || !value.instance_id || typeof value?.app_version !== "string") {
      throw new Error("The backend must be upgraded and token authentication enabled.");
    }
    return value.instance_id;
  }

  async connect(kind: "local" | "remote", address: string, token: string, expectedInstance?: string): Promise<void> {
    const url = backendOrigin(address);
    if (typeof token !== "string" || !token.trim() || /[\r\n]/.test(token)) throw new Error("An access token is required.");
    this.disconnect();
    const id = randomUUID();
    this.publish({ id, kind, status: "connecting", url, error: undefined });
    const controller = new AbortController();
    this.controllers.add(controller);
    try {
      const instance = await this.identify(url, token, AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]));
      if (this.info.id !== id) throw new Error("Connection changed.");
      if (expectedInstance && instance !== expectedInstance) throw new Error("Local backend identity did not match its launch.");
      this.target = { url, token, instance };
      this.publish({ status: "ready", error: undefined });
    } catch (error) {
      if (this.info.id === id) this.publish({ id: "", status: "failed", error: (error as Error).message });
      throw error;
    } finally { this.controllers.delete(controller); }
  }

  disconnect(): void {
    this.target = undefined;
    this.info = { id: "", kind: null, status: "disconnected" };
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
    for (const socket of this.sockets) socket.terminate();
    this.sockets.clear();
    this.emit("change", { ...this.info });
  }

  launchingLocal(): void { this.publish({ id: "", kind: "local", status: "connecting", url: undefined, error: undefined }); }
  stopping(value: boolean): void { this.publish({ status: value ? "stopping" : "ready", error: undefined }); }
  failed(message: string): void {
    if (this.info.status === "failed") return;
    this.publish({ status: "failed", error: message });
    // The renderer reconnects on closed; an HTTP failure must also recover when
    // an otherwise healthy results socket would have stayed open indefinitely.
    for (const socket of this.sockets) socket.terminate();
  }

  async request(path: string, init: RequestInit = {}, connectionId = this.info.id): Promise<Response> {
    const target = this.target;
    if (!target || !connectionId || connectionId !== this.info.id) return proxyError(409, "Connection changed or disconnected.");
    if (this.info.status === "failed" || this.info.status === "connecting") return proxyError(503, "Backend connection is unavailable.");
    const method = (init.method ?? "GET").toUpperCase();
    if (this.info.status === "stopping" && !["GET", "HEAD"].includes(method)) return proxyError(409, "The backend is stopping.");
    if (!["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].includes(method)) return proxyError(405, "Method not allowed.");
    let destination: string;
    try { destination = target.url + apiPath(path); }
    catch { return proxyError(400, "Invalid API path."); }
    const controller = new AbortController();
    this.controllers.add(controller);
    const headers = new Headers({ Authorization: `Bearer ${target.token}` });
    const incoming = new Headers(init.headers);
    for (const name of ["Accept", "Content-Type"]) {
      const value = incoming.get(name);
      if (value) headers.set(name, value);
    }
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(60_000), ...(init.signal ? [init.signal] : [])]);
    try {
      const response = await this.fetcher(destination, {
        ...init, method, headers, signal, redirect: "error", credentials: "omit",
      });
      if (this.info.id !== connectionId) { controller.abort(); return proxyError(409, "Connection changed."); }
      // Keep cancellation alive through the response body, including streamed data.
      const responseHeaders = new Headers();
      for (const name of ["Content-Type", "Content-Disposition"]) {
        const value = response.headers.get(name);
        if (value) responseHeaders.set(name, value);
      }
      responseHeaders.set("Cache-Control", "no-store");
      if (!response.body) {
        this.controllers.delete(controller);
        return new Response(null, { status: response.status, headers: responseHeaders });
      }
      const reader = response.body.getReader();
      const controllers = this.controllers;
      const current = () => this.info.id === connectionId;
      return new Response(new ReadableStream({
        async pull(stream) {
          try {
            const next = await reader.read();
            if (!current()) throw new Error("Connection changed.");
            if (next.done) { controllers.delete(controller); stream.close(); }
            else stream.enqueue(next.value);
          } catch (error) { controllers.delete(controller); controller.abort(); stream.error(error); }
        },
        async cancel() { controllers.delete(controller); controller.abort(); await reader.cancel().catch(() => {}); },
      }), { status: response.status, headers: responseHeaders });
    } catch (error) {
      this.controllers.delete(controller);
      if (!signal.aborted && this.info.id === connectionId) this.failed("Connection lost; backend state is unknown.");
      return proxyError(503, signal.aborted ? "Request canceled; check backend state before retrying." : "Backend request failed.");
    }
  }

  subscribe(id: string, listener: (event: ResultsEvent) => void): () => void {
    let socket: WebSocket | undefined;
    let canceled = false;
    const target = this.target;
    const emit = (event: ResultsEvent) => { if (!canceled && id === this.info.id) listener(event); };
    void (async () => {
      try {
        if (!target || id !== this.info.id) throw new Error("Connection changed.");
        const instance = await this.identify(target.url, target.token);
        if (canceled || id !== this.info.id) return;
        if (instance !== target.instance) {
          const { kind } = this.info;
          await this.connect(kind!, target.url, target.token);
          return;
        }
        socket = new WebSocket(target.url.replace(/^http/, "ws") + "/api/v1/ws/results", {
          headers: { Authorization: `Bearer ${target.token}` },
          followRedirects: false, handshakeTimeout: 10_000, maxPayload: 1024 * 1024,
        });
        this.sockets.add(socket);
        socket.on("open", () => { if (id === this.info.id && this.info.status !== "stopping") this.publish({ status: "ready", error: undefined }); emit({ type: "open" }); });
        socket.on("message", data => emit({ type: "message", data: data.toString() }));
        socket.on("error", () => {
          if (!canceled && id === this.info.id && this.info.status !== "stopping") this.failed("Results connection failed; backend state is unknown.");
          emit({ type: "error", data: "Results connection failed." });
        });
        socket.on("close", () => {
          this.sockets.delete(socket!);
          if (!canceled && id === this.info.id && this.info.status !== "stopping") this.failed("Connection lost; backend state is unknown.");
          emit({ type: "closed" });
        });
      } catch {
        if (!canceled && id === this.info.id) this.failed("Results connection failed; backend state is unknown.");
        emit({ type: "closed" });
      }
    })();
    return () => { canceled = true; if (socket) { this.sockets.delete(socket); socket.terminate(); } };
  }
}

/** The immutable initiator identity is supplied by Electron, never by page headers. */
export async function handleApiRequest(connection: BackendConnection,
  request: Request & { initiatorOrigin?: string }, devOrigin?: string): Promise<Response> {
  const origin = request.initiatorOrigin;
  if (origin !== "app://omnibci" && (!devOrigin || origin !== devOrigin)) return proxyError(403, "Untrusted request origin.");
  const cors = new Headers();
  if (devOrigin && origin === devOrigin) {
    cors.set("Access-Control-Allow-Origin", devOrigin);
    cors.set("Vary", "Origin");
  }
  if (request.method === "OPTIONS") {
    const method = request.headers.get("Access-Control-Request-Method");
    const names = (request.headers.get("Access-Control-Request-Headers") ?? "").toLowerCase().split(",").map(name => name.trim()).filter(Boolean);
    if (!method || !["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].includes(method)
      || names.some(name => !["accept", "content-type", CONNECTION_HEADER.toLowerCase()].includes(name))) {
      return proxyError(403, "Unsupported desktop preflight.");
    }
    cors.set("Access-Control-Allow-Methods", method);
    cors.set("Access-Control-Allow-Headers", ["Accept", "Content-Type", CONNECTION_HEADER].join(", "));
    return new Response(null, { status: 204, headers: cors });
  }
  const url = new URL(request.url);
  const response = await connection.request(url.pathname + url.search, {
    method: request.method, headers: request.headers, body: request.body, signal: request.signal,
    ...(request.body ? { duplex: "half" } : {}),
  } as RequestInit, request.headers.get(CONNECTION_HEADER) ?? "");
  cors.forEach((value, name) => response.headers.set(name, value));
  return response;
}
