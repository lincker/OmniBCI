"""Explicit development owns fixed artifacts and never borrows normal inventory."""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

from omnibci import sdk
from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.packages import PluginPackageConflictError
from omnibci.infrastructure.plugin_management.loader import PluginLoader
from omnibci.infrastructure.plugin_management.installer import PackageInstallError
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginNotFoundError
from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer
from omnibci.infrastructure.plugin_management.project_tools import package_project
from tests.plugin_development_helpers import KINDS, SLOTS, source_project, configuration_file, module_configuration


@pytest.fixture
def development():
    return importlib.import_module("omnibci.plugin_development")


@pytest.mark.parametrize("timeout", [None, 30.0])
def test_development_command_timeout_only_changes_isolated_server_settings(development, tmp_path, monkeypatch, timeout):
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    settings = ordinary / "settings.toml"
    original = '[NEUROENGINE]\ncommand_timeout_s=12.0\n'
    settings.write_text(original)
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(ordinary))
    run = development._prepare_run(project, configuration, "effector", (), project / ".omnibci-dev",
                                   command_timeout=timeout)
    isolated = tomlkit.loads((run / "configuration/settings.toml").read_text())
    assert isolated.get("NEUROENGINE", {}).get("command_timeout_s") == timeout
    assert settings.read_text() == original


@pytest.mark.parametrize("timeout", [0, -1, True, "30", float("inf"), float("nan")])
def test_invalid_development_timeout_fails_before_artifact_or_process_creation(development, tmp_path, monkeypatch, timeout):
    from omnibci.application.configuration.errors import ConfigValidationError

    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    monkeypatch.setattr(development.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Host must not launch"))
    with pytest.raises(ConfigValidationError):
        development.develop_project(project, configuration, "effector", command_timeout=timeout)
    assert not list((project / ".omnibci-dev").glob("run-*"))
    assert not (project / ".omnibci-dev/state").exists()


def test_develop_cli_passes_explicit_host_command_timeout(development, tmp_path, monkeypatch):
    from omnibci.plugin_cli import main

    calls = []
    monkeypatch.setattr(development, "develop_project", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    assert main(["develop", str(tmp_path), "--contribution", "reader", "--configuration", str(tmp_path / "current.json"),
                 "--command-timeout", "30"]) == 0
    assert calls[0][1]["command_timeout"] == 30.0


def test_ordinary_discovery_does_not_load_unmanaged_source_directories(tmp_path):
    project = source_project(tmp_path / "plugins" / "acquisition")
    catalog = PluginCatalog(plugin_dir=tmp_path / "plugins")
    assert project.is_dir()
    with pytest.raises(PluginNotFoundError):
        catalog.resolve_selection("acquisition", PluginSelection("test.device", "acquisition", "1.0.0"))


@pytest.mark.parametrize("kind,port", [
    ("acquisition", sdk.SignalSource), ("filter", sdk.Filter), ("decoder", sdk.Decoder),
    ("epoch_detector", sdk.EpochDetector), ("paradigm", sdk.Paradigm),
    ("trigger", sdk.TriggerEmitter), ("effector", sdk.Effector),
])
def test_each_kind_uses_installed_development_artifact_and_canonical_loader(development, tmp_path, kind, port):
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, KINDS)
    run = development._prepare_run(project, configuration, kind, (), project / ".omnibci-dev")
    record = json.loads((run / "run.json").read_text())
    assert record["module"] == SLOTS[kind].split(".")[0]
    store = ManagedPluginPackageStore(run / "plugins")
    selection = PluginSelection("test.device", kind, "1.0.0")
    descriptor = store.catalog.resolve_selection(kind, selection)
    assert descriptor.package.domain == "acquisition"
    assert store.get_package(selection.package_id, selection.version).origin == "development"
    binding = ProfileBinding(SLOTS[kind], kind, None, selection, {}, 1)
    snapshot = PluginArtifactPreparer(store).prepare_snapshot({"PLUGINS.bindings": [binding.to_dict()]}, [record["module"]])
    assert snapshot["PLUGINS.artifacts"][SLOTS[kind]]["origin"] == "development"
    kwargs = {"parameters": {}}
    if kind in {"filter", "decoder", "epoch_detector"}:
        kwargs["input_signal"] = sdk.SignalInfo(sample_rate=250, channels=(sdk.ChannelDescriptor(0, "Oz", channel_id="oz"),))
    instance = PluginLoader.instantiate(descriptor, port, **kwargs)
    assert instance.value == "first"
    assert instance.resource_root.is_relative_to(run)
    assert instance.resource_root != project


def test_same_version_edits_create_new_snapshots_without_changing_previous_run(development, tmp_path, monkeypatch):
    ordinary = tmp_path / "ordinary"
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(ordinary))
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    first = development._prepare_run(project, configuration, "effector", (), project / ".omnibci-dev")
    (project / "value.txt").write_text("second")
    second = development._prepare_run(project, configuration, "effector", (), project / ".omnibci-dev")
    assert json.loads((first / "run.json").read_text())["sha256"] != json.loads((second / "run.json").read_text())["sha256"]
    for run, value in ((first, "first"), (second, "second")):
        assert (run / "plugins/packages/test.device/1.0.0/value.txt").read_text() == value
    assert not ordinary.exists()
    assert os.environ["OMNIBCI_PLUGIN_DIR"] == str(ordinary)


def test_companion_selection_requires_its_own_archive_not_normal_inventory(development, tmp_path, monkeypatch):
    project = source_project(tmp_path)
    companion = source_project(tmp_path / "companion", publisher="companion")
    archive = tmp_path / "companion.zip"
    package_project(companion, archive)
    ordinary = ManagedPluginPackageStore(tmp_path / "ordinary")
    ordinary.install_archive(archive)
    ordinary.set_enabled("companion.device", "1.0.0", True)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(ordinary.plugin_dir))
    configuration = module_configuration(["acquisition", "decoder", "epoch_detector"])
    configuration["acquisition"]["bindings"][0]["selection"]["package_id"] = "companion.device"
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(configuration))
    with pytest.raises(ValueError, match="companion.device"):
        development._prepare_run(project, path, "decoder", (), project / ".omnibci-dev")
    run = development._prepare_run(project, path, "decoder", [archive], project / ".omnibci-dev")
    store = ManagedPluginPackageStore(run / "plugins")
    assert store.get_package("companion.device", "1.0.0").origin == "local"
    assert ordinary.get_package("companion.device", "1.0.0").enabled


@pytest.mark.parametrize("mutation", ["wrong_contribution", "missing_source", "external_host", "saved_revision"])
def test_development_requires_explicit_local_current_configuration(development, tmp_path, mutation):
    project = source_project(tmp_path)
    kinds = ["decoder", "epoch_detector"] if mutation == "missing_source" else ["effector"]
    data = module_configuration(kinds)
    contribution = "decoder" if mutation == "missing_source" else "effector"
    if mutation == "wrong_contribution": contribution = "trigger"
    if mutation == "external_host": data["actuation"]["settings"]["ACTUATION.auto_spawn"] = False
    if mutation == "saved_revision": data["actuation"]["expected_revision"] = "previous"
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, PackageInstallError)):
        development._prepare_run(project, path, contribution, (), project / ".omnibci-dev")


@pytest.mark.parametrize("exit_code", [0, 1])
def test_launch_cleanup_uses_child_exit_code_and_never_locks_workspace(development, tmp_path, monkeypatch, exit_code):
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    starts = []
    codes = iter([exit_code, 0])
    before = dict(os.environ)

    def spawn(command, **kwargs):
        run = Path(command[-2])
        starts.append(run)
        assert kwargs["env"]["OMNIBCI_PLUGIN_DIR"] == str(run / "plugins")
        assert kwargs["env"]["OMNIBCI_CONFIG_DIR"] == str(run / "configuration")
        code = next(codes)
        return SimpleNamespace(wait=lambda: code)

    monkeypatch.setattr(development.subprocess, "Popen", spawn)
    if exit_code == 0:
        assert development.develop_project(project, configuration, "effector", port=8123) == 0
        assert not starts[0].exists()
        assert development.develop_project(project, configuration, "effector", port=8123) == 0
        assert not starts[1].exists()
    else:
        with pytest.raises(PluginPackageConflictError, match="unconfirmed"):
            development.develop_project(project, configuration, "effector", port=8123)
        assert starts[0].exists()
        assert not (starts[0] / "shutdown.json").exists()
        # A subsequent run succeeds cleanly without manual marker deletion.
        assert development.develop_project(project, configuration, "effector", port=8123) == 0
        assert not starts[1].exists()
        assert len(starts) == 2
    assert not (project / ".omnibci-dev/state").exists()
    assert dict(os.environ) == before


def test_repeated_interrupts_request_cleanup_once_and_join_the_host(development):
    from unittest.mock import Mock
    process = Mock()
    process.wait.side_effect = [KeyboardInterrupt, KeyboardInterrupt, 0]
    assert development._wait_host(process) == 0
    process.send_signal.assert_called_once()


def test_failed_preparation_does_not_leave_artifacts_or_state(development, tmp_path):
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    with pytest.raises(PackageInstallError):
        development.develop_project(project, configuration, "missing")
    assert not (project / ".omnibci-dev/state").exists()
    assert not list((project / ".omnibci-dev").glob("run-*"))


def test_interrupted_process_creation_retains_failed_snapshot_without_blocking_retry(development, tmp_path, monkeypatch):
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ["effector"])
    attempts = 0

    def spawn(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(development.subprocess, "Popen", spawn)
    with pytest.raises(KeyboardInterrupt):
        development.develop_project(project, configuration, "effector")
    retained = list((project / ".omnibci-dev").glob("run-*"))
    assert len(retained) == 1
    assert not (project / ".omnibci-dev/state").exists()
    assert development.develop_project(project, configuration, "effector") == 0
    assert attempts == 2


def test_development_workspace_cannot_redirect_to_another_store(tmp_path, monkeypatch):
    from omnibci.plugin_development import develop_project
    project = source_project(tmp_path)
    configuration = configuration_file(tmp_path, ['effector'])
    ordinary = tmp_path / 'ordinary'
    ordinary.mkdir()
    (project / '.omnibci-dev').symlink_to(ordinary, target_is_directory=True)
    monkeypatch.setattr('omnibci.plugin_development.subprocess.Popen', lambda *a, **k: pytest.fail('Must reject before starting a host'))
    with pytest.raises(PackageInstallError, match='symlink'):
        develop_project(project, configuration, 'effector')
    assert not list(ordinary.iterdir())
