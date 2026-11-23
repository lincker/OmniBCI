"""Construct a pipeline with the effective, prepared signal description."""
from dataclasses import replace

from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin


def mapped_input(plan):
    source = plan.source
    output = source if plan.is_identity else replace(
        source, channels=plan.output_channels,
    )
    return output


def load_signal_pipeline(snapshot, plan):
    from .pipeline_configuration import load_filters
    input_signal = mapped_input(plan)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, input_signal=input_signal)
    detector = load_configured_plugin(snapshot, "processing.epoch_detector", "epoch_detector", EpochDetector,
                                      input_signal=input_signal)
    return decoder, detector, load_filters(snapshot, input_signal)
