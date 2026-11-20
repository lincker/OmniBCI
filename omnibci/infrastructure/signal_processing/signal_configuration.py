"""Construct a pipeline with the effective, prepared signal description."""
from dataclasses import replace

from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
from omnibci.sdk import PluginContext


def signal_context(snapshot, plan):
    source = plan.source
    output = source if plan.is_identity else replace(
        source, channels=plan.output_channels,
    )
    return PluginContext(input_signal=output)


def load_signal_pipeline(snapshot, plan):
    from .pipeline_configuration import load_filters
    context = signal_context(snapshot, plan)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, context=context)
    detector = load_configured_plugin(snapshot, "processing.epoch_detector", "epoch_detector", EpochDetector,
                                      context=context)
    return decoder, detector, load_filters(snapshot, context)
