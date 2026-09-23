"""Composition selects accepted configuration instead of mutable saved profiles."""
from unittest.mock import Mock

import pytest

from omnibci.bootstrap import _RuntimeSettings, build_container


@pytest.mark.asyncio
async def test_composition_migrates_once_and_saved_edits_do_not_change_startup():
    container = await build_container()
    profiles = container.plugin_profile_service
    accepted = profiles.repository.get_activation()
    assert accepted is not None
    before = container.engine_service._snapshot_provider()
    source = next(binding for binding in accepted.bindings if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    parameters = {**profile.parameters, "sample_rate": 250}
    profiles.update(profile.profile_id, name="Saved edit", parameters=parameters,
                    expected_revision=profile.revision)

    assert container.engine_service._snapshot_provider() == before
    assert before["CONFIG.revision"] == accepted.revision
    assert len(before["PLUGINS.bindings"]) >= 4


def test_status_reads_applied_values_while_engine_running():
    engine = Mock(is_engine_ready=True, applied_snapshot={"ACQUISITION.sample_rate": 250})
    profiles = Mock()
    profiles.resolve_settings.return_value = {"ACQUISITION.sample_rate": 1000}
    view = _RuntimeSettings(engine, profiles)
    assert view.get("ACQUISITION.sample_rate") == 250
    profiles.resolve_settings.assert_not_called()
    engine.is_engine_ready = False
    assert view.get("ACQUISITION.sample_rate") == 1000
