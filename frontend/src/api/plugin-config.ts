/** Plugin configuration endpoints use the generated OpenAPI contract. */
import { ApiError, type ApiEnvelope } from "./client";
import type { components } from "./rest-types";
import { requestJson } from "./transport";
import type { ConfigurationSchema } from "../SchemaForm";
import type { ProcessingInput } from "./signal-config";

type S = components["schemas"];
export type PluginKind = S["PluginProfileResponse"]["kind"];
export type PluginSelection = S["PluginSelectionResponse"];
export type PluginDefinition = Omit<S["PluginConfigurationResponse"], "schema"> & {
  schema: ConfigurationSchema | null;
};
export type PluginProfile = S["PluginProfileResponse"];
export type ModuleActivation = S["ModuleActivationResponse"];
export type ConfigurationModule = ModuleActivation["module"];
export type ModulesResponse = S["ModulesResponse"];
export type ModuleHistory = S["ModuleHistoryResponse"];
export type ProfileHistory = S["ProfileHistoryResponse"];
export type RevisionMap = Record<string, string | null>;
export const configurationModules: ConfigurationModule[] = ["acquisition", "processing", "stimulation", "actuation"];

export function selectionKey(selection: PluginSelection) {
  return JSON.stringify([selection.package_id, selection.contribution_id, selection.version]);
}

export function selectionId(selection: PluginSelection) {
  return `${selection.package_id}/${selection.contribution_id}@${selection.version}`;
}

async function request<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  const { response, body } = await requestJson(`/api/v1/config${path}`, {
    method, headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (!response.ok) throw new ApiError(response.status, body?.code ?? "UNKNOWN", body?.message ?? response.statusText);
  return (body as ApiEnvelope<T>).data as T;
}

export { request as configRequest };

export const pluginConfigApi = {
  catalog: (kind: PluginKind) => request<PluginDefinition[]>(`/plugin-catalog/${kind}`),
  profiles: (kind?: PluginKind) => request<PluginProfile[]>(`/plugin-profiles${kind ? `?kind=${kind}` : ""}`),
  modules: () => request<ModulesResponse>("/modules"),
  module: (module: ConfigurationModule) => request<ModuleActivation>(`/modules/${module}`),
  engine: () => request<S["EngineSettingsResponse"]>("/engine"),
  moduleHistory: (module: ConfigurationModule) => request<ModuleHistory[]>(`/modules/${module}/history`),
  moduleRevision: (module: ConfigurationModule, entryId: string) => request<ModuleActivation>(`/modules/${module}/history/${encodeURIComponent(entryId)}`),
  moduleRevertPlan: (module: ConfigurationModule, entryId: string) => request<S["RuntimePlanResponse"]>(`/modules/${module}/revert-plan`, "POST", { entry_id: entryId }),
  profileHistory: (id: string) => request<ProfileHistory[]>(`/plugin-profiles/${encodeURIComponent(id)}/history`),
  profileRevert: (profile: PluginProfile, revision: number) => request<PluginProfile>(`/plugin-profiles/${encodeURIComponent(profile.profile_id)}/revert`, "POST", { revision, expected_revision: profile.revision }),
  create: (kind: PluginKind, selection: PluginSelection, name: string, parameters: Record<string, unknown>, processing_input?: ProcessingInput) =>
    request<PluginProfile>("/plugin-profiles", "POST", { kind, selection, name, parameters,
      ...(processing_input === undefined ? {} : { processing_input }) }),
  delete: (profile: PluginProfile) => request<null>(
    `/plugin-profiles/${encodeURIComponent(profile.profile_id)}?expected_revision=${profile.revision}`, "DELETE"),
};
