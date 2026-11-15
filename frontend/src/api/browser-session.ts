/** Browser credentials stay in the Server's HttpOnly cookie. */
import type { components } from "./rest-types";
import { abortPendingRequests } from "../desktop";

export type ServerInfo = components["schemas"]["ServerInfoResponse"];
export class AuthenticationRequired extends Error {
  constructor() { super("A valid access token is required."); }
}

let ready = true;
let generation = 0;
let instanceId: string | undefined;
const listeners = new Set<() => void>();

export const browserSessionReady = () => ready;
export function acceptBrowserSession(info: ServerInfo) {
  generation++;
  instanceId = info.instance_id;
  ready = true;
}
export function invalidateBrowserSession() {
  if (!ready) return;
  generation++;
  ready = false;
  abortPendingRequests();
  listeners.forEach((listener) => listener());
}
export function onBrowserSessionLost(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function readBrowserSession(token?: string, signal?: AbortSignal): Promise<ServerInfo> {
  const response = await fetch(token === undefined ? "/api/v1/server/info" : "/api/v1/server/session", {
    method: token === undefined ? "GET" : "POST",
    headers: token === undefined ? undefined : { Authorization: `Bearer ${token}` },
    credentials: "same-origin", cache: "no-store", redirect: "error",
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(10_000)]) : AbortSignal.timeout(10_000),
  });
  if (response.status === 401) throw new AuthenticationRequired();
  if (!response.ok) throw new Error(`Server connection failed (HTTP ${response.status}).`);
  const value = await response.json();
  const info = value?.data;
  if (value?.success !== true || typeof info?.instance_id !== "string" || !info.instance_id
    || typeof info?.auth_enabled !== "boolean" || info?.desktop_api_version !== 1) {
    throw new Error("The Server returned an invalid connection response.");
  }
  return info;
}

/** Reconnect only to the same instance; the workspace approves a replacement. */
export async function verifyBrowserSession(): Promise<boolean> {
  if (!ready) return false;
  const expected = instanceId;
  const owner = generation;
  try {
    const info = await readBrowserSession();
    if (owner !== generation) return false;
    if (expected && info.instance_id !== expected) { invalidateBrowserSession(); return false; }
    return ready;
  } catch {
    if (owner === generation) invalidateBrowserSession();
    return false;
  }
}

export async function closeBrowserSession() {
  const response = await fetch("/api/v1/server/session", {
    method: "DELETE", credentials: "same-origin", cache: "no-store", redirect: "error",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok && response.status !== 401) throw new Error(`Disconnect failed (HTTP ${response.status}).`);
  invalidateBrowserSession();
}
