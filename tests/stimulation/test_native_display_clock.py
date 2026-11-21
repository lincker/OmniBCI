"""Native timestamp conversion and unsupported-clock failures need no display."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def cocoa_source():
    from omnibci.infrastructure.stimulation.native_display_clock import _CocoaClock
    source = _CocoaClock.__new__(_CocoaClock)
    source._host_origin = 1000
    source._frequency = 1000
    source._video_origin = None
    source._latest = None
    source._error = None
    return source


def stamp(video_time, host_time):
    return SimpleNamespace(flags=11, videoTime=video_time, videoTimeScale=60000,
                           videoRefreshPeriod=1000, hostTime=host_time)


def test_cocoa_uses_native_video_time_gaps_not_callback_count():
    source = cocoa_source()
    source._receive_stamp(stamp(100000, 1010))
    assert source._latest.refresh_index == 0
    source._receive_stamp(stamp(104000, 1077))
    assert source._latest.refresh_index == 4
    assert source._latest.target_time == pytest.approx(.077)
    assert source._latest.refresh_period == pytest.approx(1 / 60)


def test_invalid_cocoa_timestamp_is_reported_not_ignored():
    source = cocoa_source()
    invalid = stamp(100000, 1010)
    invalid.flags = 0
    source._receive_stamp(invalid)
    assert isinstance(source._error, RuntimeError)
    assert source._latest is None


def test_linux_requires_native_sync_extension(monkeypatch):
    from omnibci.infrastructure.stimulation import native_display_clock as module
    monkeypatch.setattr(module.glfw, 'extension_supported', Mock(return_value=False))
    with pytest.raises(RuntimeError, match='GLX_OML_sync_control'):
        module._GlxClock()


def test_glx_native_refresh_counter_and_microsecond_timestamp(monkeypatch):
    from omnibci.infrastructure.stimulation import native_display_clock as module
    source = module._GlxClock.__new__(module._GlxClock)
    source._display, source._drawable = 1, 2
    source._origin, source._refresh_origin = 100.0, None
    values = [100_000_000, 500]

    def sync(display, drawable, ust, msc, sbc):
        ust._obj.value, msc._obj.value, sbc._obj.value = *values, 0
        return 1

    def rate(display, drawable, numerator, denominator):
        numerator._obj.value, denominator._obj.value = 60, 1
        return 1

    source._sync, source._rate = sync, rate
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.005)
    assert source.read().refresh_index == 0
    values[:] = [100_050_000, 503]
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.055)
    frame = source.read()
    assert frame.refresh_index == 3
    assert frame.target_time == pytest.approx(.05 + 1 / 60)
    values[0] = 1_700_000_000_000_000
    with pytest.raises(RuntimeError, match='monotonic UST'):
        source.read()


def test_dwm_layout_and_qpc_conversion_preserve_refresh_gaps():
    import ctypes as C
    from omnibci.infrastructure.stimulation import native_display_clock as module
    assert C.sizeof(module._DwmTimingInfo) == 292
    source = module._DwmClock.__new__(module._DwmClock)
    source._origin, source._frequency, source._refresh_origin = 6_000_000, 60_000, None
    values = [6_000_000, 100]

    def query(window, output):
        assert window is None
        info = output._obj
        assert info.cbSize == 292
        info.qpcVBlank, info.cRefresh = values
        info.qpcRefreshPeriod = 1000
        return 0

    source._dwm = SimpleNamespace(DwmGetCompositionTimingInfo=query)
    assert source.read().refresh_index == 0
    values[:] = [6_004_000, 104]
    frame = source.read()
    assert frame.refresh_index == 4
    assert frame.target_time == pytest.approx(5 / 60)
    assert frame.refresh_period == pytest.approx(1 / 60)
