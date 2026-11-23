"""Expected input failures have declared errors; unrelated defects propagate."""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.configuration.models import PluginProfile
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.domain.channel_mapping import ProcessingInput


@pytest.mark.parametrize("bad", ["selection", "mapping"])
def test_current_configuration_converts_known_input_validation(bad):
    service = ConfigurationService(Mock(get_module=Mock(return_value=None)))
    service._parameters = Mock(return_value=(SimpleNamespace(configuration_version=1), {}))
    binding = {"slot": "processing.decoder", "selection": PluginSelection("test.decoder", "main", "1.0.0").to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}}
    if bad == "selection":
        binding["selection"]["version"] = "latest"
    else:
        binding["processing_input"]["outputs"] = [
            {"name": "C3", "source_channel_index": 0}, {"name": "C3", "source_channel_index": 1},
        ]
    with pytest.raises(PluginProfileValidationError):
        service.propose("processing", [binding], settings={})


def test_stored_selection_failure_uses_declared_configuration_error():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", PluginSelection("test.decoder", "main", "1.0.0"),
                            {}, 1, processing_input=ProcessingInput())
    values = profile.to_dict()
    values["selection"]["version"] = "latest"
    with pytest.raises(PluginProfileValidationError):
        PluginProfile.from_dict(values)


def test_profile_version_input_error_is_declared():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", PluginSelection("test.decoder", "main", "1.0.0"),
                            {}, 1, processing_input=ProcessingInput())
    service = ConfigurationService(Mock(get_profile=Mock(return_value=profile)), profile_versions=Mock())
    with pytest.raises(PluginProfileValidationError):
        service.propose_profile(profile.profile_id, "latest")


def test_unrelated_parameter_implementation_error_is_not_reclassified():
    service = ConfigurationService(Mock(get_module=Mock(return_value=None)))
    service._parameters = Mock(side_effect=ValueError("unexpected implementation failure"))
    binding = {"slot": "processing.decoder", "selection": PluginSelection("test.decoder", "main", "1.0.0").to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}}
    with pytest.raises(ValueError, match="unexpected implementation failure") as raised:
        service.propose("processing", [binding], settings={})
    assert type(raised.value) is ValueError
