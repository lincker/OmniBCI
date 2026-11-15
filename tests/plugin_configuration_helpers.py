"""Explicit profile selections for tests that exercise configured servers."""
from copy import deepcopy

from omnibci.application.configuration.definitions import MODULES, ENGINE_SETTING_DEFAULTS, SECTION_SETTING_KEYS


def section_settings(settings, section):
    """Copy the values owned by one section from a composite test fixture."""
    return {key: deepcopy(settings[key]) for key in SECTION_SETTING_KEYS[section] if key in settings}


def builtin_selection(kind, alias):
    """Choose an exact built-in package identity for a test fixture."""
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog

    return next(item.selection for item in PluginCatalog().list_contributions(kind) if item.plugin_type == alias)


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
