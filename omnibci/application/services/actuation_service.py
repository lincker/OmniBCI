"""Own Actuation's process, output settlement, and enabled state."""
import asyncio
from copy import deepcopy

from omnibci.shared.events import EventType


class ActuationAppService:
    def __init__(self, engine, process_manager=None, commands=None):
        self.engine = engine
        self.process_manager = process_manager
        self.commands = commands
        self.active = False
        self._snapshot = None

    @property
    def process_alive(self):
        return bool(self.process_manager is not None and self.process_manager.is_alive)

    @property
    def owned_snapshot(self):
        return deepcopy(self._snapshot)

    def reconnect(self, snapshot):
        """Keep unresolved output ownership while accepting a new listener identity."""
        if self._snapshot is not None:
            self._snapshot = deepcopy(snapshot)

    async def start(self, snapshot):
        await self.engine.ensure_output_ready("actuation", snapshot)
        managed = snapshot.get("ACTUATION.auto_spawn", True)
        if managed:
            if self.process_manager is None:
                raise RuntimeError("No managed Actuation process is available")
        elif not self.engine.is_peer_connected("actuation"):
            raise RuntimeError("Connect the external Actuation node before starting it")
        self._snapshot = deepcopy(snapshot)
        try:
            if managed:
                await asyncio.to_thread(self.process_manager.start, settings_snapshot=deepcopy(snapshot))
            else:
                result = await asyncio.to_thread(self.commands.request, EventType.ResumeActuation)
                self._check_receipt(result.data, snapshot, "resumed")
            await self.engine.set_actuation_active(True)
            self.active = True
        except BaseException as exc:
            try:
                await self.stop()
            except BaseException as cleanup:
                self.engine.admission.quarantine(
                    f"Actuation startup failed: {exc}; cleanup failed: {cleanup}", "actuation")
            raise

    async def stop(self, *, settled=False):
        snapshot = self._snapshot or self.engine.applied_snapshot or {}
        managed = snapshot.get("ACTUATION.auto_spawn", True)
        owned = self.active or self.process_alive or not managed and (
            self._snapshot is not None or self.engine.is_peer_connected("actuation")
            or self.engine.admission.requires_recovery("actuation"))
        if owned and not managed:
            self._snapshot = deepcopy(snapshot)
        failure = None
        try:
            if self.engine.is_engine_ready:
                await self.engine.set_actuation_active(False)
                if not settled and owned:
                    if self.engine.is_peer_connected("actuation"):
                        result = await asyncio.to_thread(self.commands.request, EventType.SettleActuation)
                        self._check_receipt(result.data, snapshot,
                                            "paused", "execution_complete", "outputs_settled")
                    elif not managed:
                        raise RuntimeError("External Actuation disconnected before confirming settlement")
            elif owned and not managed and not settled:
                raise RuntimeError("External Actuation settlement requires the engine connection")
        except Exception as exc:
            failure = exc
        process_stopped = False
        if self.process_manager is not None:
            try:
                await asyncio.to_thread(self.process_manager.stop)
                process_stopped = True
            except Exception as exc:
                failure = (RuntimeError(f"{failure}; Actuation process cleanup failed: {exc}")
                           if failure is not None else exc)
        if failure is None or managed and process_stopped:
            self.active = False
            self._snapshot = None
        if failure is not None:
            raise failure

    @staticmethod
    def _check_receipt(data, snapshot, *flags):
        if (not isinstance(data, dict) or data.get("generation") != snapshot.get("CONFIG.generation")
                or data.get("system_revision") != snapshot.get("CONFIG.system_revision")
                or data.get("module_revision") != snapshot.get("CONFIG.module_revisions", {}).get("actuation")
                or any(data.get(flag) is not True for flag in flags)):
            raise RuntimeError("Actuation did not confirm the requested boundary")
