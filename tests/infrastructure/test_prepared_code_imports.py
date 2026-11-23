"""The loader compiles the source bytes admitted for this artifact."""
import zipfile

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugin_management import preparation
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
from tests.infrastructure.test_package_store import package_archive


def prepared(tmp_path, *, helper=False):
    source = '''from omnibci.sdk import Effector
class Driver(Effector):
    def __init__(self, *, parameters, resource_root): pass
    def setup(self): pass
    def execute(self, command):
        from .helper import VALUE
        self.value = VALUE
    def quiesce(self): pass
    def teardown(self): pass
'''
    archive = package_archive(tmp_path, code=source)
    if helper:
        with zipfile.ZipFile(archive, 'a') as writer:
            writer.writestr('helper.py', 'VALUE = 1\n')
    store = ManagedPluginPackageStore(tmp_path / 'plugins')
    package = store.install_archive(archive).package
    store.set_enabled(package.package_id, package.version, True)
    binding = ProfileBinding('actuation.effector', 'effector', None,
                             package.contributions[0].selection, {'gain': 1.0})
    snapshot = preparation.PluginArtifactPreparer(store).prepare_snapshot(
        {'PLUGINS.bindings': [binding.to_dict()]}, ('actuation',),
    )
    return snapshot, store.packages_dir / 'acme.device/1.0.0'


def test_code_change_between_handoff_verification_and_import_is_rejected(tmp_path, monkeypatch):
    snapshot, root = prepared(tmp_path)
    marker = tmp_path / 'executed'
    read = preparation.read_prepared_plugin
    def change_after_read(*args):
        descriptor = read(*args)
        (root / 'driver.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n')
        return descriptor
    monkeypatch.setattr(preparation, 'read_prepared_plugin', change_after_read)
    with pytest.raises(PluginLoadError, match='content.*preparation'):
        load_configured_plugin(snapshot, 'actuation.effector', 'effector', Effector)
    assert not marker.exists()


@pytest.mark.parametrize('existed', [True, False])
def test_lazy_relative_import_rejects_changed_or_injected_source(tmp_path, existed):
    snapshot, root = prepared(tmp_path, helper=existed)
    instance = load_configured_plugin(snapshot, 'actuation.effector', 'effector', Effector)
    (root / 'helper.py').write_text('VALUE = 999\n')
    with pytest.raises(PluginLoadError, match='source.*preparation'):
        instance.execute({})
    assert not hasattr(instance, 'value')
