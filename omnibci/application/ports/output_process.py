"""Managed output process operations required by application orchestration."""
from typing import Protocol


class OutputProcess(Protocol):
    @property
    def is_alive(self) -> bool: ...

    @property
    def auto_spawn(self) -> bool: ...

    def start(self, *, settings_snapshot: dict) -> None: ...
    def stop(self) -> None: ...


class StimulationProcess(OutputProcess, Protocol):
    @property
    def current_paradigm(self) -> str | None: ...
