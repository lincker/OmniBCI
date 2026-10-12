"""Fetch node-scoped configuration at startup and after a clean disconnection."""

import argparse
import json
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from omnibci.application.node_configuration import (
    NodeConfiguration,
    materialize_node_configuration,
)
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 5.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRY_DELAY_S = 1.0


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


# Credentials are only sent to the configured backend, never a redirect target.
urlopen = build_opener(_NoRedirect()).open


class NodeConfigurationUnavailable(RuntimeError):
    """The backend cannot currently supply a running configuration."""


def node_backend_argument(role: str, argv=None) -> str:
    parser = argparse.ArgumentParser(description=f"Run an OmniBCI {role} node")
    parser.add_argument(
        "--backend",
        default=os.environ.get("OMNIBCI_BACKEND_URL", "http://127.0.0.1:8000"),
        help="Backend URL (default: OMNIBCI_BACKEND_URL or http://127.0.0.1:8000)",
    )
    return parser.parse_args(argv).backend


def fetch_node_configuration(backend_url: str, role: str, api_token: str | None = None) -> NodeConfiguration:
    """Bound each HTTP request; never fall back to a local activation file."""
    if role not in {"stimulation", "actuation"}:
        raise ValueError("Unknown node role")
    parsed = urlsplit(backend_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("Backend URL must be an HTTP(S) URL without credentials, query, or fragment")
    # Validate malformed/out-of-range ports before starting a retry loop.
    parsed.port
    headers = {"Accept": "application/json"}
    if api_token:
        if "\r" in api_token or "\n" in api_token:
            raise ValueError("API token must not contain line breaks")
        headers["Authorization"] = f"Bearer {api_token}"
    request = Request(
        backend_url.rstrip("/") + f"/api/v1/config/nodes/{role}",
        headers=headers,
    )
    try:
        deadline = time.monotonic() + _REQUEST_TIMEOUT_S
        with urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
            chunks = []
            size = 0
            while size <= _MAX_RESPONSE_BYTES:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Node configuration response timed out")
                chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            content = b"".join(chunks)
    except HTTPError as exc:
        exc.close()
        if exc.code in {409, 502, 503, 504}:
            raise NodeConfigurationUnavailable(
                f"Waiting for the backend to provide a running {role} configuration"
            ) from exc
        raise RuntimeError(f"Backend rejected {role} configuration request (HTTP {exc.code})") from exc
    except (URLError, OSError) as exc:
        raise NodeConfigurationUnavailable("Waiting for the backend connection") from exc
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ValueError("Node configuration response is too large")
    try:
        envelope = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Backend returned invalid node configuration JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise ValueError("Backend returned an invalid node configuration response")
    data = envelope.get("data")
    if (not isinstance(data, dict) or not isinstance(data.get("state"), str)
            or data["state"] not in {"selected", "running"}):
        raise ValueError("Backend returned an invalid node configuration state")
    configuration = NodeConfiguration.from_dict(data.get("configuration"))
    if configuration.role != role:
        raise ValueError("Backend returned configuration for a different node role")
    if data["state"] == "selected":
        raise NodeConfigurationUnavailable("Waiting for the engine to start")
    if not configuration.generation:
        raise ValueError("Running node configuration requires an engine generation")
    return configuration


def run_configured_node(role: str, application_factory, *, backend_url: str) -> None:
    """Retry only unavailable configuration or an explicitly clean link exit.

    HTTP polling runs between application lifetimes, outside all real-time
    loops. Hardware initialization and teardown errors propagate to the caller.
    """
    waiting_message = None
    api_token = os.environ.get("OMNIBCI_API_TOKEN")
    while True:
        try:
            configuration = fetch_node_configuration(backend_url, role, api_token)
        except NodeConfigurationUnavailable as exc:
            message = str(exc)
            if message != waiting_message:
                logger.info("%s", message)
                waiting_message = message
            time.sleep(_RETRY_DELAY_S)
            continue

        snapshot = materialize_node_configuration(configuration, CatalogConfigurationAdapter())
        from omnibci.conf import settings
        settings.use_snapshot(snapshot)
        logger.info(
            "Starting %s for revision %s, generation %s",
            role, configuration.revision, configuration.generation,
        )
        application = application_factory(settings_snapshot=snapshot)
        if application.run() is not True:
            return
        logger.info("%s disconnected and stopped cleanly; refreshing configuration", role)
        waiting_message = None
        time.sleep(_RETRY_DELAY_S)
