"""Static native frames exercise clock-driven pacing and deliberate owner stalls."""
import asyncio
import importlib
import time
from pathlib import Path
from unittest.mock import Mock

import glfw
import numpy as np
import pytest

from omnibci.sdk import Color
from omnibci.infrastructure.stimulation.render_loop import RenderLoop


pytestmark = pytest.mark.gui


@pytest.mark.parametrize('adapter', ['pyopengl', 'moderngl'])
def test_native_stall_discards_old_image_and_preserves_refresh_gap(adapter):
    if not glfw.init():
        pytest.skip('A display supporting OpenGL is required')
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    module = importlib.import_module('omnibci.infrastructure.stimulation.adapters.' + adapter + '_renderer')
    renderer = module.PyOpenGLRenderer() if adapter == 'pyopengl' else module.ModernGLRenderer()
    frames, submissions = [], []
    end_frame = renderer.end_frame

    def record_submission(frame, *, submit):
        accepted = end_frame(frame, submit=submit)
        submissions.append((frame.refresh_index, accepted))
        return accepted

    class StaticParadigm:
        def render_frame(self, frame):
            frames.append(frame)
            renderer.clear(Color(.25, .25, .25))
            if len(frames) == 5:
                time.sleep(frame.refresh_period * 3.5)
            if len(frames) == 20:
                loop.request_stop()
            return True

    try:
        renderer.initialize(64, 64, 'OmniBCI static frame timing test')
        renderer.end_frame = record_submission
        loop = RenderLoop(renderer, StaticParadigm())
        asyncio.run(asyncio.wait_for(loop.run_async(), 5))
        assert len(frames) == 20
        assert frames[5].refresh_index - frames[4].refresh_index >= 3
        assert submissions[4] == (frames[4].refresh_index, False)
        assert all(b.refresh_index > a.refresh_index for a, b in zip(frames, frames[1:]))
        assert any(accepted for _, accepted in submissions[5:])
    finally:
        renderer.shutdown()


@pytest.mark.parametrize('adapter', ['pyopengl', 'moderngl'])
def test_classic40_native_stall_keeps_240_slot_timeline(adapter, monkeypatch, record_property):
    from omnibci.infrastructure.plugins.paradigms.classic40SSVEP import ssvep
    if not glfw.init():
        pytest.skip('A display supporting OpenGL is required')
    glfw.window_hint(glfw.VISIBLE, glfw.TRUE)
    module = importlib.import_module('omnibci.infrastructure.stimulation.adapters.' + adapter + '_renderer')
    renderer = module.PyOpenGLRenderer() if adapter == 'pyopengl' else module.ModernGLRenderer()
    paradigm = ssvep.Classic40SSVEP_2(parameters={}, resource_root=Path('.'))
    # The real texture dimensions/count exercise preparation and switching,
    # while constant grey avoids running a flashing stimulus in a GUI test.
    grey = np.full((990, 1920, 1), 64, dtype=np.uint8)
    pack = Mock()
    pack.__enter__ = Mock(return_value=pack)
    pack.__exit__ = Mock(return_value=False)
    pack.idle_frame = grey
    pack.iter_stimulation_frames.return_value = iter([grey] * 240)
    pack.iter_cue_frames.return_value = iter([grey] * 40)
    pack.iter_result_frames.return_value = iter([grey] * 40)
    monkeypatch.setattr(ssvep, '_frame_pack_path', lambda root: Path('static-test.ofp'))
    monkeypatch.setattr(ssvep, 'FramePack', lambda path: pack)
    frames, images, submissions = [], [], []
    render_frame, draw_texture, end_frame = paradigm.render_frame, renderer.draw_texture, renderer.end_frame
    stalled = False

    def draw(handle, rect):
        if handle in indices:
            images.append((frames[-1].refresh_index, indices[handle]))
        draw_texture(handle, rect)

    def render(frame):
        nonlocal stalled
        frames.append(frame)
        ready = render_frame(frame)
        if not stalled and frame.refresh_index - frames[0].refresh_index >= 60:
            stalled = True
            time.sleep(frame.refresh_period * 3.5)
        if not paradigm.is_presenting():
            loop.request_stop()
        return ready

    def submit(frame, *, submit):
        accepted = end_frame(frame, submit=submit)
        submissions.append((frame.refresh_index, accepted))
        return accepted

    try:
        renderer.initialize(1920, 1080, 'OmniBCI static 240-slot timing test')
        paradigm.setup(renderer=renderer, trigger=Mock())
        indices = {handle: i for i, handle in enumerate(paradigm._stim_textures)}
        paradigm.render_frame = render
        renderer.draw_texture = draw
        renderer.end_frame = submit
        paradigm.present_trial({'event_id': 0})
        loop = RenderLoop(renderer, paradigm)
        asyncio.run(asyncio.wait_for(loop.run_async(), 8))
        origin = frames[0].refresh_index
        assert stalled
        assert all(image == refresh - origin for refresh, image in images)
        assert all(image < 240 for _, image in images)
        assert frames[-2].refresh_index - origin < 240 <= frames[-1].refresh_index - origin
        assert len(images) < 240
        assert any(not accepted for _, accepted in submissions)
        assert not paradigm.is_presenting()
        record_property('target_elapsed_seconds', frames[-1].target_time - frames[0].target_time)
        record_property('image_draws', len(images))
        record_property('discarded_submissions', sum(not accepted for _, accepted in submissions))
        record_property('skipped_refreshes', renderer._clock.skipped_refreshes)
    finally:
        try:
            paradigm.teardown()
        finally:
            renderer.shutdown()
