"""
OmniBCISettings layered-profile resolution (配置分级 #1).

All tests use a temporary settings.toml + profiles/ dir — the real
omnibci/conf/settings.toml is never touched.
"""
import pytest
import tomlkit

from omnibci.conf import OmniBCISettings, ProfileNameError, ProfileNotFoundError


@pytest.fixture()
def s(tmp_path):
    toml = tmp_path / "settings.toml"
    toml.write_text(
        "[ACQUISITION]\n"
        "signal_source = \"file_reader\"\n"
        "sample_rate = 1000\n"
        "[ALGORITHM]\n"
        "window_duration = 3\n",
        encoding="utf-8",
    )
    return OmniBCISettings(toml_file=toml)


class TestDefaultsOnly:
    def test_get_reads_default(self, s):
        assert s.get("ACQUISITION.signal_source") == "file_reader"

    def test_missing_key_raises_name_error(self, s):
        with pytest.raises(NameError):
            s.get("NOPE.nothing")

    def test_no_active_profile_initially(self, s):
        assert s.active_profile is None
        assert s.list_profiles() == []


class TestProfileCrud:
    def test_write_then_read(self, s):
        s.write_profile("快速调试", {"ACQUISITION.signal_source": "neuracle"}, "实采设备")
        assert s.read_profile("快速调试") == {"ACQUISITION.signal_source": "neuracle"}

    def test_list_carries_metadata(self, s):
        s.write_profile("p1", {"ALGORITHM.window_duration": 5.0}, "长窗")
        (info,) = s.list_profiles()
        assert info["name"] == "p1"
        assert info["description"] == "长窗"
        assert info["key_count"] == 1
        assert info["updated_at"] is not None

    def test_merge_semantics(self, s):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.write_profile("p", {"ALGORITHM.window_duration": 4.0})
        assert s.read_profile("p") == {
            "ACQUISITION.sample_rate": 500, "ALGORITHM.window_duration": 4.0,
        }

    def test_replace_semantics(self, s):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.write_profile("p", {"ALGORITHM.window_duration": 4.0}, replace=True)
        assert s.read_profile("p") == {"ALGORITHM.window_duration": 4.0}

    def test_read_missing_raises(self, s):
        with pytest.raises(ProfileNotFoundError):
            s.read_profile("ghost")

    def test_delete(self, s):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.delete_profile("p")
        assert s.list_profiles() == []
        with pytest.raises(ProfileNotFoundError):
            s.delete_profile("p")

    @pytest.mark.parametrize("bad", ["", "a/b", "../up", "a" * 65, "x\\y", "a.b"])
    def test_invalid_names_rejected(self, s, bad):
        with pytest.raises(ProfileNameError):
            s.write_profile(bad, {"ACQUISITION.sample_rate": 1})

    def test_chinese_name_ok(self, s):
        s.write_profile("文件回放_调试-01", {"ACQUISITION.sample_rate": 1})
        assert s.list_profiles()[0]["name"] == "文件回放_调试-01"


class TestActivationAndOverlay:
    def test_activate_overlays_get(self, s):
        s.write_profile("p", {"ACQUISITION.signal_source": "neuracle"})
        s.set_active_profile("p")
        assert s.get("ACQUISITION.signal_source") == "neuracle"   # overlaid
        assert s.get("ACQUISITION.sample_rate") == 1000           # falls through
        assert s.get_default("ACQUISITION.signal_source") == "file_reader"
        assert s.get_override("ACQUISITION.signal_source") == "neuracle"

    def test_deactivate_restores_default(self, s):
        s.write_profile("p", {"ACQUISITION.signal_source": "neuracle"})
        s.set_active_profile("p")
        s.set_active_profile(None)
        assert s.active_profile is None
        assert s.get("ACQUISITION.signal_source") == "file_reader"

    def test_activate_missing_raises(self, s):
        with pytest.raises(ProfileNotFoundError):
            s.set_active_profile("ghost")

    def test_activation_survives_reload(self, s):
        """spawn 子进程等价路径:重新从磁盘加载,叠加层必须一致。"""
        s.write_profile("p", {"ALGORITHM.window_duration": 5.0})
        s.set_active_profile("p")
        fresh = OmniBCISettings(toml_file=s._config_path)
        assert fresh.active_profile == "p"
        assert fresh.get("ALGORITHM.window_duration") == 5.0

    def test_editing_active_profile_refreshes_overlay(self, s):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.set_active_profile("p")
        s.write_profile("p", {"ACQUISITION.sample_rate": 250})
        assert s.get("ACQUISITION.sample_rate") == 250

    def test_missing_active_file_degrades_to_defaults(self, s, caplog):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.set_active_profile("p")
        (s._profiles_dir / "p.toml").unlink()
        fresh = OmniBCISettings(toml_file=s._config_path)
        assert fresh.get("ACQUISITION.sample_rate") == 1000  # default, no crash

    def test_set_save_writes_default_not_profile(self, s):
        s.write_profile("p", {"ACQUISITION.sample_rate": 500})
        s.set_active_profile("p")
        s.set("ACQUISITION.sample_rate", 2000)
        s.save()
        assert s.get_default("ACQUISITION.sample_rate") == 2000
        assert s.get("ACQUISITION.sample_rate") == 500      # overlay still wins
        assert s.read_profile("p") == {"ACQUISITION.sample_rate": 500}
