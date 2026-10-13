/** Processing maps the Acquisition Adapter descriptor into ordered decoder channels. */
import { configRequest } from "./plugin-config";
import type { PluginSelection } from "./plugin-config";
import type { components } from "./rest-types";

type S = components["schemas"];
export type ChannelDescriptor = S["ChannelDescriptorModel"];
export type AcquisitionDescriptor = S["AcquisitionDescriptorModel"];
export type ChannelBinding = S["ChannelBindingModel"];
/** The read endpoint fills the output list even though request fields have defaults. */
export type ProcessingInput = Omit<S["ProcessingInputModel"], "outputs"> & { outputs: ChannelBinding[] };
export type SignalConfiguration = Omit<S["SignalSetupResponse"], "processing_input" | "accepted_input" | "active_input"> & {
  processing_input?: ProcessingInput | null;
  accepted_input?: ProcessingInput | null;
  active_input?: ProcessingInput | null;
};
export type SignalPreview = Omit<S["SignalPreviewResponse"], "plan"> & {
  plan: { source: AcquisitionDescriptor; source_indices: number[]; output_channels: ChannelDescriptor[];
    processing_input: ProcessingInput; config_revision: string; fingerprint: string };
};

export const signalConfigApi = {
  get: () => configRequest<SignalConfiguration>("/signal"),
  preview: (processing_input: ProcessingInput, expected_source_revision?: string, decoder_selection?: PluginSelection) =>
    configRequest<SignalPreview>("/signal/preview", "POST", { processing_input,
      ...(expected_source_revision !== undefined ? { expected_source_revision } : {}),
      ...(decoder_selection !== undefined ? { decoder_selection } : {}) }),
};
