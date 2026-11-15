"""Explicit profile selections for tests that exercise configured servers."""
from omnibci.application.activation_settings import MODULES, section_settings


def builtin_selection(kind, alias):
    """Choose an exact built-in package identity for a test fixture."""
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog

    return next(item.selection for item in PluginCatalog().list_contributions(kind) if item.plugin_type == alias)


def accept_profiles(service, selections):
    by_module = {}
    for slot, kind, plugin_type, parameters in selections:
        definition = next(item for item in service.catalog.list(kind) if item.plugin_type == plugin_type)
        profile = service.create(kind, definition.selection, plugin_type, parameters)
        by_module.setdefault(slot.split(".", 1)[0], []).append({"slot": slot, "profile_id": profile.profile_id})
    if service.repository.get_engine() is None:
        service.repository.save_engine(service.propose_engine(section_settings(service.settings_source(), "engine")), expected_revision=None)
    for module in MODULES:
        if module in by_module:
            current = service.repository.get_module(module)
            service.repository.save_module(service.propose(module, by_module[module], settings=section_settings(service.settings_source(), module)),
                                  expected_revision=current.revision if current else None)
    return service.repository.get_activation_set()
