/**
 * Thin typed REST client over the generated OpenAPI types.
 *
 * All success responses use the ApiResponse envelope {success, message, data};
 * all failures use ErrorResponse {success:false, code, message, detail?}.
 * Branch on `code`, never on message text.
 */
import type { components } from "./rest-types";
import { ApiError, errorMessage, requestEnvelope } from "./transport";
import type { ModuleActionRequest, RuntimeOperation, StartReadingRequest } from "./runtime-config";

export { ApiError, errorMessage };

type S = components["schemas"];
export type EngineStatus = S["EngineStatusResponse"];
type AcquisitionStatus = S["AcquisitionStatusResponse"];
export type DataSourceMetadata = S["AcquisitionMetadataResponse"];
export type AcquisitionPreview = S["AcquisitionPreviewResponse"];
export type AcquisitionPreviewChunk = S["AcquisitionPreviewChunkResponse"];
export type PreviewSelection = S["AcquisitionPreviewSelectionModel"];
export type PreviewLimits = S["AcquisitionPreviewLimitsModel"];
export type Trial = S["TrialResponse"];
type ResultsSnapshot = S["ResultsResponse"];
export type EpochList = S["EpochListResponse"];
export type ConfigEntry = S["ConfigEntry"];
export type ConfigData = S["ConfigResponse"];
type PatchConfigResult = S["PatchConfigResponse"];
type StimulationStatus = S["StimulationStatusResponse"];
type ActuationStatus = S["ActuationStatusResponse"];

const get = <T>(path: string) => requestEnvelope<T>(path);
const post = <T = unknown>(path: string, body?: unknown) => requestEnvelope<T>(path, "POST", body);

export const api = {
  serverLogs: () => get<S["ServerLogsResponse"]>("/api/v1/server/logs"),
  engineStatus: () => get<EngineStatus>("/api/v1/engine/status"),
  acquisitionStatus: () => get<AcquisitionStatus>("/api/v1/acquisition/status"),
  stimulationStatus: () => get<StimulationStatus>("/api/v1/stimulation/status"),
  actuationStatus: () => get<ActuationStatus>("/api/v1/actuation/status"),

  results: () => get<ResultsSnapshot>("/api/v1/results"),
  epochs: (offset = 0, limit = 25) =>
    get<EpochList>(`/api/v1/results/epochs?${new URLSearchParams({ offset: String(offset), limit: String(limit) })}`),

  // ---- control actions ---------------------------------------------------
  engineStart: () => post("/api/v1/engine/start"),
  engineStop: () => post("/api/v1/engine/stop"),
  moduleStart: (module: "processing" | "stimulation" | "actuation", body: ModuleActionRequest) =>
    post<RuntimeOperation>(`/api/v1/${module}/start`, body),
  modulePrepare: (module: "stimulation" | "actuation") => post(`/api/v1/${module}/prepare`),
  moduleStop: (module: "processing" | "stimulation" | "actuation") => post(`/api/v1/${module}/stop`),

  acqConnect: (body: ModuleActionRequest) => post<RuntimeOperation>("/api/v1/acquisition/connect", body),
  acqDisconnect: () => post("/api/v1/acquisition/disconnect"),
  acqMetadata: () => get<DataSourceMetadata>("/api/v1/acquisition/metadata"),
  acqMetadataRefresh: () => post<DataSourceMetadata>("/api/v1/acquisition/metadata/refresh"),
  acqPreviewStart: (body: S["StartAcquisitionPreviewRequest"]) =>
    post<AcquisitionPreview>("/api/v1/acquisition/preview/start", body),
  acqPreview: (generation: string, captureId?: string) =>
    get<AcquisitionPreview>(`/api/v1/acquisition/preview?${new URLSearchParams({ generation, ...(captureId ? { capture_id: captureId } : {}) })}`),
  acqPreviewChunk: (generation: string, captureId: string, sequenceId: number) =>
    get<AcquisitionPreviewChunk>(`/api/v1/acquisition/preview/chunks/${sequenceId}?${new URLSearchParams({ generation, capture_id: captureId })}`),

  trialStart: (n_trials: number, trial_params: S["StartTrialRequest"]["trial_params"]) =>
    post("/api/v1/stimulation/trial/start", { n_trials, trial_params }),

  acqStreamStart: (body: StartReadingRequest) => post("/api/v1/acquisition/stream/start", body),
  acqStreamStop: () => post("/api/v1/acquisition/stream/stop"),

  // ---- host settings ------------------------------------------------------
  getConfig: () => get<ConfigData>("/api/v1/config"),
  patchConfig: (updates: Record<string, unknown>, expected_revision: string) =>
    requestEnvelope<PatchConfigResult>("/api/v1/config", "PATCH", { updates, expected_revision }),
};
