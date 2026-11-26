"""Managed output process operations required by application orchestration."""
from typing import Protocol


from omnibci.application.plugin_management.execution import PluginExecutionReceipt


class OutputProcess(Protocol):
    @property
    def is_alive(self) -> bool: ...

    @property
    def auto_spawn(self) -> bool: ...

    def start(self, *, settings_snapshot: dict) -> None: ...
    def stop(self) -> None: ...

    @property
    def execution_receipt(self) -> PluginExecutionReceipt | None: ...


class StimulationProcess(OutputProcess, Protocol):
    @property
    def current_paradigm(self) -> str | None: ...


class ActuationProcess(OutputProcess, Protocol):
    @property
    def current_effector(self) -> str | None: ...
