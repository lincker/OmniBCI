"""Explicit package lifecycle and checked profile copies for upgrades or rollback."""
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from omnibci.application.plugin_packages import (
    ManagedPluginPackageStore, PackageReference, PluginPackageConflictError, PluginPackageError,
)
from omnibci.application.plugin_profiles import PluginProfile, PluginSelection, ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.services.plugin_installation_service import PluginInstallationService
from omnibci.application.services.plugin_profile_service import PluginProfileService, _validate_manual_montage
from omnibci.domain.montage import ProcessingInput, SourceMetadata


@dataclass(frozen=True, slots=True)
class ProfileVersionProposal:
    source_profile_id: str
    source_revision: int
    name: str
    kind: str
    selection: PluginSelection
    parameters: dict
    configuration_version: int
    target_enabled: bool
    manual_montage: SourceMetadata | None = None
    processing_input: ProcessingInput | None = None


class PluginPackageService(PluginInstallationService):
    def __init__(self, store: ManagedPluginPackageStore, profiles: PluginProfileService, *,
                 runtime_references: Callable[[], Iterable[PackageReference]], admission_guard):
        super().__init__(store, admission_guard=admission_guard)
        self.profiles = profiles
        self.runtime_references = runtime_references

    def references(self) -> tuple[PackageReference, ...]:
        references = [PackageReference("profile", item.profile_id, item.selection)
                      for item in self.profiles.repository.list_profiles()]
        accepted = self.profiles.repository.get_activation()
        if accepted is not None:
            references.extend(PackageReference("accepted", item.slot, item.selection)
                              for item in accepted.bindings)
        references.extend(self.runtime_references())
        if any(not isinstance(item, PackageReference) for item in references):
            raise PluginPackageError("Runtime references must be explicit PackageReference values")
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _matching(references, package_id, version):
        # An unversioned reference cannot establish that a managed version is unused.
        return tuple(item for item in references if item.selection.package_id == package_id
                     and item.selection.version in {None, version})

    def list(self):
        references = self.references()
        return [replace(item, references=self._matching(references, item.package_id, item.version))
                for item in self.store.list_packages()]

    def _require_unused(self, package_id, version):
        references = self._matching(self.references(), package_id, version)
        if references:
            descriptions = ", ".join(f"{item.scope}:{item.identifier}" for item in references)
            raise PluginPackageConflictError(f"Package version is still referenced by {descriptions}")

    def set_enabled(self, package_id: str, version: str, enabled: bool):
        with self.admission_guard():
            if not enabled:
                self._require_unused(package_id, version)
            return self.store.set_enabled(package_id, version, enabled)

    def remove(self, package_id: str, version: str):
        with self.admission_guard():
            self._require_unused(package_id, version)
            self.store.remove(package_id, version)

    def propose_profile(self, source_profile_id: str, target_version: str, *,
                        name: str | None = None, parameters: dict | None = None):
        """Validate a copy against the target schema; preserve all old values by default."""
        source = self.profiles.repository.get_profile(source_profile_id)
        selection = replace(source.selection, version=target_version)
        if selection == source.selection:
            raise PluginPackageError("Choose a different exact package version")
        package = self.store.get_package(selection.package_id, target_version)
        if package.error or not package.available:
            raise PluginPackageError(package.error or "Target package is unavailable")
        definition = self.store.configuration(source.kind, selection)
        if not definition.available or definition.schema is None:
            raise PluginSchemaError("Target contribution requires an available configuration schema")
        values = validate_parameters(definition.schema, source.parameters if parameters is None else parameters)
        _validate_manual_montage(source.kind, definition, values, source.manual_montage)
        # Reuse profile validation for names and portable JSON values without saving anything.
        draft = PluginProfile(source.profile_id, source.name if name is None else name, source.kind, selection,
                              values, definition.configuration_version, manual_montage=source.manual_montage,
                              processing_input=source.processing_input)
        return ProfileVersionProposal(source.profile_id, source.revision, draft.name, draft.kind,
                                      selection, draft.parameters, draft.configuration_version, package.enabled,
                                      draft.manual_montage, draft.processing_input)

    def create_version_profile(self, source_profile_id: str, target_version: str, *,
                               expected_source_revision: int, name: str | None = None,
                               parameters: dict | None = None):
        """Save a new profile after explicit review; accepted/running bindings stay pinned."""
        with self.admission_guard():
            proposal = self.propose_profile(source_profile_id, target_version, name=name, parameters=parameters)
            if proposal.source_revision != expected_source_revision:
                raise ProfileRevisionConflict("Source profile changed; preview the version change again")
            if not proposal.target_enabled:
                raise PluginPackageConflictError("Enable the target package before creating its profile")
            return self.profiles.create(proposal.kind, proposal.selection, proposal.name, proposal.parameters,
                                        manual_montage=proposal.manual_montage,
                                        processing_input=proposal.processing_input)
