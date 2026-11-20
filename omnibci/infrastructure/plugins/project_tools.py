"""Build fixed source snapshots using the normal static ZIP installer."""
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from omnibci.infrastructure.plugins.package_installer import (
    InstallationLimits, PackageInstallError, PluginPackageInstaller, _portable_path,
)

_EXCLUDED = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode",
    ".omnibci-dev", ".ds_store", ".coverage",
})


def _project_files(project, limits):
    def visit(directory):
        for path in sorted(directory.iterdir()):
            if path.name.casefold() in _EXCLUDED or path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PackageInstallError(f"Source must not contain symlinks: {path}")
            if stat.S_ISDIR(mode):
                if not (path / "pyvenv.cfg").is_file():
                    yield from visit(path)
            elif stat.S_ISREG(mode):
                yield path
            else:
                raise PackageInstallError(f"Source must contain only regular files: {path}")

    paths = []
    for path in visit(project):
        paths.append(path)
        if len(paths) > limits.max_entries:
            raise PackageInstallError("Source exceeds the entry count limit")
    return sorted(paths, key=lambda path: path.relative_to(project).as_posix())


def _write_archive(project, destination, limits):
    total = 0
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for path in _project_files(project, limits):
            name = path.relative_to(project).as_posix()
            _portable_path(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            limit = min(limits.max_file_bytes, limits.max_manifest_bytes) if name == "manifest.toml" else limits.max_file_bytes
            size = 0
            with path.open("rb") as reader, archive.open(info, "w") as writer:
                observed = os.fstat(reader.fileno())
                if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
                    raise PackageInstallError(f"Source file changed or became a symlink: {path}")
                if observed.st_size > limit:
                    raise PackageInstallError(f"Source file exceeds the size limit: {name}")
                while block := reader.read(1024 * 1024):
                    size += len(block)
                    total += len(block)
                    if size > limit or total > min(limits.max_expanded_bytes, limits.max_archive_bytes):
                        raise PackageInstallError("Source exceeds the size limit")
                    writer.write(block)
    if destination.stat().st_size > limits.max_archive_bytes:
        raise PackageInstallError("Archive exceeds the size limit")


def package_project(project: Path, destination: Path, *, limits: InstallationLimits | None = None) -> dict:
    """Publish a deterministic ZIP only after the shared installer accepts it."""
    project, destination = Path(project).resolve(), Path(destination).resolve()
    if destination.is_relative_to(project):
        raise PackageInstallError("Package output must be outside the selected project root")
    if not project.is_dir() or not (project / "manifest.toml").is_file():
        raise PackageInstallError("Select the plugin directory containing manifest.toml")
    limits = limits or InstallationLimits()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="__package-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive = staging / "plugin.zip"
        _write_archive(project, archive, limits)
        installer = PluginPackageInstaller(staging / "plugins" / "packages", limits=limits)
        installed = installer.install(archive)
        receipt = installer.read_receipt(installed.location, installed.package_id, installed.version)
        result = {
            "package_id": installed.package_id, "version": installed.version, "sha256": installed.sha256,
            "contributions": {item.contribution_id: item.category for item in installed.contributions},
            "files": sorted(receipt["files"]),
        }
        os.replace(archive, destination)
        return result


def validate_project(project: Path, *, limits: InstallationLimits | None = None) -> dict:
    """Validate the exact packageable snapshot without importing its Python code."""
    with tempfile.TemporaryDirectory(prefix="omnibci-validate-") as temporary:
        return package_project(project, Path(temporary) / "plugin.zip", limits=limits)
