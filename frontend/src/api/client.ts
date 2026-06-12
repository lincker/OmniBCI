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

async function send<T>(method: "POST" | "DELETE", path: string, body?: unknown): Promise<T | null> {
  const res = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const resBody = await res.json().catch(() => null);
  if (!res.ok) {
    const code = resBody?.code ?? "UNKNOWN";
    throw new ApiError(res.status, code, resBody?.message ?? res.statusText);
  }
  return (resBody as ApiEnvelope<T>).data;
}

const post = <T = unknown,>(path: string, body?: unknown) => send<T>("POST", path, body);
const del = <T = unknown,>(path: string) => send<T>("DELETE", path);

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

  // ---- control actions (stage 2, lock-free) ------------------------------
  engineStart: () => post("/api/v1/engine/start"),
  engineStop: () => post("/api/v1/engine/stop"),

  acqConnect: () => post("/api/v1/acquisition/connect"),
  acqDisconnect: () => post("/api/v1/acquisition/disconnect"),
  streamStart: () => post("/api/v1/acquisition/stream/start"),
  streamStop: () => post("/api/v1/acquisition/stream/stop"),

  stimStart: (paradigm?: string) =>
    post("/api/v1/stimulation/start", paradigm ? { paradigm } : {}),
  stimStop: () => post("/api/v1/stimulation/stop"),
  trialStart: (target_index?: number) =>
    post("/api/v1/stimulation/trial/start", target_index != null ? { target_index } : {}),

  createSession: (paradigm?: string) =>
    post<SessionInfo>("/api/v1/sessions", paradigm ? { paradigm } : {}),
  endSession: (sessionId: string) =>
    del(`/api/v1/sessions/${encodeURIComponent(sessionId)}`),

  recordingStart: () => post<{ session_id: string }>("/api/v1/recording/start"),
  recordingStop: () => post<{ session_id: string }>("/api/v1/recording/stop"),
};
