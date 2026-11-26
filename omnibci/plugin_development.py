"""Explicit source snapshots launched through the normal application composition."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from uuid import uuid4

import tomlkit

from omnibci.application.commands.engine_commands import StartEngineCommand
from omnibci.application.configuration.definitions import MODULES, validate_server_settings
from omnibci.application.configuration.models import MODULE_KINDS
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.packages import PluginPackageConflictError
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugin_management.installer import PackageInstallError
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.project_tools import package_project
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.infrastructure.storage import strict_json_loads
from omnibci.presentation.schemas.runtime_config import ModuleConfigurationRequest


def _read_configuration(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Development configuration exceeds the size limit")
    document = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document or set(document) - set(MODULES):
        raise ValueError("Development configuration must map module names to current configurations")
    modules = {}
    for module, raw in document.items():
        configuration = ModuleConfigurationRequest.model_validate(raw).model_dump()
        if configuration["expected_revision"] is not None:
            raise ValueError("A fresh development run requires expected_revision=null")
        if configuration["settings"].get(f"{module.upper()}.auto_spawn", True) is not True:
            raise ValueError("Development requires a managed local module host")
        modules[module] = configuration
    if "processing" in modules and "acquisition" not in modules:
        raise ValueError("Processing development requires an explicit Acquisition companion configuration")
    return modules


def _prepare_run(project, configuration, contribution, companions, workspace, *, command_timeout=None):
    if command_timeout is not None:
        command_timeout = validate_server_settings({
            "NEUROENGINE.command_timeout_s": command_timeout,
        })["NEUROENGINE.command_timeout_s"]
    modules = _read_configuration(configuration)
    workspace.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=workspace)).resolve()
    try:
        store = ManagedPluginPackageStore(run / "plugins")
        with tempfile.TemporaryDirectory(prefix="omnibci-development-") as temporary:
            archive = Path(temporary) / "plugin.zip"
            summary = package_project(project, archive)
            installed = store.installer.install(archive, development=True)
        selected = next((item for item in installed.contributions if item.contribution_id == contribution), None)
        if selected is None:
            raise PackageInstallError("Choose a contribution declared by this project")
        module = next(name for name, kinds in MODULE_KINDS.items() if selected.category in kinds)
        store.set_enabled(installed.package_id, installed.version, True)
        for archive in companions:
            companion = store.install_archive(Path(archive)).package
            store.set_enabled(companion.package_id, companion.version, True)
        repository = TomlConfigurationRepository(run / "configuration")
        profiles = ConfigurationService(repository, CatalogConfigurationAdapter(store.catalog))
        selected_binding = False
        for name, current in modules.items():
            candidate = profiles.propose(name, current["bindings"], settings=current["settings"])
            profiles.validate_module(candidate, active_kinds=MODULE_KINDS[name])
            selected_binding |= name == module and any(binding.selection == selected.selection for binding in candidate.bindings)
        if not selected_binding:
            raise ValueError("Development configuration must select the requested contribution in its owning module")
        repository.root.mkdir(parents=True, exist_ok=True)
        settings = {"LOGGING": {"file_path": str(run / "host.log")}}
        if command_timeout is not None:
            settings["NEUROENGINE"] = {"command_timeout_s": command_timeout}
        (repository.root / "settings.toml").write_text(tomlkit.dumps(settings), encoding="utf-8")
        (run / "run.json").write_text(json.dumps({
            "module": module, "selection": selected.selection.to_dict(), "sha256": summary["sha256"],
            "modules": modules,
        }, sort_keys=True, allow_nan=False), encoding="utf-8")
        return run
    except BaseException:
        shutil.rmtree(run)
        raise


def _child_environment(run):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OMNIBCI_")}
    environment.update(
        OMNIBCI_PLUGIN_DIR=str(run / "plugins"), OMNIBCI_CONFIG_DIR=str(run / "configuration"),
        OMNIBCI_MARKET_CACHE_DIR=str(run / "market-cache"), MPLCONFIGDIR=str(run / "matplotlib"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def _wait_host(process):
    stopping = False
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            if not stopping:
                stopping = True
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            print("Waiting for development host resource cleanup...", file=sys.stderr)


def develop_project(project: Path, configuration: Path, contribution: str, *, companions=(), port=8002,
                    command_timeout=None) -> int:
    """Launch an isolated plugin snapshot in a fresh host process."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Development API port must be between 1 and 65535")
    project = Path(project).resolve()
    if not project.is_dir() or not (project / "manifest.toml").is_file():
        raise PackageInstallError("Select the plugin directory containing manifest.toml")
    workspace = project / ".omnibci-dev"
    if workspace.is_symlink():
        raise PackageInstallError("Development workspace must not be a symlink")
    run, launch_attempted = None, False
    try:
        run = _prepare_run(project, configuration, contribution, companions, workspace, command_timeout=command_timeout)
        record = json.loads((run / "run.json").read_text(encoding="utf-8"))
        print(json.dumps({"run_directory": str(run), "sha256": record["sha256"],
                          "selection": record["selection"], "url": f"http://127.0.0.1:{port}"}), flush=True)
        launch_attempted = True
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-m", "omnibci.plugin_development", str(run), str(port)],
            cwd=run, env=_child_environment(run), start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        result = _wait_host(process)
        if result != 0:
            raise PluginPackageConflictError(f"Development shutdown is unconfirmed; retained artifacts at {run}")
        shutil.rmtree(run)
        return result
    except BaseException:
        if not launch_attempted and run is not None:
            shutil.rmtree(run)
        raise


async def _start_modules(container, modules):
    await container.engine_service.handle_start_engine(StartEngineCommand())
    for module in MODULES:
        if module not in modules:
            continue
        result = await container.runtime_configuration.execute_current(
            module, "connect" if module == "acquisition" else "start",
            operation_id=str(uuid4()), configuration=modules[module],
        )
        if result["status"] != "succeeded":
            raise RuntimeError(f"Development {module} startup failed: {result['error']}")


def _create_app(run):
    from omnibci.presentation.main import create_app, lifespan

    for key, directory in (("OMNIBCI_PLUGIN_DIR", "plugins"), ("OMNIBCI_CONFIG_DIR", "configuration")):
        if os.environ.get(key) != str(run / directory):
            raise ValueError("Development host requires its isolated startup environment")
    record = json.loads((run / "run.json").read_text(encoding="utf-8"))
    app = create_app()
    app.state.allow_process_termination = False

    @asynccontextmanager
    async def development_lifespan(application):
        async with lifespan(application):
            await _start_modules(application.state.container, record["modules"])
            (run / "ready.json").write_text(json.dumps({"module": record["module"], "pid": os.getpid()}), encoding="utf-8")
            yield

    app.router.lifespan_context = development_lifespan
    return app


def main():
    import multiprocessing
    import uvicorn

    multiprocessing.freeze_support()
    run, port = Path(sys.argv[1]).resolve(), int(sys.argv[2])
    app = _create_app(run)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0 if server.started and app.state.shutdown_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
