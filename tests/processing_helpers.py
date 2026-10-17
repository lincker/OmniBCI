"""Compose the real processor and application runtime for focused tests."""

from omnibci.application.session_runtime import SessionRuntime
from omnibci.infrastructure.event_translator import DomainEventPublisher
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor


def make_processor(*args, session=None, auto_start_session=True, **kwargs) -> SignalProcessor:
    bus = args[4] if len(args) > 4 else kwargs["event_bus"]
    runtime = SessionRuntime(
        DomainEventPublisher(bus).publish_all, session=session,
        auto_start_session=auto_start_session, config_revision=kwargs.get("config_revision"),
    )
    return SignalProcessor(*args, session_runtime=runtime, **kwargs)
