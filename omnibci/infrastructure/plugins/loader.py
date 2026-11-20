#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-28 05:58
import hashlib
import sys
from threading import RLock
from importlib import import_module
from importlib.abc import MetaPathFinder
from importlib.machinery import ExtensionFileLoader, ModuleSpec, NamespaceLoader, PathFinder, SourceFileLoader, SourcelessFileLoader
from importlib.util import spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Type, TypeVar

from omnibci.application.plugin_schema import validate_parameters
from omnibci.infrastructure.plugins.descriptors import PluginDescriptor
from omnibci.infrastructure.plugins.artifact_inventory import artifact_inventory, content_digest
from omnibci.domain.signal_info import SignalInfo

T = TypeVar("T")


class PluginLoadError(RuntimeError):
    """The selected descriptor could not be loaded or instantiated."""


_ENTRYPOINT_PATHS: dict[str, tuple[Path, Path | None]] = {}
_ARTIFACT_FILES: dict[str, tuple[Path, dict[str, str]]] = {}


def _verified_source(fullname: str, path: str, source: bytes) -> bytes:
    root, files = _ARTIFACT_FILES[fullname.split('.', 1)[0]]
    location = Path(path)
    if location != location.resolve() or not location.is_relative_to(root):
        raise PluginLoadError('Plugin source location changed since preparation')
    expected = files.get(location.relative_to(root).as_posix())
    if expected != hashlib.sha256(source).hexdigest():
        raise PluginLoadError('Plugin source content changed since preparation')
    return source


class _ArtifactSourceLoader(SourceFileLoader):
    def get_code(self, fullname):
        # Never read or write .pyc files inside an immutable plugin artifact.
        source = _verified_source(fullname, self.path, self.get_data(self.path))
        return self.source_to_code(source, self.path)


class _ArtifactFinder(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("_omnibci_plugin_") or path is None:
            return None
        selected = _ENTRYPOINT_PATHS.get(fullname)
        if selected is not None:
            source, package_dir = selected
            return spec_from_file_location(
                fullname, source, loader=_ArtifactSourceLoader(fullname, str(source)),
                submodule_search_locations=[str(package_dir)] if package_dir is not None else None,
            )
        spec = PathFinder.find_spec(fullname, path)
        if spec is not None:
            if isinstance(spec.loader, SourceFileLoader):
                spec.loader = _ArtifactSourceLoader(fullname, spec.origin)
            elif isinstance(spec.loader, SourcelessFileLoader):
                raise PluginLoadError("Plugin modules require Python source, not bytecode-only imports")
            elif isinstance(spec.loader, ExtensionFileLoader):
                _verified_source(fullname, spec.origin, Path(spec.origin).read_bytes())
        return spec


sys.meta_path.insert(0, _ArtifactFinder())


class PluginLoader:

    _lock = RLock()

    @staticmethod
    def _module_prefix(descriptor: PluginDescriptor, files=None) -> str:
        """Share code by local package content, independently of Contribution."""
        location = descriptor.location.resolve()
        digest = hashlib.sha256("\0".join((
            descriptor.identifier, descriptor.version, sys.platform, str(location),
        )).encode("utf-8"))
        try:
            content = content_digest(artifact_inventory(location) if files is None else files)
            if descriptor.package.content_sha256 is not None and descriptor.package.content_sha256 != content:
                raise PluginLoadError('Plugin artifact content changed since preparation')
            digest.update(content.encode())
        except (ValueError, OSError) as exc:
            raise PluginLoadError(str(exc)) from exc
        return f"_omnibci_plugin_{digest.hexdigest()}"

    @staticmethod
    def _install_namespace(name: str, location: Path) -> None:
        """Install a non-executing namespace rooted at *location*."""
        existing = sys.modules.get(name)
        if existing is not None:
            if list(getattr(existing, "__path__", ())) != [str(location)]:
                raise PluginLoadError(
                    f"Synthetic package {name} already points at another directory"
                )
            return

        package = ModuleType(name)
        package.__file__ = None
        package.__package__ = name
        package.__path__ = [str(location)]
        namespace_loader = NamespaceLoader(name, [str(location)], PathFinder.find_spec)
        package.__loader__ = namespace_loader
        package.__spec__ = ModuleSpec(name, loader=namespace_loader, is_package=True)
        package.__spec__.submodule_search_locations = [str(location)]
        sys.modules[name] = package

    @staticmethod
    def _cleanup_modules(prefix: str, modules_before: set[str]) -> None:
        for loaded_name in tuple(sys.modules):
            if (
                loaded_name not in modules_before
                and (loaded_name == prefix or loaded_name.startswith(f"{prefix}."))
            ):
                sys.modules.pop(loaded_name, None)
        for name in tuple(_ENTRYPOINT_PATHS):
            if name == prefix or name.startswith(f"{prefix}."):
                _ENTRYPOINT_PATHS.pop(name, None)
        _ARTIFACT_FILES.pop(prefix, None)

    @classmethod
    def _prepare_module(
        cls,
        prefix: str,
        location: Path,
        module_path: str,
        manifest_path: Path,
    ) -> tuple[str, Path, Path | None]:
        """Create only the synthetic root and validate the exact entrypoint path."""
        cls._install_namespace(prefix, location)
        parts = module_path.split(".")
        parent_path = location
        parent_name = prefix

        for part in parts[:-1]:
            module_file = parent_path / f"{part}.py"
            package_dir = parent_path / part
            if module_file.exists() and package_dir.exists():
                raise PluginLoadError(
                    f"{manifest_path}: ambiguous module prefix {part!r}"
                )
            if module_file.exists() or not package_dir.is_dir():
                raise PluginLoadError(
                    f"{manifest_path}: entrypoint prefix {part!r} is not a package"
                )
            parent_name = f"{parent_name}.{part}"
            parent_path = package_dir

        leaf = parts[-1]
        module_file = parent_path / f"{leaf}.py"
        package_dir = parent_path / leaf
        package_file = package_dir / "__init__.py"
        if module_file.exists() and package_dir.exists():
            raise PluginLoadError(
                f"{manifest_path}: entrypoint module {module_path!r} is ambiguous "
                "(both module file and package directory exist)"
            )
        if not module_file.is_file() and not package_file.is_file():
            raise PluginLoadError(
                f"{manifest_path}: entrypoint module {module_path!r} does not "
                f"exist under {location}"
            )

        target = module_file if module_file.is_file() else package_file
        if not target.resolve().is_relative_to(location):
            raise PluginLoadError(
                f"{manifest_path}: entrypoint resolves outside plugin directory"
            )
        loaded_package_dir = package_dir if target == package_file else None
        return f"{parent_name}.{leaf}", target, loaded_package_dir

    @staticmethod
    def _load_source_module(
        module_name: str,
        target: Path,
        package_dir: Path | None,
    ) -> ModuleType:
        """Use Python's import locks and package initialization for the pinned path."""
        _ENTRYPOINT_PATHS[module_name] = (target, package_dir)
        return import_module(module_name)

    @classmethod
    def load_entrypoint(
        cls,
        descriptor: PluginDescriptor,
        expected_base: Type[T],
    ) -> Type[T]:
        """Load and validate the one class declared by *descriptor*.

        The plugin receives a stable synthetic package namespace, so relative
        imports work while unrelated user directories never enter ``sys.path``.
        The synthetic root executes no initializer. Nested Python packages
        initialize normally, once per locally selected artifact.
        """
        with cls._lock:
            entrypoint = descriptor.entrypoint
            if not descriptor.supports_platform(sys.platform):
                raise PluginLoadError(
                    f"{descriptor.identifier}/{descriptor.contribution_id} is not supported "
                    f"on {sys.platform}"
                )
            if entrypoint is None:
                raise PluginLoadError(
                    f"{descriptor.manifest_path}: plugin has no entrypoint"
                )

            expected_location = descriptor.location
            location = expected_location.resolve()
            if location != expected_location or not location.is_dir():
                raise PluginLoadError(
                    f"{descriptor.identifier}: plugin directory changed after discovery"
                )

            try:
                files = artifact_inventory(location)
            except (ValueError, OSError) as exc:
                raise PluginLoadError(str(exc)) from exc
            prefix = cls._module_prefix(descriptor, files)
            modules_before = set(sys.modules)
            try:
                _ARTIFACT_FILES[prefix] = location, files
                module_name, target, package_dir = cls._prepare_module(
                    prefix,
                    location,
                    entrypoint.module,
                    descriptor.manifest_path,
                )
                module = cls._load_source_module(
                    module_name,
                    target,
                    package_dir,
                )
                plugin_class = getattr(module, entrypoint.attribute)
                if not isinstance(plugin_class, type):
                    raise TypeError(
                        f"{entrypoint.attribute!r} is not a class"
                    )
                if not issubclass(plugin_class, expected_base):
                    raise TypeError(
                        f"{plugin_class.__name__} is not a subclass of "
                        f"{expected_base.__name__}"
                    )
                return plugin_class
            except BaseException as exc:
                if prefix not in modules_before:
                    cls._cleanup_modules(prefix, modules_before)
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, PluginLoadError):
                    raise
                raise PluginLoadError(
                    f"{descriptor.manifest_path}: failed to load entrypoint "
                    f"{entrypoint}: {exc}"
                ) from exc

    @classmethod
    def instantiate(
        cls,
        descriptor: PluginDescriptor,
        expected_base: Type[T],
        **kwargs,
    ) -> T:
        """Construct using the declared API, never a constructor-error fallback."""
        try:
            if descriptor.api_version != "1":
                raise ValueError(f"Unsupported plugin API: {descriptor.api_version}")
            processing = descriptor.category in {"filter", "decoder", "epoch_detector"}
            required = {"parameters", "input_signal"} if processing else {"parameters"}
            if set(kwargs) != required:
                raise ValueError(f"API 1 {descriptor.category} requires {sorted(required)} keyword inputs")
            if processing and not isinstance(kwargs["input_signal"], SignalInfo):
                raise ValueError("Processing requires prepared input_signal")
            arguments = {
                "parameters": validate_parameters(
                    dict(descriptor.configuration_schema) if descriptor.configuration_schema is not None else None,
                    kwargs["parameters"],
                ),
                "resource_root": descriptor.location,
            }
            if processing:
                arguments["input_signal"] = kwargs["input_signal"]
            plugin_class = cls.load_entrypoint(descriptor, expected_base)
            return plugin_class(**arguments)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(
                f"{descriptor.manifest_path}: failed to instantiate "
                f"{descriptor.identifier}: {exc}"
            ) from exc
