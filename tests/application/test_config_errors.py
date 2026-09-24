"""Shared configuration errors preserve catches and historical pickle imports."""
import pickle

import pytest

from omnibci.application import config_errors, configuration


@pytest.mark.parametrize("name", ["ConfigProfileValidationError", "ConfigProfileConflictError"])
def test_legacy_error_export_preserves_type_identity(name):
    assert getattr(configuration, name) is getattr(config_errors, name)


@pytest.mark.parametrize("name", ["ConfigProfileValidationError", "ConfigProfileConflictError"])
def test_historical_error_pickle_still_resolves(name):
    payload = f"comnibci.application.configuration\n{name}\n(Vmessage\ntR.".encode("ascii")

    restored = pickle.loads(payload)

    assert type(restored) is getattr(config_errors, name)
    assert str(restored) == "message"
    assert type(pickle.loads(pickle.dumps(restored))) is type(restored)
