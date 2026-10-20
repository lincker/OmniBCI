"""Archive effective runtime inputs before workers may observe a revision."""
from copy import deepcopy

from omnibci.application.activation_settings import PLUGIN_SETTING_KEYS, RUNTIME_HOST_SETTING_KEYS
from omnibci.application.configuration_archive import ConfigurationArchive
from omnibci.application.plugin_profiles import PluginSelection
from omnibci.domain.channel_mapping import ProcessingInput


_BINDING_ALIASES = frozenset({
    "ALGORITHM.decoder", "ALGORITHM.epoch_detector", "ALGORITHM.filters",
    "STIMULATION.paradigm", "ACQUISITION.trigger", "ACTUATION.effector",
})


def _archive_parameters(schema, value, redacted, path="", *, redactions=()):
    if schema.get("x-scope") == "secret" or (path or "$") in redactions:
        redacted.append(path or "$")
        return "[redacted]"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        return {key: _archive_parameters(properties.get(key, {}), item, redacted,
                                         f"{path}.{key}" if path else key, redactions=redactions)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_archive_parameters(schema.get("items", {}), item, redacted, f"{path}[{index}]",
                                    redactions=redactions)
                for index, item in enumerate(value)]
    return deepcopy(value)


def _canonical_snapshot(snapshot):
    result = deepcopy(snapshot)
    result["settings"] = {key: value for key, value in result["settings"].items()
                          if key not in _BINDING_ALIASES}
    for binding in result["bindings"]:
        if binding.get("slot") == "processing.decoder" and "processing_input" not in binding:
            binding["processing_input"] = ProcessingInput().to_dict()
    return result


def configuration_snapshot(settings: dict, *, catalog=None, previous=None) -> dict:
    """Select reproducible inputs, excluding unrelated host and process state.

    Ordered bindings carry exact plugin versions, materialized parameters and
    decoder channel mappings. Generation belongs to an application attempt,
    because the same revision can run in multiple engine processes. Editable
    profile references do not define revision identity. Schema-declared secrets
    are redacted when the composition root supplies the configuration catalog.
    """
    keys = (PLUGIN_SETTING_KEYS | RUNTIME_HOST_SETTING_KEYS) - _BINDING_ALIASES
    runtime = {key: deepcopy(value) for key, value in settings.items() if key in keys}
    prior_bindings = {binding["slot"]: binding for binding in previous["bindings"]} if previous else {}
    bindings = []
    for binding in settings.get("PLUGINS.bindings", []):
        archived = {key: deepcopy(value) for key, value in binding.items()
                    if key not in {"profile_id", "profile_revision"}}
        if catalog is not None:
            prior = prior_bindings.get(binding["slot"])
            retained = prior is not None and all(prior.get(key) == binding.get(key)
                for key in ("kind", "selection", "configuration_version"))
            if retained:
                # Redaction is part of this immutable revision, independent of
                # whether its dormant plugin remains installed.
                schema = {}
            else:
                try:
                    schema = catalog.get(binding["kind"], PluginSelection.from_dict(binding["selection"])).schema
                except (ValueError, KeyError, RuntimeError):
                    schema = None
            redacted = []
            archived["parameters"] = _archive_parameters(
                schema if schema is not None else {"x-scope": "secret"},
                binding.get("parameters", {}), redacted,
                redactions=prior.get("redacted_parameters", ()) if retained else (),
            )
            if redacted:
                archived["redacted_parameters"] = redacted
                if binding["kind"] == "acquisition":
                    for key in binding.get("parameters", {}):
                        mirror = f"ACQUISITION.{key}"
                        if mirror in runtime:
                            parameters = archived["parameters"]
                            runtime[mirror] = parameters[key] if isinstance(parameters, dict) else parameters
        bindings.append(archived)
    return {
        "schema_version": 1,
        "settings": runtime,
        "bindings": bindings,
    }


class ConfigurationArchiveService:
    def __init__(self, repository: ConfigurationArchive, *, catalog=None) -> None:
        self.repository = repository
        self.catalog = catalog

    def stage(self, settings: dict, *, context: str, session_id: str | None = None) -> str:
        revision = self._revision(settings)
        archived = self.repository.get(revision)
        previous = archived["snapshot"] if archived else None
        snapshot = configuration_snapshot(settings, catalog=self.catalog, previous=previous)
        if previous is not None and _canonical_snapshot(previous) == _canonical_snapshot(snapshot):
            snapshot = previous
        return self.repository.stage(
            revision, snapshot,
            generation=settings.get("CONFIG.generation"), context=context, session_id=session_id,
        )

    def confirm(self, attempt_id: str, settings: dict, *, receipt: dict) -> None:
        revision = self._revision(settings)
        generation = settings.get("CONFIG.generation")
        if not isinstance(generation, str) or not generation:
            raise ValueError("An acknowledged configuration requires an engine generation")
        archived = self.repository.get(revision)
        if archived is None or _canonical_snapshot(archived["snapshot"]) != _canonical_snapshot(
                configuration_snapshot(settings, catalog=self.catalog, previous=archived["snapshot"])):
            raise ValueError("Acknowledged configuration differs from the archived candidate")
        self.repository.confirm(attempt_id, revision, generation=generation, receipt=receipt)

    def fail(self, attempt_id: str, error: str) -> None:
        self.repository.fail(attempt_id, error)

    def link_session(self, session_id: str, config_revision: str) -> None:
        self.repository.link_session(session_id, config_revision)

    def get(self, revision: str) -> dict | None:
        return self.repository.get(revision)

    def get_for_session(self, session_id: str) -> list[dict]:
        return self.repository.get_for_session(session_id)

    @staticmethod
    def _revision(settings: dict) -> str:
        revision = settings.get("CONFIG.revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError("Runtime configuration requires a revision")
        return revision
