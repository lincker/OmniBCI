"""Native refresh samples for the host, without a timer-generated fallback.

CoreVideo supplies output timestamps on macOS. DWM supplies refresh counters
on Windows; GLX_OML_sync_control supplies MSC/UST on Linux/X11. None of these
samples proves that this application's particular image was displayed.
"""
from __future__ import annotations

import ctypes as C
import sys
import time

import glfw

from omnibci.domain.ports.renderer import FrameTiming


def _monitor(window):
    x, y = glfw.get_window_pos(window)
    w, h = glfw.get_window_size(window)
    candidates = []
    for monitor in glfw.get_monitors():
        mx, my = glfw.get_monitor_pos(monitor)
        mode = glfw.get_video_mode(monitor)
        area = max(0, min(x + w, mx + mode.size.width) - max(x, mx)) * max(
            0, min(y + h, my + mode.size.height) - max(y, my))
        candidates.append((area, monitor, mode))
    if not candidates:
        raise RuntimeError("No display is available for the stimulation window")
    _, monitor, mode = max(candidates, key=lambda item: item[0])
    return monitor, (C.cast(monitor, C.c_void_p).value, mode.size.width, mode.size.height, mode.refresh_rate)


class _WindowClock:
    def __init__(self, window, mode, source):
        self._window, self._mode, self._source = window, mode, source

    def now(self):
        return self._source.now()

    def read(self):
        if _monitor(self._window)[1] != self._mode:
            raise RuntimeError("Display clock changed: restart after moving the window or changing its mode")
        return self._source.read()

    def close(self):
        self._source.close()


def create_source(window):
    monitor, mode = _monitor(window)
    if sys.platform == 'darwin':
        source = _CocoaClock(glfw.get_cocoa_monitor(monitor))
    else:
        # DWM's global timing and GLX's screen counter do not identify an
        # arbitrary output. Do not silently use another monitor's clock.
        if len(glfw.get_monitors()) != 1:
            raise RuntimeError("This native display clock requires a single active display on Windows/Linux")
        if sys.platform == 'win32':
            source = _DwmClock()
        elif sys.platform.startswith('linux'):
            source = _GlxClock(window)
        else:
            raise RuntimeError(f"Native display clock is unsupported on {sys.platform}")
    return _WindowClock(window, mode, source)


class _SMPTETime(C.Structure):
    _fields_ = [
        ('subframes', C.c_int16), ('subframeDivisor', C.c_int16),
        ('counter', C.c_uint32), ('type', C.c_uint32), ('flags', C.c_uint32),
        ('hours', C.c_int16), ('minutes', C.c_int16), ('seconds', C.c_int16), ('frames', C.c_int16)]


class _CVTimeStamp(C.Structure):
    _fields_ = [('version', C.c_uint32), ('videoTimeScale', C.c_int32),
                ('videoTime', C.c_int64), ('hostTime', C.c_uint64), ('rateScalar', C.c_double),
                ('videoRefreshPeriod', C.c_int64), ('smpteTime', _SMPTETime),
                ('flags', C.c_uint64), ('reserved', C.c_uint64)]


_CVCallback = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.POINTER(_CVTimeStamp), C.POINTER(_CVTimeStamp),
                        C.c_uint64, C.POINTER(C.c_uint64), C.c_void_p)


class _CocoaClock:
    def __init__(self, display_id):
        self._cv = cv = C.CDLL('/System/Library/Frameworks/CoreVideo.framework/CoreVideo')
        cv.CVDisplayLinkCreateWithCGDisplay.argtypes = [C.c_uint32, C.POINTER(C.c_void_p)]
        cv.CVDisplayLinkSetOutputCallback.argtypes = [C.c_void_p, _CVCallback, C.c_void_p]
        for name in ('CVDisplayLinkStart', 'CVDisplayLinkStop', 'CVDisplayLinkRelease'):
            getattr(cv, name).argtypes = [C.c_void_p]
        cv.CVDisplayLinkRelease.restype = None
        cv.CVGetHostClockFrequency.restype = C.c_double
        cv.CVGetCurrentHostTime.restype = C.c_uint64
        self._frequency = cv.CVGetHostClockFrequency()
        self._host_origin = cv.CVGetCurrentHostTime()
        self._link = C.c_void_p()
        self._started = False
        self._video_origin = None
        self._latest = None
        self._error = None
        # Keep the callback alive until the display link has stopped.
        self._callback = _CVCallback(self._callback_frame)
        try:
            result = cv.CVDisplayLinkCreateWithCGDisplay(display_id, C.byref(self._link))
            if result:
                raise RuntimeError(f"Display clock creation failed: {result}")
            result = cv.CVDisplayLinkSetOutputCallback(self._link, self._callback, None)
            if result:
                raise RuntimeError(f"Display clock callback setup failed: {result}")
            result = cv.CVDisplayLinkStart(self._link)
            if result:
                raise RuntimeError(f"Display clock startup failed: {result}")
            self._started = True
        except BaseException:
            self.close()
            raise

    def now(self):
        return (self._cv.CVGetCurrentHostTime() - self._host_origin) / self._frequency

    def _callback_frame(self, link, now, output, flags_in, flags_out, context):
        self._receive_stamp(output.contents)
        return 0

    def _receive_stamp(self, stamp):
        try:
            # videoTime, hostTime and videoRefreshPeriod must all be valid.
            if stamp.flags & 11 != 11 or stamp.videoTimeScale <= 0 or stamp.videoRefreshPeriod <= 0:
                raise RuntimeError("Display clock supplied an invalid CoreVideo timestamp")
            if self._video_origin is None:
                self._video_origin = stamp.videoTime
            delta = stamp.videoTime - self._video_origin
            if delta < 0 or delta % stamp.videoRefreshPeriod:
                raise RuntimeError("Display clock video timeline changed")
            self._latest = FrameTiming(
                delta // stamp.videoRefreshPeriod,
                (stamp.hostTime - self._host_origin) / self._frequency,
                stamp.videoRefreshPeriod / stamp.videoTimeScale,
            )
        except Exception as error:
            # Exceptions cannot escape a ctypes callback. Propagate on the owner.
            self._error = error

    def read(self):
        if self._error is not None:
            raise self._error
        return self._latest

    def close(self):
        if self._started:
            result = self._cv.CVDisplayLinkStop(self._link)
            if result:
                raise RuntimeError(f"Display clock stop failed: {result}")
            self._started = False
        if self._link:
            self._cv.CVDisplayLinkRelease(self._link)
            self._link = C.c_void_p()


class _UnsignedRatio(C.Structure):
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [('numerator', C.c_uint32), ('denominator', C.c_uint32)]


class _DwmTimingInfo(C.Structure):
    # dwmapi.h uses pshpack1.h; the complete layout is required for cbSize.
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [('cbSize', C.c_uint32), ('rateRefresh', _UnsignedRatio),
                ('qpcRefreshPeriod', C.c_uint64), ('rateCompose', _UnsignedRatio)] + [
        (name, C.c_uint32 if name.startswith('cDX') or name == 'cFramesOutstanding' else C.c_uint64)
        for name in (
            'qpcVBlank', 'cRefresh', 'cDXRefresh', 'qpcCompose', 'cFrame', 'cDXPresent',
            'cRefreshFrame', 'cFrameSubmitted', 'cDXPresentSubmitted', 'cFrameConfirmed',
            'cDXPresentConfirmed', 'cRefreshConfirmed', 'cDXRefreshConfirmed', 'cFramesLate',
            'cFramesOutstanding', 'cFrameDisplayed', 'qpcFrameDisplayed', 'cRefreshFrameDisplayed',
            'cFrameComplete', 'qpcFrameComplete', 'cFramePending', 'qpcFramePending',
            'cFramesDisplayed', 'cFramesComplete', 'cFramesPending', 'cFramesAvailable',
            'cFramesDropped', 'cFramesMissed', 'cRefreshNextDisplayed', 'cRefreshNextPresented',
            'cRefreshesDisplayed', 'cRefreshesPresented', 'cRefreshStarted', 'cPixelsReceived',
            'cPixelsDrawn', 'cBuffersEmpty')]


class _DwmClock:
    def __init__(self):
        self._kernel = C.WinDLL('kernel32')
        self._dwm = C.WinDLL('dwmapi')
        self._kernel.QueryPerformanceCounter.argtypes = [C.POINTER(C.c_int64)]
        self._kernel.QueryPerformanceFrequency.argtypes = [C.POINTER(C.c_int64)]
        self._dwm.DwmGetCompositionTimingInfo.argtypes = [C.c_void_p, C.POINTER(_DwmTimingInfo)]
        self._dwm.DwmGetCompositionTimingInfo.restype = C.c_int32
        frequency = C.c_int64()
        if not self._kernel.QueryPerformanceFrequency(C.byref(frequency)) or frequency.value <= 0:
            raise RuntimeError("Display clock QPC frequency is unavailable")
        self._frequency = frequency.value
        self._origin = self._counter()
        self._refresh_origin = None

    def _counter(self):
        counter = C.c_int64()
        if not self._kernel.QueryPerformanceCounter(C.byref(counter)):
            raise RuntimeError("Display clock QPC query failed")
        return counter.value

    def now(self):
        return (self._counter() - self._origin) / self._frequency

    def read(self):
        info = _DwmTimingInfo(cbSize=C.sizeof(_DwmTimingInfo))
        result = self._dwm.DwmGetCompositionTimingInfo(None, C.byref(info))
        if result:
            raise RuntimeError(f"DWM display clock query failed: {result}")
        if not info.qpcVBlank or not info.qpcRefreshPeriod:
            return None
        if self._refresh_origin is None:
            self._refresh_origin = info.cRefresh
        target = (info.qpcVBlank + info.qpcRefreshPeriod - self._origin) / self._frequency
        if target < 0:
            return None
        return FrameTiming(info.cRefresh - self._refresh_origin, target,
                           info.qpcRefreshPeriod / self._frequency)

    def close(self):
        pass


class _GlxClock:
    def __init__(self, window=None):
        if not glfw.extension_supported('GLX_OML_sync_control'):
            raise RuntimeError("Display timing requires GLX_OML_sync_control on Linux/X11")
        self._display = glfw.get_x11_display()
        self._drawable = glfw.get_glx_window(window)
        signature = (C.c_void_p, C.c_ulong, C.POINTER(C.c_int64), C.POINTER(C.c_int64), C.POINTER(C.c_int64))
        rate_signature = (C.c_void_p, C.c_ulong, C.POINTER(C.c_int32), C.POINTER(C.c_int32))
        sync = glfw.get_proc_address('glXGetSyncValuesOML')
        rate = glfw.get_proc_address('glXGetMscRateOML')
        if not sync or not rate:
            raise RuntimeError("GLX_OML_sync_control entry points are unavailable")
        self._sync = C.CFUNCTYPE(C.c_int, *signature)(sync)
        self._rate = C.CFUNCTYPE(C.c_int, *rate_signature)(rate)
        self._origin = time.monotonic()
        self._refresh_origin = None

    def now(self):
        return time.monotonic() - self._origin

    def read(self):
        ust, msc, sbc = C.c_int64(), C.c_int64(), C.c_int64()
        numerator, denominator = C.c_int32(), C.c_int32()
        if (not self._sync(self._display, self._drawable, C.byref(ust), C.byref(msc), C.byref(sbc))
                or not self._rate(self._display, self._drawable, C.byref(numerator), C.byref(denominator))):
            raise RuntimeError("GLX display clock query failed")
        if numerator.value <= 0 or denominator.value <= 0:
            raise RuntimeError("GLX display refresh period is invalid")
        if msc.value <= 0:
            return None
        period = denominator.value / numerator.value
        last_refresh = ust.value / 1_000_000
        # Accept the monotonic-microsecond UST supplied by current X11 drivers.
        # An unknown UST timebase must not become a fabricated display deadline.
        if not -period <= time.monotonic() - last_refresh <= 1:
            raise RuntimeError("GLX display clock does not provide a current monotonic UST")
        if self._refresh_origin is None:
            self._refresh_origin = msc.value
        target = last_refresh + period - self._origin
        if target < 0:
            return None
        return FrameTiming(msc.value - self._refresh_origin, target, period)

    def close(self):
        pass
