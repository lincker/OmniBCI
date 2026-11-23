"""Standalone configuration retrieval stays outside the application lifetime."""
import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from omnibci.application.node_configuration import NodeConfiguration
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.configuration.models import ProfileBinding
from omnibci.infrastructure import node_client


def configuration(*, generation="run-1"):
    return NodeConfiguration(
        "actuation", {'actuation': "a" * 64}, "c" * 64, generation,
        {},
        (ProfileBinding("actuation.effector", "effector", None,
                        PluginSelection("test.effector", "logger", "1.0.0"), {}),),
        operation_id="prepare-1" if generation is not None else None,
    )


def install_response(monkeypatch, content):
    if not isinstance(content, bytes):
        content = json.dumps(content).encode()
    opener = Mock(return_value=io.BytesIO(content))
    monkeypatch.setattr(node_client, "urlopen", opener)
    return opener


def response(*, state="running", generation="run-1"):
    return {"success": True, "data": {"state": state,
            "configuration": configuration(generation=generation).to_dict()}}


def test_fetch_running_configuration_is_bounded_and_role_scoped(monkeypatch):
    opener = install_response(monkeypatch, response())
    fetched = node_client.fetch_node_configuration("http://192.0.2.1:8000/", "actuation")
    assert fetched == configuration()
    request = opener.call_args.args[0]
    assert request.full_url == "http://192.0.2.1:8000/api/v1/config/nodes/actuation"
    assert opener.call_args.kwargs["timeout"] == 5.0


def test_fetch_sends_token_only_in_authorization_header(monkeypatch):
    opener = install_response(monkeypatch, response())
    node_client.fetch_node_configuration("http://server", "actuation", "private-token")
    request = opener.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer private-token"
    assert "private-token" not in request.full_url


def test_supervisor_captures_token_once_before_retry(monkeypatch):
    monkeypatch.setenv("OMNIBCI_API_TOKEN", "original-token")
    fetch = Mock(side_effect=[node_client.NodeConfigurationUnavailable("waiting"), RuntimeError("done")])
    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    monkeypatch.setattr(node_client.time, "sleep", lambda _: monkeypatch.setenv("OMNIBCI_API_TOKEN", "changed-token"))
    with pytest.raises(RuntimeError, match="done"):
        node_client.run_configured_node("actuation", Mock(), server_url="http://server")
    assert [call.args[2] for call in fetch.call_args_list] == ["original-token", "original-token"]


@pytest.mark.integration
def test_authenticated_configuration_never_follows_redirect():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/credential-leak")
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with pytest.raises(RuntimeError, match="HTTP 302"):
                node_client.fetch_node_configuration(f"http://127.0.0.1:{server.server_port}", "actuation", "private-token")
            assert paths == ["/api/v1/config/nodes/actuation"]
        finally:
            server.shutdown()
            worker.join(timeout=5)


def test_selected_configuration_waits_without_hardware(monkeypatch):
    install_response(monkeypatch, response(state="selected", generation=None))
    with pytest.raises(node_client.NodeConfigurationUnavailable, match="engine to start"):
        node_client.fetch_node_configuration("http://localhost:8000", "actuation")


@pytest.mark.parametrize("status", [409, 502, 503, 504])
def test_transient_server_status_waits(monkeypatch, status):
    monkeypatch.setattr(node_client, "urlopen", Mock(side_effect=HTTPError("url", status, "wait", {}, None)))
    with pytest.raises(node_client.NodeConfigurationUnavailable):
        node_client.fetch_node_configuration("http://localhost", "actuation")


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timeout")])
def test_offline_server_waits(monkeypatch, error):
    monkeypatch.setattr(node_client, "urlopen", Mock(side_effect=error))
    with pytest.raises(node_client.NodeConfigurationUnavailable):
        node_client.fetch_node_configuration("http://localhost", "actuation")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_invalid_server_request_is_fatal(monkeypatch, status):
    monkeypatch.setattr(node_client, "urlopen", Mock(side_effect=HTTPError("url", status, "bad", {}, None)))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        node_client.fetch_node_configuration("http://localhost", "actuation")
    assert not isinstance(error.value, node_client.NodeConfigurationUnavailable)


@pytest.mark.parametrize("content", [b"not json", [], {"success": False},
    {"success": True, "data": {"state": []}}, response(generation=None),
    pytest.param(b" " * (node_client._MAX_RESPONSE_BYTES + 1), id="oversized-response")])
def test_malformed_or_oversized_configuration_is_fatal(monkeypatch, content):
    install_response(monkeypatch, content)
    with pytest.raises(ValueError):
        node_client.fetch_node_configuration("http://localhost", "actuation")


def test_wrong_role_cannot_be_used(monkeypatch):
    install_response(monkeypatch, response())
    with pytest.raises(ValueError, match="different node role"):
        node_client.fetch_node_configuration("http://localhost", "stimulation")


@pytest.mark.parametrize("url", ["file:///config", "localhost", "http://u:p@localhost", "http://localhost?query=1"])
def test_invalid_server_url_fails_without_request(monkeypatch, url):
    opener = Mock()
    monkeypatch.setattr(node_client, "urlopen", opener)
    with pytest.raises(ValueError):
        node_client.fetch_node_configuration(url, "actuation")
    opener.assert_not_called()


def test_server_cli_flag_overrides_environment(monkeypatch):
    monkeypatch.setenv("OMNIBCI_SERVER_URL", "http://env:8000")
    assert node_client.node_server_argument("actuation", []) == "http://env:8000"
    assert node_client.node_server_argument("actuation", ["--server", "http://cli:9000"]) == "http://cli:9000"


def test_old_cli_flag_is_rejected():
    with pytest.raises(SystemExit) as error:
        node_client.node_server_argument("actuation", ["--backend", "http://old:8000"])
    assert error.value.code == 2


def test_supervisor_retries_before_application_and_refetches_only_after_clean_exit(monkeypatch):
    calls = []
    snapshots = []
    values = iter([node_client.NodeConfigurationUnavailable("offline"), configuration(),
                   configuration(generation="run-2")])

    def fetch(*args):
        calls.append("fetch")
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    def materialize(config, catalog):
        calls.append("validate")
        return {"CONFIG.generation": config.generation}

    def application(**kwargs):
        calls.append("construct")
        snapshots.append(kwargs["settings_snapshot"])

        def run():
            calls.append("run-cleanup")
            return kwargs["settings_snapshot"]["CONFIG.generation"] == "run-1"
        return Mock(run=run)

    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    def prepare(snapshot, modules):
        assert modules == ("actuation",)
        calls.append("prepare")
        return snapshot
    monkeypatch.setattr(node_client, "PluginArtifactPreparer", lambda store: Mock(prepare_snapshot=prepare))
    monkeypatch.setattr(node_client, "materialize_node_configuration", materialize)
    monkeypatch.setattr(node_client, "CatalogConfigurationAdapter", Mock())
    monkeypatch.setattr(node_client.time, "sleep", lambda _: calls.append("wait"))
    node_client.run_configured_node("actuation", application, server_url="http://server")
    assert calls == ["fetch", "wait", "fetch", "validate", "prepare", "construct", "run-cleanup", "wait",
                     "fetch", "validate", "prepare", "construct", "run-cleanup"]
    assert snapshots == [{"CONFIG.generation": "run-1"}, {"CONFIG.generation": "run-2"}]


@pytest.mark.parametrize("stage", ["fetch", "validate", "construct", "run"])
def test_supervisor_never_retries_invalid_configuration_hardware_or_cleanup(monkeypatch, stage):
    error = RuntimeError("invalid config or hardware cleanup")
    fetch = Mock(return_value=configuration())
    validate = Mock(return_value={"PLUGINS.bindings": []})
    application = Mock(return_value=Mock(run=Mock(side_effect=error if stage == "run" else None)))
    if stage == "fetch":
        fetch.side_effect = error
    elif stage == "validate":
        validate.side_effect = error
    elif stage == "construct":
        application.side_effect = error
    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    monkeypatch.setattr(node_client, "materialize_node_configuration", validate)
    monkeypatch.setattr(node_client, "CatalogConfigurationAdapter", Mock())
    sleeper = Mock()
    monkeypatch.setattr(node_client.time, "sleep", sleeper)
    with pytest.raises(RuntimeError, match="invalid config or hardware cleanup"):
        node_client.run_configured_node("actuation", application, server_url="http://server")
    fetch.assert_called_once()
    sleeper.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'prepare', 'run'])
def test_node_reserves_resources_before_construction_and_retains_failed_cleanup(monkeypatch, tmp_path, failure):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from tests.infrastructure.test_plugin_store_ownership import claim
    root = tmp_path / 'plugins'
    monkeypatch.setattr(node_client, 'fetch_node_configuration', lambda *args: configuration())
    monkeypatch.setattr(node_client, 'materialize_node_configuration', lambda *args: {'PLUGINS.bindings': []})

    def prepare(snapshot, modules):
        if failure == 'prepare':
            raise RuntimeError('static preparation failed')
        return snapshot

    def construct(**kwargs):
        controller = claim(root)
        try:
            with pytest.raises(PluginPackageConflictError, match='owner'):
                with controller.mutation():
                    pytest.fail('Node resources were not protected')
        finally:
            controller.close()
        return Mock(run=Mock(return_value=False, side_effect=RuntimeError('cleanup failed') if failure == 'run' else None))

    factory = Mock(side_effect=construct)
    monkeypatch.setattr(node_client, 'PluginArtifactPreparer', lambda store: Mock(prepare_snapshot=prepare))
    if failure:
        with pytest.raises(RuntimeError, match='failed'):
            node_client.run_configured_node('actuation', factory, server_url='http://server')
    else:
        node_client.run_configured_node('actuation', factory, server_url='http://server')
    if failure == 'prepare':
        factory.assert_not_called()
    if failure == 'run':
        owner = claim(root)
        try:
            with pytest.raises(PluginPackageConflictError, match='unconfirmed shutdown'):
                with owner.mutation():
                    pytest.fail('Cleanup failure admitted a package mutation')
        finally:
            owner.close()
    else:
        owner = claim(root)
        owner.close()


def test_supervisor_keeps_its_store_when_a_clean_restart_changes_the_environment(monkeypatch, tmp_path):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from tests.infrastructure.test_plugin_store_ownership import claim
    root = tmp_path / 'plugins'
    monkeypatch.setattr(node_client, 'fetch_node_configuration', lambda *args: configuration())
    monkeypatch.setattr(node_client, 'materialize_node_configuration', lambda *args: {'PLUGINS.bindings': []})
    monkeypatch.setattr(node_client.time, 'sleep', lambda _: None)
    starts = []

    def construct(**kwargs):
        with pytest.raises(PluginPackageConflictError, match='owner'):
            claim(root, role='actuation')
        starts.append(kwargs['settings_snapshot'])
        def run():
            monkeypatch.setenv('OMNIBCI_PLUGIN_DIR', str(tmp_path / 'other-plugins'))
            return len(starts) == 1
        return Mock(run=run)

    node_client.run_configured_node('actuation', construct, server_url='http://server')
    assert len(starts) == 2
