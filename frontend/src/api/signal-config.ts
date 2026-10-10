/** Signal preview reads Acquisition montage and decoder-owned channel input. */
import { configRequest } from "./plugin-config";
import type { components } from "./rest-types";

type S = components["schemas"];
export type SignalChannel = S["SignalChannelModel"];
export type SourceMetadata = S["SourceMetadataModel"];
export type BipolarOutput = S["BipolarOutputModel"];
/** The read endpoint fills these defaults even though request fields are optional. */
export type SignalSetup = Omit<S["SignalSetupModel"], "assignments" | "outputs" | "reference" | "excluded_channels"> & {
  assignments: S["ChannelAssignmentModel"][];
  outputs: (string | BipolarOutput)[];
  reference: S["ReferenceModel"] & { channels: string[] };
  excluded_channels: string[];
};
export type ProcessingInput = Omit<S["ProcessingInputModel"], "outputs" | "reference" | "excluded_channels"> & {
  outputs: (string | BipolarOutput)[];
  reference: S["ReferenceModel"] & { channels: string[] };
  excluded_channels: string[];
};
/** Empty optional values are omitted from responses by the backend. */
export type SignalConfiguration = Omit<S["SignalSetupResponse"], "setup" | "accepted_setup" | "active_setup" | "legacy_setup"> & {
  setup?: SignalSetup | null;
  accepted_setup?: SignalSetup | null;
  active_setup?: SignalSetup | null;
  legacy_setup?: SignalSetup | null;
};
export type SignalPreview = Omit<S["SignalPreviewResponse"], "plan"> & {
  plan: { source: SourceMetadata; output_channels: SignalChannel[]; coefficients: number[][]; fingerprint: string; warnings: string[] };
};

export const signalConfigApi = {
  get: () => configRequest<SignalConfiguration>("/signal"),
  preview: (processing_input: ProcessingInput, source?: SourceMetadata) =>
    configRequest<SignalPreview>("/signal/preview", "POST", { processing_input, ...(source ? { metadata: source } : {}) }),
};
