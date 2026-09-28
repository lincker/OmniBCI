/** Package and signed-repository requests use generated OpenAPI contracts. */
import { ApiError, type ApiEnvelope } from "./client";
import type { components } from "./rest-types";

type S = components["schemas"];
export type InstalledPackage = S["PackageVersionResponse"];
export type MarketPackage = S["MarketPackageResponse"];
export type ProfileVersionProposal = S["ProfileVersionProposalResponse"];
export type ProfileVersionRequest = S["ProfileVersionRequest"];
type Installation = S["PackageInstallationResponse"];

async function request<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  const upload = data instanceof File;
  const response = await fetch(`/api/v1${path}`, {
    method, headers: { Accept: "application/json", "Content-Type": upload ? "application/zip" : "application/json" },
    body: data === undefined ? undefined : upload ? data : JSON.stringify(data),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, body?.code ?? "UNKNOWN", body?.message ?? response.statusText);
  return (body as ApiEnvelope<T>).data as T;
}

function packagePath(item: InstalledPackage) {
  return `/plugin-packages/${encodeURIComponent(item.package_id)}/${encodeURIComponent(item.version)}`;
}

export const pluginMarketApi = {
  installed: () => request<InstalledPackage[]>("/plugin-packages"),
  upload: (file: File) => request<Installation>("/plugin-packages/install", "POST", file),
  setEnabled: (item: InstalledPackage, enabled: boolean) =>
    request<InstalledPackage>(`${packagePath(item)}/enabled`, "PUT", { enabled }),
  remove: (item: InstalledPackage) => request<null>(packagePath(item), "DELETE"),
  catalog: () => request<MarketPackage[]>("/market/catalog"),
  install: (item: MarketPackage) => request<Installation>("/market/install", "POST", {
    package_id: item.package_id, version: item.version,
  }),
  propose: (body: ProfileVersionRequest) => request<ProfileVersionProposal>("/plugin-packages/profile-version-proposals", "POST", body),
  createVersionProfile: (body: S["CreateVersionProfileRequest"]) =>
    request<S["PluginProfileResponse"]>("/plugin-packages/version-profiles", "POST", body),
};
