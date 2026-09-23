"""Plans classify actual changes and pin inputs through concurrent saved edits."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: {})
    selections = []
    for slot, kind, alias in (
        ("acquisition.source", "acquisition", "file_reader"),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ):
        definition = profiles.catalog.legacy(kind, alias)
        profile = profiles.create(kind, definition.selection, alias, {})
        selections.append({"slot": slot, "profile_id": profile.profile_id})
    snapshot = profiles.propose(selections)
    profiles.accept(snapshot, expected_revision=None)
    engine = SimpleNamespace(is_engine_ready=True, applied_revision=snapshot.revision,
                             generation="generation-1", applied_snapshot=profiles.materialize(snapshot))
    return RuntimeConfigurationService(profiles, engine), selections, snapshot


def edit(runtime, slot, values):
    service, selections, _ = runtime
    profile = service.profiles.repository.get_profile(next(s["profile_id"] for s in selections if s["slot"] == slot))
    service.profiles.update(profile.profile_id, name=profile.name,
                            parameters={**profile.parameters, **values}, expected_revision=profile.revision)


def test_plan_is_pinned_and_does_not_apply_saved_values(runtime):
    service, selections, before = runtime
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "trial_boundary"
    assert plan.session_effect == "preserve"
    assert not plan.errors
    edit(runtime, "processing.decoder", {"window_duration": 1.5})
    pinned = service.resolve_plan(plan.plan_id).snapshot
    assert pinned.bindings[1].parameters["window_duration"] == 2.5
    assert service.profiles.repository.get_activation() == before
    assert service.status()["applied_revision"] == before.revision


def test_source_change_requires_continuation(runtime):
    service, selections, before = runtime
    edit(runtime, "acquisition.source", {"sample_rate": 500})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "rebuild"
    assert plan.session_effect == "continuation"


def test_effector_selection_uses_module_boundary(runtime):
    service, selections, before = runtime
    definition = service.profiles.catalog.legacy("effector", "logger")
    profile = service.profiles.create("effector", definition.selection, "Logger", {})
    selections.append({"slot": "actuation.effector", "profile_id": profile.profile_id})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "module"


def test_idle_plan_does_not_claim_applied_revision(runtime):
    service, selections, before = runtime
    service.engine.is_engine_ready = False
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "select_on_start"
    assert plan.applied_revision is None
    assert service.status()["applied_revision"] is None


def test_no_semantic_change_does_not_restart(runtime):
    service, selections, before = runtime
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "no_change"
    assert plan.target_revision == before.revision


@pytest.mark.parametrize("changed", ["generation", "applied_revision", "is_engine_ready"])
def test_stale_runtime_plan_cannot_execute(runtime, changed):
    service, selections, before = runtime
    plan = service.plan(selections, expected_revision=before.revision)
    setattr(service.engine, changed, False if changed == "is_engine_ready" else "changed")
    with pytest.raises(ProfileRevisionConflict, match="changed"):
        service.resolve_plan(plan.plan_id)


def test_plan_compare_and_swap_and_expiry(runtime):
    service, selections, before = runtime
    with pytest.raises(ProfileRevisionConflict):
        service.plan(selections, expected_revision="stale")
    plan = service.plan(selections, expected_revision=before.revision)
    service._plans[plan.plan_id] = replace(service._plans[plan.plan_id], created_at=-1000)
    with pytest.raises(ProfileRevisionConflict, match="expired"):
        service.resolve_plan(plan.plan_id)


def test_missing_required_slot_is_reviewable_error(runtime):
    service, selections, before = runtime
    plan = service.plan(selections[1:], expected_revision=before.revision)
    assert any("acquisition.source" in error for error in plan.errors)


def test_external_peer_rejected_before_rebuild(runtime):
    service, selections, before = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    edit(runtime, "acquisition.source", {"sample_rate": 500})
    plan = service.plan(selections, expected_revision=before.revision)
    assert any("externally managed" in error for error in plan.errors)


def test_filter_order_is_a_real_trial_boundary_change(runtime):
    service, selections, before = runtime
    definition = service.profiles.catalog.legacy("filter", "BASELINE_CORRECTION")
    profile = service.profiles.create("filter", definition.selection, "Baseline", {})
    selections += [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id} for _ in range(2)]
    selected = service.profiles.propose(selections)
    service.profiles.accept(selected, expected_revision=before.revision)
    service.engine.applied_revision = selected.revision
    reordered = selections[:-2] + list(reversed(selections[-2:]))
    plan = service.plan(reordered, expected_revision=selected.revision)
    assert plan.strategy == "trial_boundary"
    assert plan.target_revision != selected.revision
