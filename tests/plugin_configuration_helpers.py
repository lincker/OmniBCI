"""Explicit profile selections for tests that exercise configured backends."""


def accept_profiles(service, selections):
    bindings = []
    for slot, kind, plugin_type, parameters in selections:
        definition = next(item for item in service.catalog.list(kind) if item.plugin_type == plugin_type)
        profile = service.create(kind, definition.selection, plugin_type, parameters)
        bindings.append({"slot": slot, "profile_id": profile.profile_id})
    current = service.repository.get_activation()
    return service.accept(service.propose(bindings),
                          expected_revision=current.revision if current else None)
