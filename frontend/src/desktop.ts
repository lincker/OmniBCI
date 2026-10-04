/** The browser build never imports an Electron runtime module. */
import type { DesktopApi, ConnectionInfo } from "../../desktop/src/contracts";

declare global {
  interface Window { omnibciDesktop?: DesktopApi }
}

export const desktopBridge = () => typeof window === "undefined" ? undefined : window.omnibciDesktop;

let connection: ConnectionInfo | null = null;
const requests = new Set<AbortController>();

export function currentConnection() { return connection; }

/** Invalidate old requests before React mounts the next backend's workspace. */
export function setDesktopConnection(next: ConnectionInfo) {
  if (connection?.id !== next.id || (connection?.status === "ready" && next.status !== "ready")) {
    requests.forEach((request) => request.abort());
    requests.clear();
  }
  connection = next;
}

export function trackRequest(controller: AbortController) {
  requests.add(controller);
  return () => { requests.delete(controller); };
}
