/**
 * Thin typed REST client over the generated OpenAPI types.
 *
 * All success responses use the ApiResponse envelope {success, message, data};
 * all failures use ErrorResponse {success:false, code, message, detail?}.
 * Branch on `code`, never on message text.
 */
import type { components } from "./rest-types";
import { requestJson } from "./transport";

type S = components["schemas"];
export type EngineStatus = S["EngineStatusResponse"];
export type AcquisitionStatus = S["AcquisitionStatusResponse"];
export type DataSourceMetadata = S["AcquisitionMetadataResponse"];
export type Trial = S["TrialResponse"];
export type ResultsSnapshot = S["ResultsResponse"];
export type ConfigEntry = S["ConfigEntry"];
export type ConfigData = S["ConfigResponse"];
export type PatchConfigResult = S["PatchConfigResponse"];
export type ParadigmInfo = S["ParadigmInfo"];
export type ParadigmsResponse = S["ParadigmsResponse"];
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
  const { response: res, body } = await requestJson(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    const code = body?.code ?? "UNKNOWN";
    throw new ApiError(res.status, code, body?.message ?? res.statusText);
  }
  return (body as ApiEnvelope<T>).data;
}

async function send<T>(method: "POST" | "DELETE" | "PATCH" | "PUT", path: string, body?: unknown): Promise<T | null> {
  const { response: res, body: resBody } = await requestJson(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const code = resBody?.code ?? "UNKNOWN";
    throw new ApiError(res.status, code, resBody?.message ?? res.statusText);
  }
  return (resBody as ApiEnvelope<T>).data;
}

const post = <T = unknown,>(path: string, body?: unknown) => send<T>("POST", path, body);

export const api = {
  backendLogs: () => get<S["BackendLogsResponse"]>("/api/v1/system/logs"),
  engineStatus: () => get<EngineStatus>("/api/v1/engine/status"),
  acquisitionStatus: () => get<AcquisitionStatus>("/api/v1/acquisition/status"),
  stimulationStatus: () => get<StimulationStatus>("/api/v1/stimulation/status"),

  results: () => get<ResultsSnapshot>("/api/v1/results"),

  // ---- control actions (stage 2, lock-free) ------------------------------
  engineStart: () => post("/api/v1/engine/start"),
  engineStop: () => post("/api/v1/engine/stop"),

  acqConnect: () => post("/api/v1/acquisition/connect"),
  acqDisconnect: () => post("/api/v1/acquisition/disconnect"),
  acqMetadata: () => post<DataSourceMetadata>("/api/v1/acquisition/metadata/refresh"),

  stimStart: (paradigm?: string) =>
    post("/api/v1/stimulation/start", paradigm ? { paradigm } : {}),
  stimStop: () => post("/api/v1/stimulation/stop"),
  trialStart: (n_trials = 1, trial_params?: number[]) =>
    post("/api/v1/stimulation/trial/start", { n_trials, trial_params }),

  acqStreamStart: () => post("/api/v1/acquisition/stream/start"),
  acqStreamStop: () => post("/api/v1/acquisition/stream/stop"),

  // ---- plugin discovery ----------------------------------------------------
  /** List plugins by category: "paradigm" | "decoder" | "acquisition" | "effector" */
  listPlugins: (category: string) => get<ParadigmsResponse>(`/api/v1/plugins/${encodeURIComponent(category)}`),

  // ---- host settings ------------------------------------------------------
  getConfig: () => get<ConfigData>("/api/v1/config"),
  patchConfig: (updates: Record<string, unknown>, expected_revision: string) =>
    send<PatchConfigResult>("PATCH", "/api/v1/config", { updates, expected_revision }),
};
