"""
PeerGateway — neuroengine's single boundary to all external peers.

Replaces ServerMessageManagement. Instead of one stimulation server that
broadcasts every registered event to every connected client, the gateway owns
one PeerChannel per peer and routes events per-peer via each channel's RouteSpec.

Lifecycle is readiness-aware and transactional:
  - start()  : async — each channel is ready before the next one starts; a partial
               failure rolls already-started channels back in reverse order.
  - stop()   : async — attempts every channel even when one cleanup fails.
"""

import asyncio
import logging
from typing import Dict

from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig

logger = logging.getLogger(__name__)


class PeerGateway:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._channels: Dict[str, PeerChannel] = {}
        self._started = False
        self._dirty = False
        self._configuration_frozen = False
        self._lifecycle_lock = asyncio.Lock()

    def add_peer(self, config: PeerConfig) -> None:
        if self._configuration_frozen:
            raise RuntimeError("Cannot add peers after gateway startup has begun")
        if config.name in self._channels:
            raise ValueError(f"Peer already registered: {config.name}")
        self._channels[config.name] = PeerChannel(config, self._bus)

    def peer(self, name: str) -> PeerChannel:
        return self._channels[name]

    async def start(self) -> None:
        async with self._lifecycle_lock:
            self._configuration_frozen = True
            if self._started and not self._dirty:
                return

            # A previous failed stop/start rollback must be explicitly completed
            # before a new startup attempt can wire listeners again.
            if self._dirty:
                await self._stop_all_locked()

            attempted = []
            try:
                for channel in self._channels.values():
                    # Record the current channel before awaiting start(): a failed
                    # or cancelled start still creates a cleanup obligation.
                    attempted.append(channel)
                    await channel.start()
            except BaseException as startup_exc:
                cleanup_errors: list[BaseException] = []
                for channel in reversed(attempted):
                    try:
                        await channel.stop()
                    except BaseException as cleanup_exc:
                        cleanup_errors.append(cleanup_exc)
                        logger.warning(
                            "PeerGateway: rollback failed for channel '%s': %s",
                            channel.name,
                            cleanup_exc,
                        )

                dirty_channels = [
                    channel.name
                    for channel in self._channels.values()
                    if channel.has_lifecycle_resources
                ]
                if dirty_channels and not cleanup_errors:
                    cleanup_errors.append(RuntimeError(
                        "PeerGateway channels still own lifecycle resources "
                        "after startup rollback: " + ", ".join(dirty_channels)
                    ))

                self._started = False
                self._dirty = bool(cleanup_errors) or bool(dirty_channels)

                if cleanup_errors:
                    cleanup_group = BaseExceptionGroup(
                        "PeerGateway startup rollback failed",
                        cleanup_errors,
                    )
                    if isinstance(startup_exc, asyncio.CancelledError):
                        # Keep caller cancellation as the primary outcome while
                        # retaining every rollback error in its cause chain.
                        raise startup_exc from cleanup_group
                    raise BaseExceptionGroup(
                        "PeerGateway start and rollback failed",
                        [startup_exc, cleanup_group],
                    )
                raise

            self._started = True
            self._dirty = False

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_all_locked()

    async def _stop_all_locked(self) -> None:
        """Stop every channel and aggregate failures; caller holds the lock."""
        errors: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        was_started = self._started

        # Reverse startup order so downstream channels release resources before
        # the dependencies they were started after.  Channel.stop is itself
        # idempotent, so calling gateway.stop repeatedly remains safe.
        for channel in reversed(list(self._channels.values())):
            try:
                await channel.stop()
            except asyncio.CancelledError as exc:
                # Preserve caller cancellation, but first give every remaining
                # channel its cleanup opportunity.
                cancellation = cancellation or exc
                if exc.__cause__ is not None:
                    errors.append(exc.__cause__)
            except BaseException as exc:
                errors.append(exc)
                logger.warning(
                    "PeerGateway: error stopping channel '%s': %s",
                    channel.name,
                    exc,
                )

        dirty_channels = [
            channel.name
            for channel in self._channels.values()
            if channel.has_lifecycle_resources
        ]
        if dirty_channels and not errors:
            # Defensive invariant: even a buggy/non-conforming channel that
            # returns from stop() while retaining resources must make gateway
            # shutdown fail visibly.
            errors.append(RuntimeError(
                "PeerGateway channels still own lifecycle resources after stop: "
                + ", ".join(dirty_channels)
            ))

        self._dirty = bool(errors) or bool(dirty_channels)
        if self._dirty:
            # A failed shutdown of a running gateway must not masquerade as a
            # completed stop.  A failed startup remains not-started but dirty.
            self._started = was_started
        else:
            self._started = False

        if cancellation is not None:
            if errors:
                raise cancellation from BaseExceptionGroup(
                    "PeerGateway cleanup also failed while cancellation was pending",
                    errors,
                )
            raise cancellation

        if errors:
            raise BaseExceptionGroup("PeerGateway cleanup failed", errors)
