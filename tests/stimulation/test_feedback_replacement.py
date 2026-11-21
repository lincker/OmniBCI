"""Feedback texture replacement preserves the last usable frame on upload failure."""
from unittest.mock import Mock

import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.feedback_display import FeedbackDisplay


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
