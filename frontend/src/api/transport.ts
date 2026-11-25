/** One request boundary for browser and desktop, including response consumption. */
import { currentConnection, desktopBridge, trackRequest } from "../desktop";
import { AuthenticationRequired, browserSessionReady, invalidateBrowserSession } from "./browser-session";

export async function requestApi<T>(path: string, options: RequestInit,
  consume: (response: Response) => Promise<T>): Promise<T> {
  if (!path.startsWith("/api/v1/") || path.includes("\\")) throw new Error("Invalid API path.");
  const desktop = desktopBridge();
  const owner = currentConnection();
  if (desktop && (!owner?.id || owner.status !== "ready")) {
    throw new Error("Server connection is unavailable. Reconnect before sending commands.");
  }
  if (!desktop && !browserSessionReady()) throw new Error("Server connection is unavailable. Reconnect before sending commands.");
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
    if (controller.signal.aborted) throw new DOMException("The request was cancelled.", "AbortError");
    if (!desktop && response.status === 401) {
      invalidateBrowserSession();
      throw new AuthenticationRequired();
    }
    const result = await consume(response);
    if (controller.signal.aborted || (desktop && currentConnection()?.id !== owner!.id)) {
      throw new DOMException("The server connection changed or the request was cancelled.", "AbortError");
    }
    return result;
  } catch (cause) {
    if (cause instanceof AuthenticationRequired) throw cause;
    if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) {
      throw new Error("The request was interrupted. The server may have applied the change; check its current state before retrying.");
    }
    throw cause;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
    untrack();
  }
}

export interface ApiEnvelope<T> {
  success: boolean;
  message: string;
  data: T | null;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
  }
}

export const errorMessage = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

export function requestJson(path: string, options: RequestInit = {}) {
  return requestApi(path, options, async (response) => ({ response, body: await response.json().catch(() => null) }));
}

export async function requestEnvelope<T>(path: string, method = "GET", data?: unknown): Promise<T | null> {
  const upload = data instanceof File;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (data !== undefined || method !== "GET") {
    headers["Content-Type"] = upload ? "application/zip" : "application/json";
  }
  const { response, body } = await requestJson(path, {
    method,
    headers,
    body: data === undefined ? undefined : upload ? data : JSON.stringify(data),
  });
  if (!response.ok) {
    throw new ApiError(response.status, body?.code ?? "UNKNOWN", body?.message ?? response.statusText, body?.detail);
  }
  return (body as ApiEnvelope<T>).data;
}
