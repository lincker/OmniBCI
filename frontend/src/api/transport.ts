/** One request boundary for browser and desktop, including response consumption. */
import { currentConnection, desktopBridge, trackRequest } from "../desktop";

export async function requestApi<T>(path: string, options: RequestInit,
  consume: (response: Response) => Promise<T>): Promise<T> {
  if (!path.startsWith("/api/v1/") || path.includes("\\")) throw new Error("Invalid API path.");
  const desktop = desktopBridge();
  const owner = currentConnection();
  if (desktop && (!owner?.id || owner.status !== "ready")) {
    throw new Error("Backend connection is unavailable. Reconnect before sending commands.");
  }
  const controller = new AbortController();
  const untrack = trackRequest(controller);
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  const timeout = setTimeout(abort, 60_000);
  const headers = new Headers(options.headers);
  if (desktop) headers.set("X-OmniBCI-Connection-Id", owner!.id);
  try {
    const response = await fetch(desktop ? `app://omnibci${path}` : path, {
      ...options, headers, signal: controller.signal, redirect: "error",
    });
    const result = await consume(response);
    if (controller.signal.aborted || (desktop && currentConnection()?.id !== owner!.id)) {
      throw new DOMException("The backend connection changed or the request was cancelled.", "AbortError");
    }
    return result;
  } catch (cause) {
    if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) {
      throw new Error("The request was interrupted. The backend may have applied the change; check its current state before retrying.");
    }
    throw cause;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
    untrack();
  }
}

export function requestJson(path: string, options: RequestInit = {}) {
  return requestApi(path, options, async (response) => ({ response, body: await response.json().catch(() => null) }));
}
