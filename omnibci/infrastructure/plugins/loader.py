#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-28 05:58
import hashlib
import importlib
import logging
import sys
from importlib.machinery import ModuleSpec, NamespaceLoader, PathFinder
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Type, TypeVar

from omnibci.infrastructure.plugins.descriptors import PluginDescriptor
from omnibci.infrastructure.plugins.registry import isolated_registry

logger = logging.getLogger(__name__)

T = TypeVar("T")


class PluginLoadError(RuntimeError):
    """The selected descriptor could not be loaded or instantiated."""


class PluginLoader:

    @staticmethod
    def _module_prefix(descriptor: PluginDescriptor) -> str:
        identity = "\0".join((
            descriptor.category,
            descriptor.identifier,
            descriptor.contribution_id,
            descriptor.version or "",
            str(descriptor.location.resolve()),
        ))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"_omnibci_plugin_{digest}"

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

    @staticmethod
    def _reject_symlinks(location: Path, manifest_path: Path) -> None:
        for child in location.rglob("*"):
            if child.is_symlink():
                raise PluginLoadError(
                    f"{manifest_path}: plugin tree contains symlink {child}"
                )

    @classmethod
    def _prepare_module(
        cls,
        prefix: str,
        location: Path,
        module_path: str,
        manifest_path: Path,
    ) -> tuple[str, Path, Path | None]:
        """Create namespace parents and return the exact source import target."""
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
            cls._install_namespace(parent_name, package_dir)
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
        """Execute the validated ``.py`` path instead of re-resolving its name."""
        search_locations = [str(package_dir)] if package_dir is not None else None
        spec = spec_from_file_location(
            module_name,
            target,
            submodule_search_locations=search_locations,
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"Cannot create an import spec for {target}")

        module = module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    @classmethod
    def load_entrypoint(
        cls,
        descriptor: PluginDescriptor,
        expected_base: Type[T],
    ) -> Type[T]:
        """Load and validate the one class declared by *descriptor*.

        The plugin receives a stable synthetic package namespace, so relative
        imports work while unrelated user directories never enter ``sys.path``.
        The exact validated source file is executed; the plugin root and
        intermediate package ``__init__.py`` files are intentionally skipped.
        """
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
        cls._reject_symlinks(location, descriptor.manifest_path)

        prefix = cls._module_prefix(descriptor)
        modules_before = set(sys.modules)
        try:
            module_name, target, package_dir = cls._prepare_module(
                prefix,
                location,
                entrypoint.module,
                descriptor.manifest_path,
            )
            with isolated_registry():
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
                class_plugin_type = getattr(plugin_class, "plugin_type", None)
                if class_plugin_type != descriptor.plugin_type:
                    raise TypeError(
                        f"entrypoint plugin_type {class_plugin_type!r} does not "
                        f"match manifest plugin_type {descriptor.plugin_type!r}"
                    )
                return plugin_class
        except BaseException as exc:
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
        """Load the selected entrypoint class and construct one instance."""
        prefix = cls._module_prefix(descriptor)
        modules_before = set(sys.modules)
        try:
            plugin_class = cls.load_entrypoint(descriptor, expected_base)
            with isolated_registry():
                return plugin_class(**kwargs)
        except BaseException as exc:
            cls._cleanup_modules(prefix, modules_before)
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(
                f"{descriptor.manifest_path}: failed to instantiate "
                f"{descriptor.identifier}: {exc}"
            ) from exc

    @staticmethod
    def load_from_dir(
        plugin_dir: str,
        base_package: str = "omnibci.infrastructure.plugins",
    ) -> None:
        """Load legacy plugins without retaining a temporary import path."""
        root_path = Path(plugin_dir).resolve()
        if not root_path.exists():
            logger.warning(f"[PluginLoader] Directory not found: {root_path}")
            return

        import_path = str(root_path.parent)
        added_import_path = import_path not in sys.path
        if added_import_path:
            sys.path.insert(0, import_path)

        try:
            count = 0
            for file_path in root_path.rglob("*.py"):
                try:
                    relative_path = file_path.relative_to(root_path)
                except ValueError:
                    continue

                if file_path.name == "__init__.py":
                    module_parts = relative_path.parent.parts
                else:
                    module_parts = relative_path.with_suffix("").parts

                if not module_parts:
                    continue

                module_name = f"{base_package}.{'.'.join(module_parts)}"

                if module_name in sys.modules:
                    continue

                try:
                    importlib.import_module(module_name)
                    count += 1
                    logger.debug(f"Loaded plugin module: {module_name}")
                except Exception as exc:
                    logger.error(
                        f"Failed to load module '{module_name}': {exc}",
                        exc_info=True,
                    )

            logger.info(
                f"[PluginLoader] Scanned '{plugin_dir}' - Loaded {count} modules."
            )
        finally:
            if added_import_path:
                try:
                    sys.path.remove(import_path)
                except ValueError:
                    pass
