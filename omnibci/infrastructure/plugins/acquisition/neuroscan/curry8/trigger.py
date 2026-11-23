"""Windows parallel-port TTL markers for a Neuroscan amplifier used by CURRY 8."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
import ctypes
from numbers import Integral
import sys
import time

from omnibci.sdk import TriggerEmitter


class Curry8Trigger(TriggerEmitter):
    """Write eight stimulus bits; the paradigm controls when to clear them.

    Like Scan45, clear holds the zero baseline for 20 ms before returning.
    A successful send means Out32 returned; it does not verify hardware output.
    """


    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
    ) -> None:
        self.address = int(parameters.get("parallel_port_address", "0x3100"), 16)
        self._driver = None
        self._ready = False

    def setup(self) -> None:
        if self._driver is not None:
            raise RuntimeError("CURRY 8 trigger is already initialized")
        if sys.platform != "win32":
            raise RuntimeError("CURRY 8 Parallel Trigger requires Windows and an InpOut driver")

        dll_name = "inpoutx64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "inpout32.dll"
        try:
            self._driver = ctypes.WinDLL(dll_name)
            self._driver.Out32.argtypes = [ctypes.c_short, ctypes.c_short]
            self._driver.Out32.restype = None
            self._driver.IsInpOutDriverOpen.argtypes = []
            self._driver.IsInpOutDriverOpen.restype = ctypes.c_int
        except (OSError, AttributeError) as exc:
            raise RuntimeError(
                f"Cannot load {dll_name}. Install the matching InpOut DLL and kernel "
                "driver on the Windows stimulation host."
            ) from exc
        if not self._driver.IsInpOutDriverOpen():
            raise RuntimeError(
                "InpOut kernel driver is not open. Install the driver with administrator "
                "permissions on the Windows stimulation host."
            )

        self._ready = True

    def send(self, event_id: int) -> None:
        """Set a code in 0..255; zero is the baseline, not an event marker."""
        if isinstance(event_id, bool) or not isinstance(event_id, Integral) or not 0 <= event_id <= 255:
            raise ValueError("CURRY 8 parallel trigger event_id must be an integer from 0 to 255")
        if not self._ready:
            raise RuntimeError("CURRY 8 trigger requires successful setup")
        self._driver.Out32(self.address, int(event_id))

    def clear(self) -> None:
        """Reset all stimulus bits before the next event, including repeated codes."""
        if not self._ready:
            raise RuntimeError("CURRY 8 trigger requires successful setup")
        self._driver.Out32(self.address, 0)
        time.sleep(0.02)

    def teardown(self) -> None:
        # InpOut exposes no per-instance close. The host clears before release.
        self._ready = False
        self._driver = None
