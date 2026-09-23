"""Module profiles are complete, validated overlays with stable identities."""
from dataclasses import replace

import pytest

from omnibci.application.configuration import (
    ConfigProfileConflictError, ConfigProfileValidationError,
)
from omnibci.application.services.config_service import ConfigService
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.repositories.config_profile_repository import TomlConfigProfileRepository


@pytest.fixture
def module_service(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
                    'channel_number = 10\ndata_file_path = "recordings/demo.cnt"\n'
                    'file_reader_chunk_size = 100\nparallel_port_address = "0x3100"\n'
                    '[ALGORITHM]\nwindow_duration = 3.0\n', encoding="utf-8")
    return ConfigService(OmniBCISettings(path),
                         repository=TomlConfigProfileRepository(tmp_path / "user"))


def create_scan(svc, name="Lab A", **overrides):
    return svc.create_module_profile("acquisition", "neuroscan_scan45_reader", name, {
        "DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
        "sample_rate": 500, "channel_number": 10, **overrides,
    })


def test_complete_snapshot_overlays_legacy_without_writing_defaults(module_service):
    svc = module_service
    svc.apply({"ACQUISITION.sample_rate": 250, "ALGORITHM.window_duration": 4.0}, scope="legacy")
    svc.activate("legacy")
    before = svc._settings._config_path.read_bytes()
    profile = create_scan(svc)
    svc.select_module_profile("acquisition", profile.profile_id)
    snapshot = svc.resolve_snapshot()
    assert snapshot["ACQUISITION.sample_rate"] == 500
    assert snapshot["ACQUISITION.signal_source"] == "neuroscan_scan45_reader"
    assert snapshot["ALGORITHM.window_duration"] == 4.0
    assert snapshot["ACQUISITION.parallel_port_address"] == "0x3100"
    assert svc._settings._config_path.read_bytes() == before
    assert svc._settings.read_profile("legacy")["ACQUISITION.sample_rate"] == 250
    entries, _ = svc.list_entries()
    assert next(e for e in entries if e["key"] == "ACQUISITION.sample_rate")["effective_value"] == 500
    svc.select_module_profile("acquisition", None)
    assert svc.resolve_snapshot()["ACQUISITION.sample_rate"] == 250


def test_selection_and_saved_parameters_survive_service_restart(module_service):
    svc = module_service
    p = create_scan(svc)
    svc.select_module_profile("acquisition", p.profile_id)
    fresh = ConfigService(OmniBCISettings(svc._settings._config_path),
                          repository=TomlConfigProfileRepository(svc._repository.root))
    assert fresh.module_detail("acquisition")["active_profile_id"] == p.profile_id
    assert fresh.module_detail("acquisition")["effective_parameters"] == p.parameters


def test_rename_and_save_as_have_distinct_identity_semantics(module_service):
    svc = module_service
    p = create_scan(svc)
    renamed = svc.update_module_profile("acquisition", p.profile_id, " Renamed ", p.parameters)
    copy = svc.create_module_profile("acquisition", p.plugin_type, "Copy", p.parameters)
    assert renamed.profile_id == p.profile_id and renamed.name == "Renamed"
    assert copy.profile_id != p.profile_id and copy.parameters == p.parameters


def test_names_are_unique_within_a_plugin(module_service):
    create_scan(module_service)
    with pytest.raises(ConfigProfileConflictError):
        create_scan(module_service, " lab a ")


@pytest.mark.parametrize("updates", [
    {"channel_number": 1}, {"channel_number": True}, {"sample_rate": 0}, {"sample_rate": True},
    {"DATA_PROVIDER_PORT": 65536}, {"DATA_PROVIDER_PORT": "4000"},
    {"DATA_PROVIDER_IP": ""}, {"signal_source": "file_reader"}, {"speed": 2},
])
def test_invalid_parameters_leave_no_file(module_service, updates):
    with pytest.raises(ConfigProfileValidationError):
        create_scan(module_service, **updates)
    assert module_service.list_module_profiles("acquisition") == []


@pytest.mark.parametrize("channels", [2, 9, 10, 33, 65])
def test_scan45_profiles_allow_device_channel_count(module_service, channels):
    profile = create_scan(module_service, channel_number=channels)
    module_service.select_module_profile("acquisition", profile.profile_id)
    assert module_service.resolve_snapshot()["ACQUISITION.channel_number"] == channels


def test_missing_parameters_and_unknown_plugins_rejected(module_service):
    svc = module_service
    for plugin in ("neuroscan_scan45_reader", "unknown_reader"):
        with pytest.raises(ConfigProfileValidationError):
            svc.create_module_profile("acquisition", plugin, "Incomplete", {"sample_rate": 500})
    with pytest.raises(ConfigProfileValidationError):
        svc.module_detail("montage")


def test_active_cannot_be_deleted(module_service):
    svc = module_service
    p = create_scan(svc)
    svc.select_module_profile("acquisition", p.profile_id)
    with pytest.raises(ConfigProfileConflictError):
        svc.delete_module_profile("acquisition", p.profile_id)
    svc.select_module_profile("acquisition", None)
    svc.delete_module_profile("acquisition", p.profile_id)
    assert svc.list_module_profiles("acquisition") == []


def test_missing_or_invalid_selection_blocks_snapshot_but_remains_inspectable(module_service):
    svc = module_service
    p = create_scan(svc)
    svc.select_module_profile("acquisition", p.profile_id)
    svc._repository.save(replace(p, parameters={"sample_rate": -1}))
    with pytest.raises(ConfigProfileValidationError):
        svc.resolve_snapshot()
    assert svc.module_detail("acquisition")["selection_error"]
    svc.select_module_profile("acquisition", None)
    assert svc.resolve_snapshot()["ACQUISITION.sample_rate"] == 1000


def test_file_reader_has_no_speed_control_and_curry_has_its_own_port_default(module_service):
    detail = module_service.module_detail("acquisition")
    assert detail["effective_parameters"]["data_file_path"] == "recordings/demo.cnt"
    plugins = {p["plugin_type"]: p for p in detail["plugins"]}
    file_keys = {f["key"] for f in plugins["file_reader"]["fields"]}
    assert "file_reader_play_speed" not in file_keys
    curry = {f["key"]: f["default"] for f in plugins["neuroscan_curry8_reader"]["fields"]}
    assert curry["DATA_PROVIDER_PORT"] == 4455


def test_selected_profile_owns_acquisition_fields_in_legacy_api(module_service):
    svc = module_service
    p = create_scan(svc)
    svc.select_module_profile("acquisition", p.profile_id)
    with pytest.raises(ConfigProfileConflictError):
        svc.apply({"ACQUISITION.sample_rate": 250})
    svc.apply({"ACQUISITION.sample_rate": 250}, scope="inactive legacy")
    assert svc.resolve_snapshot()["ACQUISITION.sample_rate"] == 500


@pytest.mark.parametrize("name", [None, "", "   ", "a\nb", "a" * 65])
def test_invalid_names_are_rejected_before_writing(module_service, name):
    with pytest.raises(ConfigProfileValidationError):
        create_scan(module_service, name)
    assert module_service.list_module_profiles("acquisition") == []
