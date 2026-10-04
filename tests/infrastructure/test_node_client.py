"""Standalone configuration retrieval stays outside the application lifetime."""
import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from omnibci.application.node_configuration import NodeConfiguration
from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
from omnibci.infrastructure.plugins import node_client


def configuration(*, generation="run-1"):
    return NodeConfiguration(
        "actuation", "a" * 64, generation,
        {"ACQUISITION.sample_rate": 250, "ACQUISITION.channel_number": 2,
         "ACTUATION.enabled": True},
        (ProfileBinding("actuation.effector", "effector", None,
                        PluginSelection("test.effector", "logger", "1.0.0"), {}),),
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


def test_selected_configuration_waits_without_hardware(monkeypatch):
    install_response(monkeypatch, response(state="selected", generation=None))
    with pytest.raises(node_client.NodeConfigurationUnavailable, match="engine to start"):
        node_client.fetch_node_configuration("http://localhost:8000", "actuation")


@pytest.mark.parametrize("status", [409, 502, 503, 504])
def test_transient_backend_status_waits(monkeypatch, status):
    monkeypatch.setattr(node_client, "urlopen", Mock(side_effect=HTTPError("url", status, "wait", {}, None)))
    with pytest.raises(node_client.NodeConfigurationUnavailable):
        node_client.fetch_node_configuration("http://localhost", "actuation")


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timeout")])
def test_offline_backend_waits(monkeypatch, error):
    monkeypatch.setattr(node_client, "urlopen", Mock(side_effect=error))
    with pytest.raises(node_client.NodeConfigurationUnavailable):
        node_client.fetch_node_configuration("http://localhost", "actuation")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_invalid_backend_request_is_fatal(monkeypatch, status):
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
def test_invalid_backend_url_fails_without_request(monkeypatch, url):
    opener = Mock()
    monkeypatch.setattr(node_client, "urlopen", opener)
    with pytest.raises(ValueError):
        node_client.fetch_node_configuration(url, "actuation")
    opener.assert_not_called()


def test_backend_cli_flag_overrides_environment(monkeypatch):
    monkeypatch.setenv("OMNIBCI_BACKEND_URL", "http://env:8000")
    assert node_client.node_backend_argument("actuation", []) == "http://env:8000"
    assert node_client.node_backend_argument("actuation", ["--backend", "http://cli:9000"]) == "http://cli:9000"


def test_supervisor_retries_before_application_and_refetches_only_after_clean_exit(monkeypatch):
    import omnibci.conf as conf
    calls = []
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

        def run():
            calls.append("run-cleanup")
            return kwargs["settings_snapshot"]["CONFIG.generation"] == "run-1"
        return Mock(run=run)

    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    monkeypatch.setattr(node_client, "materialize_node_configuration", materialize)
    monkeypatch.setattr(node_client, "CatalogConfigurationAdapter", Mock())
    monkeypatch.setattr(node_client.time, "sleep", lambda _: calls.append("wait"))
    monkeypatch.setattr(conf, "settings", Mock())
    node_client.run_configured_node("actuation", application, backend_url="http://backend")
    assert calls == ["fetch", "wait", "fetch", "validate", "construct", "run-cleanup", "wait",
                     "fetch", "validate", "construct", "run-cleanup"]
    assert conf.settings.use_snapshot.call_args.args[0] == {"CONFIG.generation": "run-2"}


@pytest.mark.parametrize("stage", ["fetch", "validate", "construct", "run"])
def test_supervisor_never_retries_invalid_configuration_hardware_or_cleanup(monkeypatch, stage):
    import omnibci.conf as conf
    error = RuntimeError("invalid config or hardware cleanup")
    fetch = Mock(return_value=configuration())
    validate = Mock(return_value={})
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
    monkeypatch.setattr(conf, "settings", Mock())
    with pytest.raises(RuntimeError, match="invalid config or hardware cleanup"):
        node_client.run_configured_node("actuation", application, backend_url="http://backend")
    fetch.assert_called_once()
    sleeper.assert_not_called()
