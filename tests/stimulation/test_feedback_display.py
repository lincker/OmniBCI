"""
Unit tests for FeedbackDisplay.

No real GPU or font files are required:
  - renderer  : MagicMock with upload_texture → unique TextureHandle
  - PIL font  : ImageFont.load_default() is used automatically when
                arial.ttf is absent (which it is in CI).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.feedback_display import (
    FeedbackDisplay,
)
from omnibci.sdk import TextureHandle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def renderer():
    r = MagicMock()
    r.upload_texture.side_effect = lambda _: TextureHandle()
    return r


@pytest.fixture()
def display():
    return FeedbackDisplay(pos_x=0, pos_y=495)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestFeedbackDisplayLifecycle:
    def test_setup_uploads_blank_texture(self, display, renderer):
        display.setup(renderer)
        renderer.upload_texture.assert_called_once()
        assert display._tex is not None

    def test_teardown_releases_texture(self, display, renderer):
        display.setup(renderer)
        tex = display._tex
        display.teardown(renderer)
        renderer.release_texture.assert_called_once_with(tex)

    def test_teardown_clears_tex_reference(self, display, renderer):
        display.setup(renderer)
        display.teardown(renderer)
        assert display._tex is None

    def test_teardown_without_setup_is_noop(self, display, renderer):
        display.teardown(renderer)   # should not raise


# ---------------------------------------------------------------------------
# Text mutations
# ---------------------------------------------------------------------------

class TestFeedbackDisplayMutations:
    def test_add_text_appends(self, display, renderer):
        display.setup(renderer)
        display.add_text("A")
        display.add_text("B")
        assert display._content == "AB"

    def test_remove_last_removes_char(self, display, renderer):
        display.setup(renderer)
        display.add_text("AB")
        display.remove_last()
        assert display._content == "A"

    def test_remove_last_on_empty_is_noop(self, display, renderer):
        display.setup(renderer)
        display.remove_last()    # should not raise
        assert display._content == ""

    def test_add_text_marks_dirty(self, display, renderer):
        display.setup(renderer)
        display.add_text("X")
        assert display._dirty is True

    def test_remove_last_marks_dirty(self, display, renderer):
        display.setup(renderer)
        display.add_text("X")
        display._dirty = False   # reset
        display.remove_last()
        assert display._dirty is True


# ---------------------------------------------------------------------------
# draw() — texture upload and drawing
# ---------------------------------------------------------------------------

class TestFeedbackDisplayDraw:
    def test_draw_calls_draw_texture(self, display, renderer):
        display.setup(renderer)
        renderer.reset_mock()
        display.draw(renderer)
        renderer.draw_texture.assert_called_once()

    def test_draw_reuploads_when_dirty(self, display, renderer):
        display.setup(renderer)
        display.add_text("Hello")           # marks dirty
        upload_count_before = renderer.upload_texture.call_count
        display.draw(renderer)
        assert renderer.upload_texture.call_count == upload_count_before + 1

    def test_draw_releases_old_texture_before_reuploading(self, display, renderer):
        display.setup(renderer)
        old_tex = display._tex
        display.add_text("X")
        display.draw(renderer)
        renderer.release_texture.assert_called_once_with(old_tex)

    def test_draw_clears_dirty_flag(self, display, renderer):
        display.setup(renderer)
        display.add_text("X")
        display.draw(renderer)
        assert display._dirty is False

    def test_draw_does_not_reupload_when_clean(self, display, renderer):
        display.setup(renderer)
        display.draw(renderer)             # first draw — clean
        renderer.reset_mock()
        display.draw(renderer)             # second draw — still clean
        renderer.upload_texture.assert_not_called()


# ---------------------------------------------------------------------------
# Thread safety (basic smoke test)
# ---------------------------------------------------------------------------

class TestFeedbackDisplayThreadSafety:
    def test_concurrent_add_and_draw_does_not_crash(self, display, renderer):
        import threading

        display.setup(renderer)
        errors = []

        def writer():
            try:
                for ch in "HELLO":
                    display.add_text(ch)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(10):
                    display.draw(renderer)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert errors == [], f"Errors during concurrent access: {errors}"


from unittest.mock import Mock


def prepared_display():
    surface = Mock()
    old, new = object(), object()
    surface.upload_texture.side_effect = [old, new]
    display = FeedbackDisplay(width=20, height=10, font_size=5)
    display.setup(surface)
    display.add_text('A')
    return display, surface, old, new


def test_failed_upload_preserves_previous_texture_and_pending_text():
    display, surface, old, _ = prepared_display()
    surface.upload_texture.side_effect = RuntimeError('upload failed')
    with pytest.raises(RuntimeError, match='upload failed'):
        display.draw(surface)
    assert display._tex is old
    assert display._dirty
    surface.release_texture.assert_not_called()
    surface.draw_texture.assert_not_called()


def test_replacement_upload_precedes_release_of_previous_texture():
    display, surface, old, new = prepared_display()
    order = []
    surface.upload_texture.side_effect = lambda image: order.append('upload') or new
    surface.release_texture.side_effect = lambda handle: order.append('release')
    display.draw(surface)
    assert order == ['upload', 'release']
    surface.release_texture.assert_called_once_with(old)
    assert surface.draw_texture.call_args.args[0] is new


def test_failed_old_release_retains_new_texture_for_teardown():
    display, surface, old, new = prepared_display()
    surface.release_texture.side_effect = RuntimeError('release failed')
    with pytest.raises(RuntimeError, match='release failed'):
        display.draw(surface)
    assert display._tex is new
    surface.release_texture.assert_called_once_with(old)
    surface.release_texture.side_effect = None
    display.teardown(surface)
    assert surface.release_texture.call_args.args[0] is new


def test_text_changed_during_upload_remains_pending():
    display, surface, _, new = prepared_display()
    def upload(image):
        display.add_text('B')
        return new
    surface.upload_texture.side_effect = upload
    display.draw(surface)
    assert display._dirty
    assert display._content == 'AB'
