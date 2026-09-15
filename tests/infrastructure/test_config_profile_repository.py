"""Local profile persistence uses isolated user directories."""
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.configuration import (
    ConfigProfile, ConfigProfileNotFoundError, ConfigProfileValidationError,
)
from omnibci.infrastructure.repositories.config_profile_repository import (
    TomlConfigProfileRepository, config_root,
)


@pytest.fixture()
def repository(tmp_path):
    return TomlConfigProfileRepository(tmp_path / "config")


@pytest.fixture()
def profile():
    return ConfigProfile(
        profile_id=str(uuid4()), name="Scan45 Lab A", module="acquisition",
        plugin_type="neuroscan_scan45_reader",
        parameters={"DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
                    "sample_rate": 1000, "channel_number": 10},
    )


def test_reads_do_not_create_user_directory(repository):
    assert repository.list("acquisition") == []
    assert repository.get_active("acquisition") is None
    assert not repository.root.exists()


def test_profile_and_selection_survive_restart(repository, profile):
    repository.save(profile)
    repository.set_active("acquisition", profile.profile_id)
    fresh = TomlConfigProfileRepository(repository.root)
    assert fresh.get("acquisition", profile.profile_id) == profile
    assert fresh.get_active("acquisition") == profile.profile_id
    assert fresh.list("acquisition", "file_reader") == []
    assert fresh.list("acquisition", profile.plugin_type) == [profile]


def test_rename_preserves_identity_and_file(repository, profile):
    repository.save(profile)
    files = list(repository.root.rglob("*.toml"))
    renamed = replace(profile, name="Scan45 Lab B")
    repository.save(renamed)
    assert list(repository.root.rglob("*.toml")) == files
    assert repository.get("acquisition", profile.profile_id) == renamed


def test_returned_parameters_are_independent(repository, profile):
    repository.save(profile)
    repository.get("acquisition", profile.profile_id).parameters["sample_rate"] = 250
    assert repository.get("acquisition", profile.profile_id).parameters["sample_rate"] == 1000


def test_deactivate_and_delete(repository, profile):
    repository.save(profile)
    repository.set_active("acquisition", profile.profile_id)
    repository.set_active("acquisition", None)
    assert repository.get_active("acquisition") is None
    repository.delete("acquisition", profile.profile_id)
    with pytest.raises(ConfigProfileNotFoundError):
        repository.get("acquisition", profile.profile_id)


def test_missing_selection_is_rejected(repository):
    with pytest.raises(ConfigProfileNotFoundError):
        repository.set_active("acquisition", str(uuid4()))
    assert not repository.root.exists()


@pytest.mark.parametrize("profile_id", ["../outside", "", "not-a-uuid"])
def test_profile_ids_cannot_be_paths(repository, profile_id):
    with pytest.raises(ConfigProfileValidationError):
        repository.get("acquisition", profile_id)


@pytest.mark.parametrize("module", ["../outside", "algorithm", ""])
def test_unsupported_modules_rejected(repository, module):
    with pytest.raises(ConfigProfileValidationError):
        repository.list(module)


def test_failed_replace_preserves_original_and_removes_temporary_file(
    repository, profile, monkeypatch,
):
    repository.save(profile)
    original = list(repository.root.rglob("*.toml"))[0].read_bytes()
    monkeypatch.setattr(
        "omnibci.infrastructure.repositories.config_profile_repository.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk error")),
    )
    with pytest.raises(OSError, match="disk error"):
        repository.save(replace(profile, name="Changed"))
    assert list(repository.root.rglob("*.toml"))[0].read_bytes() == original
    assert len([p for p in repository.root.rglob("*") if p.is_file()]) == 1


def test_unknown_schema_is_reported(repository, profile):
    repository.save(profile)
    path = next(repository.root.rglob("*.toml"))
    path.write_text(path.read_text().replace("schema_version = 1", "schema_version = 2"))
    with pytest.raises(ConfigProfileValidationError, match="schema_version"):
        repository.get("acquisition", profile.profile_id)


def test_filename_and_payload_identity_must_match(repository, profile):
    repository.save(profile)
    path = next(repository.root.rglob("*.toml"))
    path.write_text(path.read_text().replace(profile.profile_id, str(uuid4())))
    with pytest.raises(ConfigProfileValidationError):
        repository.get("acquisition", profile.profile_id)


def test_directory_override_is_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path))
    assert config_root() == tmp_path
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", "relative/config")
    with pytest.raises(ConfigProfileValidationError):
        config_root()


def test_malformed_toml_is_reported_without_overwriting(repository, profile):
    repository.save(profile)
    path = next(repository.root.rglob("*.toml"))
    path.write_text('name = "unterminated')
    with pytest.raises(ConfigProfileValidationError):
        repository.get("acquisition", profile.profile_id)
    assert path.read_text() == 'name = "unterminated'


def test_failed_selection_write_preserves_previous_choice(repository, profile, monkeypatch):
    repository.save(profile)
    repository.set_active("acquisition", profile.profile_id)
    monkeypatch.setattr("omnibci.infrastructure.repositories.config_profile_repository.os.replace",
                        lambda *args: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError):
        repository.set_active("acquisition", None)
    assert repository.get_active("acquisition") == profile.profile_id


def test_symlink_cannot_redirect_writes_outside_config_root(repository, profile, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repository.root.mkdir()
    try:
        (repository.root / "profiles").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    with pytest.raises(ConfigProfileValidationError, match="escapes"):
        repository.save(profile)
    assert list(outside.iterdir()) == []
