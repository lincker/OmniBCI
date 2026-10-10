/** Signal setup is saved separately from the accepted runtime configuration. */
import { ApiError } from "./client";
import { configRequest } from "./plugin-config";
import { requestJson } from "./transport";
import type { components } from "./rest-types";

type S = components["schemas"];
export type SignalChannel = S["SignalChannelModel"];
export type SourceMetadata = S["SourceMetadataModel"];
export type SignalAssignment = S["ChannelAssignmentModel"];
export type BipolarOutput = S["BipolarOutputModel"];
/** The read endpoint fills these defaults even though request fields are optional. */
export type SignalSetup = Omit<S["SignalSetupModel"], "assignments" | "outputs" | "reference" | "excluded_channels"> & {
  assignments: SignalAssignment[];
  outputs: (string | BipolarOutput)[];
  reference: S["ReferenceModel"] & { channels: string[] };
  excluded_channels: string[];
};
/** Empty optional values are omitted from responses by the backend. */
export type SignalConfiguration = Omit<S["SignalSetupResponse"], "setup" | "accepted_setup" | "active_setup"> & {
  setup?: SignalSetup | null;
  accepted_setup?: SignalSetup | null;
  active_setup?: SignalSetup | null;
};
export type SignalPreview = Omit<S["SignalPreviewResponse"], "plan"> & {
  plan: { source: SourceMetadata; output_channels: SignalChannel[]; coefficients: number[][]; fingerprint: string; warnings: string[] };
};
export type SignalTemplate = S["SignalTemplateResponse"];

async function metadata(refresh = false): Promise<SourceMetadata> {
  const { response, body } = await requestJson(`/api/v1/acquisition/metadata${refresh ? "/refresh" : ""}`, {
    method: refresh ? "POST" : "GET", headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new ApiError(response.status, body?.code ?? "UNKNOWN", body?.message ?? response.statusText);
  return body.data as SourceMetadata;
}

export const signalConfigApi = {
  get: () => configRequest<SignalConfiguration>("/signal"),
  save: (setup: SignalSetup, expected_revision: string) =>
    configRequest<SignalConfiguration>("/signal", "PUT", { setup, expected_revision }),
  preview: (setup: SignalSetup, source?: SourceMetadata) =>
    configRequest<SignalPreview>("/signal/preview", "POST", { setup, ...(source ? { metadata: source } : {}) }),
  templates: () => configRequest<S["SignalTemplatesResponse"]>("/signal/templates"),
  template: (name: string) => configRequest<SignalTemplate>(`/signal/templates/${encodeURIComponent(name)}`),
  metadata,
};
