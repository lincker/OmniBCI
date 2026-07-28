"""
Actuator — device lifecycle for the actuation subsystem.

Mirrors the plugin-loading part of stimulation's Stimulator: loads the
Effector plugin named by ``ACTUATION.effector`` from the effectors plugin
directory and drives it through the domain port (setup / play / teardown).
No render loop — the actuator is command-driven.
"""

import logging
from typing import Any, Mapping, Optional

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog

logger = logging.getLogger(__name__)


class Actuator:
    def __init__(
        self,
        *,
        plugin_catalog: PluginCatalog | None = None,
        plugin_loader: type[PluginLoader] = PluginLoader,
    ) -> None:
        self.effector: Optional[Effector] = None
        self._plugin_catalog = (
            plugin_catalog if plugin_catalog is not None else PluginCatalog()
        )
        self._plugin_loader = plugin_loader

    # ── Manifest-resolved plugin loading ──────────────────────────────────

    def _load_effector(self) -> Optional[Effector]:
        from omnibci.conf import settings

        plugin_type = settings.get('ACTUATION.effector')
        if not plugin_type:
            return None
        descriptor = self._plugin_catalog.resolve("effector", plugin_type)
        return self._plugin_loader.instantiate(descriptor, Effector)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def setup(self) -> None:
        """Load the configured Effector plugin and acquire the device.

        Raises RuntimeError when no effector is configured/loadable — the
        subsystem cannot serve without one (same failure mode as stimulation
        failing to load a paradigm).
        """
        effector = self._load_effector()
        if effector is None:
            raise RuntimeError(
                "No Effector plugin loaded (check ACTUATION.effector and the "
                "effectors plugin directory)"
            )
        self.effector = effector
        try:
            effector.setup()
        except BaseException:
            # setup() may fail after opening a serial port, claiming a device,
            # or moving hardware into an intermediate state.  Always give the
            # plugin a best-effort chance to roll that work back, while keeping
            # the original setup exception as the one callers observe.
            try:
                effector.teardown()
            except BaseException as teardown_exc:
                logger.error(
                    "Effector teardown after failed setup also failed: %s",
                    teardown_exc,
                    exc_info=True,
                )
            finally:
                self.effector = None
            raise
        logger.info("Actuator ready (effector=%s)", type(effector).__name__)

    def execute(self, command: Mapping[str, Any]) -> None:
        """Carry out one ActuationCommand payload via the effector.

        Exceptions propagate — the application loop translates them into an
        ActuationFault without killing the process.
        """
        if self.effector is None:
            raise RuntimeError("Actuator.execute called before setup()")
        self.effector.play(command)

    def teardown(self) -> None:
        """Release the device; safe to call when setup never completed."""
        if self.effector is not None:
            try:
                self.effector.teardown()
            except Exception as exc:
                logger.error("Effector teardown failed: %s", exc)
            self.effector = None
