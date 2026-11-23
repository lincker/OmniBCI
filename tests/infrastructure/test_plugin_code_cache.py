"""One locally selected artifact owns code; each binding owns its Instance."""
from concurrent.futures import ThreadPoolExecutor
import os
import py_compile
import sys

import pytest

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugin_management.catalog import PluginRoot
from tests.infrastructure.test_plugin_contributions import _catalog, _contribution, _write_package


def package(tmp_path, *, entrypoint='driver:Driver', body='', constructor='self.parameters = parameters', extra=None):
    root = _write_package(tmp_path, contributions=[
        _contribution('first', 'effector', entrypoint=entrypoint),
        _contribution('second', 'effector', entrypoint=entrypoint),
    ])
    marker = tmp_path / 'imports.txt'
    code = f'''from pathlib import Path
from omnibci.sdk import Effector
marker = Path({str(marker)!r})
marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')
{body}
class Driver(Effector):
    def __init__(self, *, parameters, resource_root):
        {constructor}
    def setup(self): pass
    def execute(self, command): pass
    def quiesce(self): pass
    def teardown(self): pass
'''
    module = root.joinpath(*entrypoint.split(':')[0].split('.')).with_suffix('.py')
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(code)
    for name, contents in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    catalog = _catalog(PluginRoot(root, 'user', 0), category='effector')
    descriptors = catalog.list_contributions('effector')
    return root, descriptors, marker


def construct(descriptor, **parameters):
    return PluginLoader.instantiate(descriptor, Effector, parameters=parameters)


def test_repeated_binding_shares_code_but_has_independent_instances_and_parameters(tmp_path):
    _, (first, _), marker = package(tmp_path)
    a, b = construct(first, values=[1]), construct(first, values=[2])
    assert a is not b and type(a) is type(b)
    a.parameters['values'].append(9)
    assert b.parameters == {'values': [2]}
    assert marker.read_text() == 'x'


def test_contributions_in_the_same_package_share_one_namespace(tmp_path):
    _, (first, second), marker = package(tmp_path)
    a, b = construct(first), construct(second)
    assert type(a) is type(b)
    assert marker.read_text() == 'x'


def test_nested_packages_initialize_normally_once_below_the_synthetic_root(tmp_path):
    root, (first, second), marker = package(
        tmp_path, entrypoint='impl.nested.driver:Driver', body='from . import VALUE',
        constructor='self.value = VALUE', extra={
            '__init__.py': "raise AssertionError('Synthetic root must not execute this')\n",
            'impl/__init__.py': 'VALUE = 41\n',
            'impl/nested/__init__.py': 'from .. import VALUE\nVALUE += 1\n',
        },
    )
    before = sys.path.copy()
    a, b = construct(first), construct(second)
    assert a.value == b.value == 42
    assert type(a) is type(b) and marker.read_text() == 'x'
    assert sys.path == before
    assert not list(root.rglob('*.pyc'))


def test_concurrent_construction_never_executes_the_entry_module_twice(tmp_path):
    _, (descriptor, _), marker = package(tmp_path, body='import time\ntime.sleep(0.02)')
    with ThreadPoolExecutor(max_workers=8) as executor:
        instances = list(executor.map(lambda value: construct(descriptor, value=value), range(8)))
    assert len({id(instance) for instance in instances}) == 8
    assert len({type(instance) for instance in instances}) == 1
    assert marker.read_text() == 'x'
    assert [item.parameters['value'] for item in instances] == list(range(8))


def test_constructor_failure_does_not_evict_successfully_imported_code(tmp_path):
    _, (descriptor, _), marker = package(tmp_path, constructor=(
        "self.parameters = parameters\n        if parameters.get('fail'): raise ValueError('construction failed')"
    ))
    a = construct(descriptor)
    with pytest.raises(PluginLoadError, match='construction failed'):
        construct(descriptor, fail=True)
    b = construct(descriptor)
    assert type(a) is type(b) and marker.read_text() == 'x'


@pytest.mark.parametrize('exception', ['RuntimeError("import failed")', 'SystemExit(7)'])
def test_failed_import_leaves_no_partial_artifact_then_can_retry(tmp_path, exception):
    allowed = tmp_path / 'allow.txt'
    _, (descriptor, _), marker = package(tmp_path, body=f'if not Path({str(allowed)!r}).exists(): raise {exception}')
    prefix = PluginLoader._module_prefix(descriptor)
    with pytest.raises(PluginLoadError if exception.startswith('RuntimeError') else SystemExit):
        construct(descriptor)
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)
    allowed.write_text('ready')
    instance = construct(descriptor)
    assert instance is not None and marker.read_text() == 'xx'


def test_changed_artifact_uses_distinct_code_without_rewriting_old_instances(tmp_path):
    root, (descriptor, _), _ = package(tmp_path, body='VERSION = 1', constructor='self.version = VERSION')
    first = construct(descriptor)
    path = root / 'driver.py'
    path.write_text(path.read_text().replace('VERSION = 1', 'VERSION = 2'))
    second = construct(descriptor)
    assert first.version == 1 and second.version == 2
    assert type(first).__module__ != type(second).__module__


@pytest.mark.parametrize('entrypoint_cache', [False, True])
def test_source_bytes_take_precedence_over_stale_bytecode(tmp_path, entrypoint_cache):
    body = 'VERSION = 1' if entrypoint_cache else 'from .helper import VERSION'
    root, (descriptor, _), _ = package(tmp_path, body=body, constructor='self.version = VERSION',
                                    extra={} if entrypoint_cache else {'helper.py': 'VERSION = 1\n'})
    path = root / ('driver.py' if entrypoint_cache else 'helper.py')
    original = path.stat()
    py_compile.compile(str(path), doraise=True)
    path.write_text(path.read_text().replace('VERSION = 1', 'VERSION = 2'))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert construct(descriptor).version == 2


def test_non_code_metadata_changes_do_not_evict_artifact_code(tmp_path):
    root, (descriptor, _), marker = package(tmp_path)
    first = construct(descriptor)
    (root / '.omnibci-install.json').write_text('{"origin":"local"}')
    (root / '__pycache__').mkdir(exist_ok=True)
    (root / '__pycache__/irrelevant.pyc').write_bytes(b'ignored bytecode')
    second = construct(descriptor)
    assert type(first) is type(second) and marker.read_text() == 'x'


def test_resource_changes_are_part_of_artifact_identity(tmp_path):
    root, (descriptor, _), _ = package(tmp_path, extra={'resource/data.txt': 'first'})
    first = construct(descriptor)
    (root / 'resource/data.txt').write_text('second')
    assert type(first).__module__ != type(construct(descriptor)).__module__


def test_failed_sibling_import_does_not_evict_running_contribution_code(tmp_path):
    from dataclasses import replace
    from omnibci.infrastructure.plugin_management.descriptors import PluginEntrypoint

    _, (descriptor, second), marker = package(tmp_path, extra={'broken.py': "raise RuntimeError('broken sibling')\n"})
    first = construct(descriptor)
    sibling = replace(second, entrypoint=PluginEntrypoint.parse('broken:Driver'))
    prefix = type(first).__module__.rsplit('.', 1)[0]
    with pytest.raises(PluginLoadError, match='broken sibling'):
        construct(sibling)
    assert prefix + '.broken' not in sys.modules
    assert type(construct(descriptor)) is type(first)
    assert marker.read_text() == 'x'


def test_bytecode_only_helpers_cannot_bypass_source_artifact_identity(tmp_path):
    root, (descriptor, _), _ = package(tmp_path, body='from .helper import VERSION',
                                    extra={'helper.py': 'VERSION = 1\n'})
    path = root / 'helper.py'
    py_compile.compile(str(path), cfile=str(root / 'helper.pyc'), doraise=True)
    path.unlink()
    with pytest.raises(PluginLoadError, match='bytecode-only'):
        construct(descriptor)
