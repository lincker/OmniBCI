/** Plugin configuration endpoints use the generated OpenAPI contract. */
import { ApiError, type ApiEnvelope } from "./client";
import type { components } from "./rest-types";
import type { ConfigurationSchema } from "../SchemaForm";

type S = components["schemas"];
export type PluginKind = S["PluginProfileResponse"]["kind"];
export type PluginSelection = S["PluginSelectionResponse"];
export type PluginDefinition = Omit<S["PluginConfigurationResponse"], "schema"> & {
  schema: ConfigurationSchema | null;
};
export type PluginProfile = S["PluginProfileResponse"];
export type ProfileBinding = S["ProfileBindingResponse"];
export type Activation = S["ActivationResponse"];
export type BindingSelection = S["SelectProfileBindingRequest"];

export function selectionKey(selection: PluginSelection) {
  return JSON.stringify([selection.package_id, selection.contribution_id, selection.version]);
}

async function request<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/config${path}`, {
    method, headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, body?.code ?? "UNKNOWN", body?.message ?? response.statusText);
  return (body as ApiEnvelope<T>).data as T;
}

export { request as configRequest };

export const pluginConfigApi = {
  catalog: (kind: PluginKind) => request<PluginDefinition[]>(`/plugin-catalog/${kind}`),
  profiles: () => request<PluginProfile[]>("/plugin-profiles"),
  activation: () => request<Activation | null>("/activation"),
  create: (kind: PluginKind, selection: PluginSelection, name: string, parameters: Record<string, unknown>) =>
    request<PluginProfile>("/plugin-profiles", "POST", { kind, selection, name, parameters }),
  update: (profile: PluginProfile, name: string, parameters: Record<string, unknown>) =>
    request<PluginProfile>(`/plugin-profiles/${encodeURIComponent(profile.profile_id)}`, "PUT",
      { name, parameters, expected_revision: profile.revision }),
  delete: (profile: PluginProfile) => request<null>(
    `/plugin-profiles/${encodeURIComponent(profile.profile_id)}?expected_revision=${profile.revision}`, "DELETE"),
  accept: (bindings: BindingSelection[], expected_revision: string | null, refresh_slots: string[]) =>
    request<Activation>("/activation", "PUT", { bindings, expected_revision, refresh_slots }),
};
