/** Package and signed-repository requests use generated OpenAPI contracts. */
import type { components } from "./rest-types";
import { requestEnvelope } from "./transport";

type S = components["schemas"];
export type InstalledPackage = S["PackageVersionResponse"];
export type MarketPackage = S["MarketPackageResponse"];
export type ProfileVersionProposal = S["ProfileVersionProposalResponse"];
export type ProfileVersionRequest = S["ProfileVersionRequest"];
type Installation = S["PackageInstallationResponse"];

async function request<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  return await requestEnvelope<T>(`/api/v1${path}`, method, data) as T;
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
