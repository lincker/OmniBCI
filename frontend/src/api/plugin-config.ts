/** Plugin configuration endpoints use the generated OpenAPI contract. */
import type { components } from "./rest-types";
import { requestEnvelope } from "./transport";
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
type ModulesResponse = S["ModulesResponse"];
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

export async function configRequest<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  return await requestEnvelope<T>(`/api/v1/config${path}`, method, data) as T;
}

export const pluginConfigApi = {
  catalog: (kind: PluginKind) => configRequest<PluginDefinition[]>(`/plugin-catalog/${kind}`),
  profiles: (kind?: PluginKind) => configRequest<PluginProfile[]>(`/plugin-profiles${kind ? `?kind=${kind}` : ""}`),
  modules: () => configRequest<ModulesResponse>("/modules"),
  module: (module: ConfigurationModule) => configRequest<ModuleActivation>(`/modules/${module}`),
  engine: () => configRequest<S["EngineSettingsResponse"]>("/engine"),
  moduleHistory: (module: ConfigurationModule) => configRequest<ModuleHistory[]>(`/modules/${module}/history`),
  moduleRevision: (module: ConfigurationModule, entryId: string) => configRequest<ModuleActivation>(`/modules/${module}/history/${encodeURIComponent(entryId)}`),
  moduleRevertPlan: (module: ConfigurationModule, entryId: string) => configRequest<S["RuntimePlanResponse"]>(`/modules/${module}/revert-plan`, "POST", { entry_id: entryId }),
  profileHistory: (id: string) => configRequest<ProfileHistory[]>(`/plugin-profiles/${encodeURIComponent(id)}/history`),
  profileRevert: (profile: PluginProfile, revision: number) => configRequest<PluginProfile>(`/plugin-profiles/${encodeURIComponent(profile.profile_id)}/revert`, "POST", { revision, expected_revision: profile.revision }),
  create: (kind: PluginKind, selection: PluginSelection, name: string, parameters: Record<string, unknown>, processing_input?: ProcessingInput) =>
    configRequest<PluginProfile>("/plugin-profiles", "POST", { kind, selection, name, parameters,
      ...(processing_input === undefined ? {} : { processing_input }) }),
  delete: (profile: PluginProfile) => configRequest<null>(
    `/plugin-profiles/${encodeURIComponent(profile.profile_id)}?expected_revision=${profile.revision}`, "DELETE"),
};
