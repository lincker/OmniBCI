/** Signal preview reads Acquisition montage and decoder-owned channel input. */
import { configRequest } from "./plugin-config";
import type { PluginSelection } from "./plugin-config";
import type { components } from "./rest-types";

type S = components["schemas"];
export type SignalChannel = S["SignalChannelModel"];
export type SourceMetadata = S["SourceMetadataModel"];
export type ChannelBinding = S["ChannelBindingModel"];
export type BipolarOutput = S["BipolarOutputModel"];
/** The read endpoint fills these defaults even though request fields are optional. */
export type SignalSetup = Omit<S["SignalSetupModel"], "assignments" | "outputs" | "reference" | "excluded_channels"> & {
  assignments: S["ChannelAssignmentModel"][];
  outputs: (string | ChannelBinding | BipolarOutput)[];
  reference: S["ReferenceModel"] & { channels: string[] };
  excluded_channels: string[];
};
export type ProcessingInput = Omit<S["ProcessingInputModel"], "outputs" | "reference" | "excluded_channels"> & {
  outputs: (string | ChannelBinding | BipolarOutput)[];
  reference: S["ReferenceModel"] & { channels: string[] };
  excluded_channels: string[];
};
/** Empty optional values are omitted from responses by the backend. */
export type SignalConfiguration = Omit<S["SignalSetupResponse"], "setup" | "accepted_setup" | "active_setup" | "legacy_setup" | "processing_input" | "accepted_input" | "active_input"> & {
  setup?: SignalSetup | null;
  accepted_setup?: SignalSetup | null;
  active_setup?: SignalSetup | null;
  legacy_setup?: SignalSetup | null;
  processing_input?: ProcessingInput | null;
  accepted_input?: ProcessingInput | null;
  active_input?: ProcessingInput | null;
};
export type SignalPreview = Omit<S["SignalPreviewResponse"], "plan"> & {
  plan: { source: SourceMetadata; output_channels: SignalChannel[]; coefficients: number[][]; fingerprint: string; warnings: string[] };
};

export const signalConfigApi = {
  get: () => configRequest<SignalConfiguration>("/signal"),
  preview: (processing_input: ProcessingInput, expected_source_revision?: string, decoder_selection?: PluginSelection) =>
    configRequest<SignalPreview>("/signal/preview", "POST", { processing_input,
      ...(expected_source_revision !== undefined ? { expected_source_revision } : {}),
      ...(decoder_selection !== undefined ? { decoder_selection } : {}) }),
};
