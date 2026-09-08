"""
ConfigService unit tests — whitelist policy over layered settings.

Temp settings.toml per test; the real config file is never touched.
"""
import pytest

from omnibci.application.services.config_service import (
    CONFIG_WHITELIST,
    ConfigKeyNotAllowedError,
    ConfigService,
    ConfigValidationError,
    ProfileInUseError,
)
from omnibci.conf import OmniBCISettings, ProfileNotFoundError


@pytest.fixture()
def svc(tmp_path):
    toml = tmp_path / "settings.toml"
    toml.write_text(
        "[ACQUISITION]\n"
        "signal_source = \"file_reader\"\n"
        "sample_rate = 1000\n"
        "parallel_port_address = \"0x3100\"\n"
        "[ALGORITHM]\n"
        "window_duration = 3.0\n"
        "filters = []\n"
        "[STIMULATION]\n"
        "paradigm = \"classic40\"\n"
        "[ACTUATION]\n"
        "effector = \"\"\n",
        encoding="utf-8",
    )
    return ConfigService(OmniBCISettings(toml_file=toml))


class TestListEntries:
    def test_covers_whole_whitelist(self, svc):
        entries, active = svc.list_entries()
        assert {e["key"] for e in entries} == set(CONFIG_WHITELIST)
        assert active is None

    def test_effective_equals_default_without_profile(self, svc):
        entries, _ = svc.list_entries()
        e = next(x for x in entries if x["key"] == "ACQUISITION.sample_rate")
        assert e["default_value"] == 1000
        assert e["profile_value"] is None
        assert e["effective_value"] == 1000

    def test_overlay_reflected(self, svc):
        svc.apply({"ACQUISITION.sample_rate": 500}, scope="实采")
        svc.activate("实采")
        entries, active = svc.list_entries()
        e = next(x for x in entries if x["key"] == "ACQUISITION.sample_rate")
        assert (e["default_value"], e["profile_value"], e["effective_value"]) == (1000, 500, 500)
        assert active == "实采"


class TestApplyValidation:
    @pytest.mark.parametrize("value", [0x3100, True, None, 1.5])
    def test_parallel_port_address_requires_hexadecimal_text(self, svc, value):
        with pytest.raises(ConfigValidationError) as exc:
            svc.apply({"ACQUISITION.parallel_port_address": value})
        assert "ACQUISITION.parallel_port_address" in exc.value.bad_keys

    def test_unknown_key_rejected(self, svc):
        with pytest.raises(ConfigKeyNotAllowedError) as exc:
            svc.apply({"LOGGING.level": "DEBUG"})
        assert exc.value.keys == ["LOGGING.level"]

    def test_type_mismatch_rejected(self, svc):
        with pytest.raises(ConfigValidationError) as exc:
            svc.apply({"ACQUISITION.sample_rate": "fast"})
        assert "ACQUISITION.sample_rate" in exc.value.bad_keys

    def test_bool_not_accepted_as_int(self, svc):
        with pytest.raises(ConfigValidationError):
            svc.apply({"ACQUISITION.sample_rate": True})

    def test_list_str_checked_elementwise(self, svc):
        with pytest.raises(ConfigValidationError):
            svc.apply({"ALGORITHM.filters": ["ok", 42]})

    def test_effector_key_is_allowed_and_type_checked(self, svc):
        applied, restart = svc.apply({"ACTUATION.effector": "logger"})
        assert applied == ["ACTUATION.effector"]
        assert restart is True
        assert svc._settings.get_default("ACTUATION.effector") == "logger"

        with pytest.raises(ConfigValidationError):
            svc.apply({"ACTUATION.effector": 42})

    def test_effector_is_soft_validated_against_effector_catalog(self, svc):
        categories = []
        svc._plugin_catalog = lambda category: categories.append(category) or []

        svc.apply({"ACTUATION.effector": "logger"})

        assert categories == ["effector"]

    def test_empty_updates_rejected(self, svc):
        with pytest.raises(ConfigValidationError):
            svc.apply({})

    def test_all_or_nothing(self, svc):
        with pytest.raises(ConfigValidationError):
            svc.apply({"ACQUISITION.sample_rate": 250, "ALGORITHM.window_duration": "x"})
        # the valid key must NOT have been written
        assert svc._settings.get_default("ACQUISITION.sample_rate") == 1000


class TestApplyScopes:
    @pytest.mark.parametrize("scope", ["default", "hardware"])
    def test_parallel_port_address_is_editable_and_survives_reload(
        self, svc, tmp_path, scope,
    ):
        key = "ACQUISITION.parallel_port_address"

        applied, restart = svc.apply({key: "0x378"}, scope=scope)
        if scope != "default":
            svc.activate(scope)
        settings = OmniBCISettings(tmp_path / "settings.toml")
        entries, _ = ConfigService(settings).list_entries()
        entry = next(item for item in entries if item["key"] == key)

        assert applied == [key]
        assert restart is True
        assert settings.get(key) == "0x378"
        assert entry["type"] == "str"
        assert entry["effective_value"] == "0x378"
        if scope != "default":
            assert entry["default_value"] == "0x3100"

    def test_default_scope_persists(self, svc, tmp_path):
        applied, restart = svc.apply({"ACQUISITION.sample_rate": 2000})
        assert applied == ["ACQUISITION.sample_rate"]
        assert restart is True
        fresh = OmniBCISettings(toml_file=tmp_path / "settings.toml")
        assert fresh.get("ACQUISITION.sample_rate") == 2000

    def test_hot_key_reports_no_restart(self, svc):
        _, restart = svc.apply({"STIMULATION.paradigm": "other"})
        assert restart is False

    def test_profile_scope_does_not_touch_default(self, svc):
        svc.apply({"ACQUISITION.sample_rate": 500}, scope="p1")
        assert svc._settings.get_default("ACQUISITION.sample_rate") == 1000
        assert svc._settings.read_profile("p1") == {"ACQUISITION.sample_rate": 500}

    def test_int_normalised_for_float_keys(self, svc):
        svc.apply({"ALGORITHM.window_duration": 4}, scope="p")
        assert svc._settings.read_profile("p") == {"ALGORITHM.window_duration": 4.0}


class TestProfileLifecycle:
    def test_put_replaces(self, svc):
        svc.apply({"ACQUISITION.sample_rate": 500}, scope="p")
        svc.put_profile("p", {"ALGORITHM.window_duration": 5.0}, "长窗")
        assert svc._settings.read_profile("p") == {"ALGORITHM.window_duration": 5.0}

    def test_delete_active_raises_in_use(self, svc):
        svc.apply({"ACQUISITION.sample_rate": 500}, scope="p")
        svc.activate("p")
        with pytest.raises(ProfileInUseError):
            svc.delete_profile("p")

    def test_delete_after_deactivate(self, svc):
        svc.apply({"ACQUISITION.sample_rate": 500}, scope="p")
        svc.activate("p")
        svc.deactivate()
        svc.delete_profile("p")
        assert svc.list_profiles() == ([], None)

    def test_activate_missing_raises(self, svc):
        with pytest.raises(ProfileNotFoundError):
            svc.activate("ghost")

    def test_profile_detail_flags_off_whitelist_keys(self, svc):
        svc._settings.write_profile("p", {"LOGGING.level": "DEBUG"})  # 模拟手编文件
        detail = svc.profile_detail("p")
        assert detail["off_whitelist_keys"] == ["LOGGING.level"]
