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
    A successful send confirms the driver call, not amplifier receipt.
    """

    plugin_type = "neuroscan_curry8_trigger"

    def __init__(
        self, *, parameters: PluginParameters, context: PluginContext | None = None,
    ):
        address = parameters.get("parallel_port_address", "0x3100")
        try:
            self.address = int(address, 16) if isinstance(address, str) else 0
        except ValueError:
            self.address = 0
        # The data, control and ECP registers must fit the 16-bit I/O space.
        if not 0 < self.address <= 0xFFFF - 0x402:
            raise ValueError(
                "parallel_port_address must be a hexadecimal I/O base address "
                "between 0x1 and 0xFBFD"
            )
        if sys.platform != "win32":
            raise RuntimeError("CURRY 8 Parallel Trigger requires Windows and an InpOut driver")

        dll_name = "inpoutx64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "inpout32.dll"
        try:
            self._driver = ctypes.WinDLL(dll_name)
            self._driver.Inp32.argtypes = [ctypes.c_short]
            self._driver.Inp32.restype = ctypes.c_short
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

        # Use the same byte/output mode as the Scan45 parallel-port adapter.
        ecp = self._driver.Inp32(self.address + 0x402)
        self._driver.Out32(self.address + 0x402, (ecp & 0x1F) | 0x20)
        control = self._driver.Inp32(self.address + 2)
        self._driver.Out32(self.address + 2, control & 0xDF)
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
