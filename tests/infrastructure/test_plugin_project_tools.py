"""Source projects use the same static package admission as shared ZIP files."""
import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile

import pytest
import tomlkit

from omnibci.infrastructure.plugins.package_installer import InstallationLimits, PackageInstallError, PluginPackageInstaller
from tests.infrastructure.test_package_installer import _manifest


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "manifest.toml").write_text(tomlkit.dumps(_manifest()))
    (root / "driver.py").write_text("raise AssertionError('Static tools must not import plugins')\n")
    (root / "setup.py").write_text("raise AssertionError('Static tools must not run build hooks')\n")
    (root / "resources").mkdir()
    (root / "resources" / "data.bin").write_bytes(b"runtime resource\x00")
    return root


@pytest.fixture
def tools():
    return importlib.import_module("omnibci.infrastructure.plugins.project_tools")


def test_static_validation_and_packaging_have_identical_inventory(tools, project, tmp_path, monkeypatch):
    ordinary = tmp_path / "ordinary"
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(ordinary))
    validated = tools.validate_project(project)
    archive = tmp_path / "release.zip"
    packaged = tools.package_project(project, archive)
    assert validated == packaged
    assert packaged["package_id"] == "acme.device"
    assert packaged["contributions"] == {"reader": "acquisition"}
    assert packaged["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert packaged["files"] == ["driver.py", "manifest.toml", "resources/data.bin", "setup.py"]
    installed = PluginPackageInstaller(tmp_path / "installed").install(archive)
    assert installed.sha256 == packaged["sha256"]
    assert (installed.location / "resources/data.bin").read_bytes() == b"runtime resource\x00"
    assert not ordinary.exists()


def test_archive_is_deterministic_and_prunes_tooling_not_runtime_resources(tools, project, tmp_path):
    for name in (".git", ".venv", "__pycache__", ".pytest_cache", ".omnibci-dev"):
        (project / name).mkdir()
        (project / name / "junk").write_text("excluded")
    (project / "custom-env").mkdir()
    (project / "custom-env" / "pyvenv.cfg").write_text("home = unused")
    (project / "custom-env" / "junk").write_text("excluded")
    (project / "resources" / "nested.zip").write_bytes(b"an intentional resource")
    (project / "driver.pyc").write_bytes(b"stale bytecode")
    (project / ".DS_Store").write_bytes(b"desktop metadata")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    tools.package_project(project, first)
    for path in project.rglob("*"):
        os.utime(path, (1234567890, 1234567890))
    (project / "driver.py").chmod(0o755)
    tools.package_project(project, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["driver.py", "manifest.toml", "resources/data.bin", "resources/nested.zip", "setup.py"]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


@pytest.mark.parametrize("name", ["inside.zip", "dist/inside.zip"])
def test_package_output_must_be_outside_selected_source_root(tools, project, name):
    with pytest.raises(PackageInstallError, match="outside"):
        tools.package_project(project, project / name)
    assert not (project / name).exists()


@pytest.mark.parametrize("name", ["resources/link.bin", "manifest-copy.toml"])
def test_source_symlinks_are_not_followed(tools, project, tmp_path, name):
    outside = tmp_path / "private.txt"
    outside.write_text("must not enter archive")
    (project / name).symlink_to(outside)
    with pytest.raises(PackageInstallError, match="symlink"):
        tools.package_project(project, tmp_path / "release.zip")
    assert not (tmp_path / "release.zip").exists()


@pytest.mark.parametrize("name", ["CON.txt", "bad\\name.py", ".omnibci-install.json", "omnibci.py"])
def test_shared_installer_rejects_nonportable_and_host_owned_paths(tools, project, tmp_path, name):
    (project / name).write_text("invalid archive member")
    with pytest.raises(PackageInstallError):
        tools.package_project(project, tmp_path / "release.zip")


def test_invalid_project_does_not_replace_existing_archive(tools, project, tmp_path):
    archive = tmp_path / "release.zip"
    archive.write_bytes(b"previous release")
    (project / "driver.py").unlink()
    with pytest.raises(PackageInstallError, match="entrypoint"):
        tools.package_project(project, archive)
    assert archive.read_bytes() == b"previous release"


def test_wrapped_repository_requires_explicit_plugin_root(tools, project):
    with pytest.raises(PackageInstallError, match="manifest"):
        tools.validate_project(project.parent)
    assert tools.validate_project(project)["version"] == "1.0.0"


def test_project_size_limits_apply_before_publication(tools, project, tmp_path):
    with pytest.raises(PackageInstallError, match="limit"):
        tools.package_project(project, tmp_path / "release.zip", limits=InstallationLimits(max_file_bytes=16))
    assert not (tmp_path / "release.zip").exists()


def test_cli_validate_and_package_print_the_same_static_result(project, tmp_path):
    command = [sys.executable, "-B", "-m", "omnibci.plugin_cli"]
    validated = subprocess.run([*command, "validate", str(project)], capture_output=True, text=True)
    assert validated.returncode == 0, validated.stderr
    output = tmp_path / "release.zip"
    packaged = subprocess.run([*command, "package", str(project), "--output", str(output)], capture_output=True, text=True)
    assert packaged.returncode == 0, packaged.stderr
    assert json.loads(validated.stdout) == json.loads(packaged.stdout)
    assert output.is_file()
