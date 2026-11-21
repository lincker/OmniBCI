"""Controller ownership lasts until final server settlement, including resume."""
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from omnibci.application.plugin_packages import PluginPackageConflictError
from omnibci.bootstrap import build_container
from omnibci.presentation.main import create_app


@pytest.mark.asyncio
async def test_bootstrap_rejects_second_controller_before_runtime_construction():
    first = await build_container()
    try:
        with pytest.raises(PluginPackageConflictError, match='owner'):
            await build_container()
    finally:
        await first.engine_service.shutdown()
        first.package_owner.close()


def test_shutdown_prepare_and_resume_keep_ownership_until_lifespan_exits(tmp_path):
    from tests.infrastructure.test_plugin_store_ownership import claim
    from omnibci.infrastructure.config_paths import config_root
    app = create_app()
    with TestClient(app) as client:
        assert client.portal.call(app.state.shutdown_service.prepare) == []
        with pytest.raises(PluginPackageConflictError, match='owner'):
            claim(tmp_path / 'plugins', configuration_root=config_root())
        app.state.shutdown_service.resume()
        with pytest.raises(PluginPackageConflictError, match='owner'):
            claim(tmp_path / 'plugins', configuration_root=config_root())
    next_owner = claim(tmp_path / 'plugins', configuration_root=config_root())
    next_owner.close()


def test_failed_final_shutdown_does_not_admit_another_owner(tmp_path, monkeypatch):
    from tests.infrastructure.test_plugin_store_ownership import claim
    from omnibci.infrastructure.config_paths import config_root
    app = create_app()
    with pytest.raises(RuntimeError, match='resources still held'):
        with TestClient(app):
            monkeypatch.setattr(app.state.engine_service, 'shutdown',
                                AsyncMock(side_effect=RuntimeError('resources still held')))
    with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
        claim(tmp_path / 'plugins', configuration_root=config_root())


def test_presentation_startup_failure_still_settles_the_acquired_owner(tmp_path, monkeypatch):
    from tests.infrastructure.test_plugin_store_ownership import claim
    from omnibci.infrastructure.config_paths import config_root
    from omnibci.presentation import main
    monkeypatch.setattr(main, 'register_engine_listeners',
                        Mock(side_effect=RuntimeError('listener registration failed')))
    with pytest.raises(RuntimeError, match='listener registration failed'):
        with TestClient(create_app()):
            pytest.fail('Startup should fail')
    owner = claim(tmp_path / 'plugins', configuration_root=config_root())
    owner.close()
