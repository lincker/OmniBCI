"""Windows parallel-port TTL markers for a Neuroscan amplifier used by CURRY 8."""

from __future__ import annotations

import ctypes
from numbers import Integral
import sys
import time

from omnibci.sdk import PluginContext, PluginParameters, TriggerEmitter


class Curry8Trigger(TriggerEmitter):
    """Write eight stimulus bits; the paradigm controls when to clear them.

    Like Scan45, clear holds the zero baseline for 20 ms before returning.
    A successful send means Out32 returned; it does not verify hardware output.
    """

    plugin_type = "neuroscan_curry8_trigger"

    def __init__(
        self, *, parameters: PluginParameters, context: PluginContext | None = None,
    ):
        self.address = int(parameters.get("parallel_port_address", "0x3100"), 16)
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

        self.clear()

    def send(self, event_id: int) -> bool:
        """Set a code in 0..255; zero is the baseline, not an event marker."""
        if isinstance(event_id, bool) or not isinstance(event_id, Integral) or not 0 <= event_id <= 255:
            raise ValueError("CURRY 8 parallel trigger event_id must be an integer from 0 to 255")
        self._driver.Out32(self.address, int(event_id))
        return True

    def clear(self) -> None:
        """Reset all stimulus bits before the next event, including repeated codes."""
        self._driver.Out32(self.address, 0)
        time.sleep(0.02)
