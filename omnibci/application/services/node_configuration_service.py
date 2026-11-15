"""Publish role-specific configuration from the server's settled runtime."""
from omnibci.application.configuration.errors import ConfigProfileConflictError
from omnibci.application.node_configuration import project_node_configuration
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.configuration.models import ActivationSet


class NodeConfigurationService:
    def __init__(self, engine, profiles):
        self.engine = engine
        self.profiles = profiles

    def configuration(self, role: str) -> dict:
        if role not in ("stimulation", "actuation"):
            raise PluginSchemaError("Unsupported external node role")
        state, snapshot = self.engine.node_configuration_snapshot(
            lambda: self.profiles.materialize(ActivationSet(
                {role: self.profiles.repository.get_module(role)}, self.profiles.repository.get_engine())),
            module=role,
        )
        module = role.upper()
        if snapshot.get(f"{module}.auto_spawn", True):
            raise ConfigProfileConflictError(f"{module.title()} is managed by the server")
        return {"state": state, "configuration": project_node_configuration(snapshot, role).to_dict()}
