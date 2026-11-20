"""One local authority protects all users of a plugin store."""
import subprocess
import sys

import pytest

from omnibci.application.plugin_packages import PluginPackageConflictError, PluginPackageStorageError


def claim(root, *, role=None, configuration_root=None):
    from omnibci.infrastructure.plugins.store_ownership import PluginStoreOwner
    return PluginStoreOwner(root, role=role,
                            configuration_root=configuration_root or root.parent / 'configuration')


def test_second_controller_cannot_bypass_the_first_reference_view(tmp_path):
    root = tmp_path / 'plugins'
    owner = claim(root)
    try:
        with pytest.raises(PluginPackageConflictError, match='owner'):
            claim(root)
        with owner:
            pass
    finally:
        owner.close()
    replacement = claim(root)
    replacement.close()
    with pytest.raises(PluginPackageConflictError, match='closed'):
        with owner:
            pytest.fail('Closed authority admitted a mutation')


def test_distinct_nodes_can_read_together_but_exclude_inventory_mutation(tmp_path):
    root = tmp_path / 'plugins'
    stimulation = claim(root, role='stimulation')
    actuation = claim(root, role='actuation')
    controller = None
    try:
        controller = claim(root)
        with controller:
            pass
        with pytest.raises(PluginPackageConflictError, match='owner'):
            with controller.mutation():
                pytest.fail('An independent node still owns package resources')
        with pytest.raises(PluginPackageConflictError, match='owner'):
            claim(root, role='stimulation')
    finally:
        stimulation.close()
        actuation.close()
        if controller is not None:
            controller.close()
    controller = claim(root)
    try:
        for role in ('stimulation', 'actuation'):
            node = claim(root, role=role)
            node.close()
        with controller.mutation():
            for role in ('stimulation', 'actuation'):
                with pytest.raises(PluginPackageConflictError, match='owner'):
                    claim(root, role=role)
    finally:
        controller.close()


@pytest.mark.parametrize('role', [None, 'stimulation', 'actuation'])
def test_uncertain_exit_keeps_store_blocked_after_process_lock_is_released(tmp_path, role):
    root = tmp_path / 'plugins'
    owner = claim(root, role=role)
    owner.abandon()
    if role is None:
        with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
            claim(root)
    else:
        controller = claim(root)
        try:
            with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
                with controller.mutation():
                    pytest.fail('Unknown resource ownership admitted a mutation')
        finally:
            controller.close()
    with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
        claim(root, role=role)


def test_clean_restart_cannot_hide_saved_references_in_another_configuration_root(tmp_path):
    root = tmp_path / 'plugins'
    owner = claim(root)
    owner.close()
    with pytest.raises(PluginPackageConflictError, match='configuration directory'):
        claim(root, configuration_root=tmp_path / 'other-config')


@pytest.mark.parametrize('path', ['state', 'state/.controller.lock', 'state/.controller.active'])
def test_ownership_state_rejects_symlinks(tmp_path, path):
    root = tmp_path / 'plugins'
    link = root / path
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'target'
    target.mkdir() if path == 'state' else target.write_text('')
    try:
        link.symlink_to(target, target_is_directory=path == 'state')
    except OSError:
        pytest.skip('Symlink creation is unavailable')
    with pytest.raises(PluginPackageStorageError, match='symlink'):
        claim(root)


@pytest.mark.integration
@pytest.mark.parametrize('role', [None, 'actuation'])
def test_cross_process_owner_exclusion_and_crash_retention(tmp_path, role):
    root = tmp_path / 'plugins'
    script = '''import os, sys
from pathlib import Path
from omnibci.infrastructure.plugins.store_ownership import PluginStoreOwner
owner = PluginStoreOwner(Path(sys.argv[1]), configuration_root=Path(sys.argv[2]), role=sys.argv[3] or None)
os._exit(0)
'''
    command = [sys.executable, '-B', '-c', script, str(root), str(tmp_path / 'configuration'), role or '']
    owner = claim(root, role=role)
    try:
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert blocked.returncode != 0 and 'owner' in blocked.stderr
    finally:
        owner.close()
    crashed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert crashed.returncode == 0, crashed.stderr
    if role is None:
        with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
            claim(root)
    else:
        controller = claim(root)
        try:
            with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
                with controller.mutation():
                    pytest.fail('Process death was mistaken for node settlement')
        finally:
            controller.close()
