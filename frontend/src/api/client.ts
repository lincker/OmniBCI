/**
 * Thin typed REST client over the generated OpenAPI types.
 *
 * All success responses use the ApiResponse envelope {success, message, data};
 * all failures use ErrorResponse {success:false, code, message, detail?}.
 * Branch on `code`, never on message text.
 */
import type { components } from "./rest-types";

type S = components["schemas"];
export type EngineStatus = S["EngineStatusResponse"];
export type AcquisitionStatus = S["AcquisitionStatusResponse"];
export type SessionInfo = S["SessionResponse"];
export type Trial = S["TrialResponse"];
export type ErrorCode = S["ErrorCode"];

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
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T | null> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const code = body?.code ?? "UNKNOWN";
    throw new ApiError(res.status, code, body?.message ?? res.statusText);
  }
  return (body as ApiEnvelope<T>).data;
}

export const api = {
  engineStatus: () => get<EngineStatus>("/api/v1/engine/status"),
  acquisitionStatus: () => get<AcquisitionStatus>("/api/v1/acquisition/status"),
  listSessions: () =>
    get<S["SessionListResponse"]>("/api/v1/sessions"),

  /** null = no active session (NO_ACTIVE_SESSION is a normal state, not an error). */
  activeSession: async (): Promise<SessionInfo | null> => {
    try {
      return await get<SessionInfo>("/api/v1/sessions/active");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },

  sessionTrials: (sessionId: string) =>
    get<Trial[]>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/trials`),
};
