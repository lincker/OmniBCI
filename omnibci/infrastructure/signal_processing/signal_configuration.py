"""Construct a pipeline with the effective, prepared signal description."""
from dataclasses import replace

from omnibci.domain.ports.algorithm import Decoder, EpochDetector
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
from omnibci.sdk import PluginContext


def signal_context(snapshot, plan):
    source = plan.source
    output = source if plan.is_identity else replace(
        source, channels=plan.output_channels,
        trigger_row_index=len(plan.output_channels),
    )
    return PluginContext(
        sample_rate=source.sample_rate,
        channel_names=tuple(channel.name for channel in source.channels),
        config_revision=snapshot.get("CONFIG.revision"),
        acquired_signal=source, input_signal=output,
    )


def load_signal_pipeline(snapshot, plan):
    from .pipeline_configuration import load_filters
    context = signal_context(snapshot, plan)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, context=context)
    detector = load_configured_plugin(snapshot, "processing.epoch_detector", "epoch_detector", EpochDetector,
                                      context=context)
    return decoder, detector, load_filters(snapshot, context)
