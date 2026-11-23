"""Explicit profile selections for tests that exercise configured servers."""
from copy import deepcopy

from omnibci.application.configuration.definitions import MODULES, ENGINE_SETTING_DEFAULTS, SECTION_SETTING_KEYS


def section_settings(settings, section):
    """Copy the values owned by one section from a composite test fixture."""
    return {key: deepcopy(settings[key]) for key in SECTION_SETTING_KEYS[section] if key in settings}


def builtin_selection(kind, alias):
    """Choose an exact built-in package identity for a test fixture."""
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog

    return next(item.selection for item in PluginCatalog().list_contributions(kind) if item.plugin_type == alias)


def prepared_snapshot(snapshot, modules=None):
    """Create a fresh local preparation after a test edits its candidate inputs."""
    snapshot = deepcopy(snapshot)
    snapshot.pop('PLUGINS.artifacts', None)
    if modules is None:
        modules = tuple(dict.fromkeys(binding['slot'].split('.', 1)[0] for binding in snapshot['PLUGINS.bindings']))
    return prepare_runtime_plugins(snapshot, modules)


def prepare_runtime_plugins(snapshot, modules):
    """Use the host preparation path without replacing existing artifact pins."""
    from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer

    return PluginArtifactPreparer(ManagedPluginPackageStore()).prepare_snapshot(snapshot, modules)


def install_package_root(root):
    """Publish an external-style fixture through the real managed installer."""
    import zipfile
    from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore

    archive = root.with_name(root.name + '.zip')
    with zipfile.ZipFile(archive, 'w') as writer:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                writer.write(path, path.relative_to(root).as_posix())
    store = ManagedPluginPackageStore()
    package = store.install_archive(archive).package
    store.set_enabled(package.package_id, package.version, True)
    return package


def accept_profiles(service, selections, *, module_settings=None, engine_settings=None):
    by_module = {}
    for slot, kind, plugin_type, parameters in selections:
        definition = next(item for item in service.catalog.list(kind) if item.plugin_type == plugin_type)
        profile = service.create(kind, definition.selection, plugin_type, parameters)
        by_module.setdefault(slot.split(".", 1)[0], []).append({"slot": slot, "profile_id": profile.profile_id})
    if service.repository.get_engine() is None:
        service.repository.save_engine(service.propose_engine(
            ENGINE_SETTING_DEFAULTS if engine_settings is None else engine_settings), expected_revision=None)
    for module in MODULES:
        if module in by_module:
            current = service.repository.get_module(module)
            settings = {**service.default_module_settings(module), **(module_settings or {}).get(module, {})}
            service.repository.save_module(service.propose(module, by_module[module], settings=settings),
                                  expected_revision=current.revision if current else None)
    return service.repository.get_activation_set()


def output_payload_files(document):
    """Give synthetic output Contributions explicit empty-object contracts."""
    import json

    files = {}
    for index, contribution in enumerate(document.get("contributions", [])):
        kind = contribution.get("kind")
        if not isinstance(kind, str):
            continue
        fields = {"paradigm": ("trial", "result"), "effector": ("command",)}.get(kind, ())
        if fields and "payload_schemas" not in contribution:
            name = f"payload-{index}.schema.json"
            contribution["payload_schemas"] = dict.fromkeys(fields, name)
            files[name] = json.dumps({"type": "object", "properties": {}, "additionalProperties": False})
    return files
