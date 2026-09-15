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
export type ConfigEntry = S["ConfigEntry"];
export type ConfigData = S["ConfigResponse"];
export type PatchConfigResult = S["PatchConfigResponse"];
export type ProfileList = S["ProfileListResponse"];
export type ParadigmInfo = S["ParadigmInfo"];
export type ParadigmsResponse = S["ParadigmsResponse"];
export type ModuleConfig = S["ModuleConfigResponse"];
export type ModuleProfile = S["ModuleProfileResponse"];
export type ModuleConfigField = S["ModuleConfigField"];
export interface StimulationStatus {
  active: boolean;
  process_alive: boolean;
  engine_connected: boolean;
  paradigm: string | null;
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

async function send<T>(method: "POST" | "DELETE" | "PATCH" | "PUT", path: string, body?: unknown): Promise<T | null> {
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
  stimulationStatus: () => get<StimulationStatus>("/api/v1/stimulation/status"),
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
  trialStart: (n_trials = 1, trial_params?: number[]) =>
    post("/api/v1/stimulation/trial/start", { n_trials, trial_params }),

  createSession: (paradigm?: string) =>
    post<SessionInfo>("/api/v1/sessions", paradigm ? { paradigm } : {}),
  endSession: (sessionId: string) =>
    del(`/api/v1/sessions/${encodeURIComponent(sessionId)}`),

  recordingStart: () => post<{ session_id: string }>("/api/v1/recording/start"),
  recordingStop: () => post<{ session_id: string }>("/api/v1/recording/stop"),

  // ---- plugin discovery ----------------------------------------------------
  /** List plugins by category: "paradigm" | "decoder" | "acquisition" | "effector" */
  listPlugins: (category: string) => get<ParadigmsResponse>(`/api/v1/plugins/${encodeURIComponent(category)}`),
  /** @deprecated Use listPlugins("paradigm") */
  listParadigms: () => get<ParadigmsResponse>("/api/v1/stimulation/paradigms"),

  // ---- layered config (stage 4) ------------------------------------------
  getConfig: () => get<ConfigData>("/api/v1/config"),
  patchConfig: (updates: Record<string, unknown>, scope = "default", description?: string) =>
    send<PatchConfigResult>("PATCH", "/api/v1/config", { updates, scope, description }),
  listProfiles: () => get<ProfileList>("/api/v1/config/profiles"),
  putProfile: (name: string, updates: Record<string, unknown>, description?: string) =>
    send<PatchConfigResult>("PUT", `/api/v1/config/profiles/${encodeURIComponent(name)}`,
      { updates, description }),
  deleteProfile: (name: string) =>
    del(`/api/v1/config/profiles/${encodeURIComponent(name)}`),
  activateProfile: (name: string) =>
    post(`/api/v1/config/profiles/${encodeURIComponent(name)}/activate`),
  deactivateProfile: () => post("/api/v1/config/profiles/deactivate"),

  getAcquisitionConfig: () => get<ModuleConfig>("/api/v1/config/modules/acquisition"),
  listAcquisitionProfiles: () =>
    get<S["ModuleProfileListResponse"]>("/api/v1/config/modules/acquisition/profiles"),
  createAcquisitionProfile: (body: S["CreateModuleProfileRequest"]) =>
    post<ModuleProfile>("/api/v1/config/modules/acquisition/profiles", body),
  updateAcquisitionProfile: (id: string, body: S["UpdateModuleProfileRequest"]) =>
    send<ModuleProfile>("PUT", `/api/v1/config/modules/acquisition/profiles/${encodeURIComponent(id)}`, body),
  deleteAcquisitionProfile: (id: string) =>
    del(`/api/v1/config/modules/acquisition/profiles/${encodeURIComponent(id)}`),
  selectAcquisitionProfile: (profileId: string | null) =>
    send<ModuleConfig>("PUT", "/api/v1/config/modules/acquisition/active-profile", { profile_id: profileId }),
};
