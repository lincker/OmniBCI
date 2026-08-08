"""Tests for the plugin discovery API."""

from unittest.mock import patch

import pytest

from omnibci.application.queries.plugin_queries import PluginInfo


_PATCH_TARGET = "omnibci.presentation.routers.plugins.list_plugins"
_VALID_CATEGORIES = ("paradigm", "decoder", "acquisition", "effector")


@pytest.mark.parametrize("category", _VALID_CATEGORIES)
def test_each_plugin_category_is_forwarded_to_catalog(api_client, category):
    with patch(_PATCH_TARGET, return_value=[]) as list_plugins:
        response = api_client.get(f"/api/v1/plugins/{category}")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "ok",
        "data": {"paradigms": []},
    }
    list_plugins.assert_called_once_with(category)


def test_plugin_manifest_fields_are_exposed(api_client):
    plugin = PluginInfo(
        id="logger",
        plugin_type="EFFECTOR_LOGGER",
        display_name="Logger",
        description="Writes decisions to the application log.",
        version="1.2.3",
        source="builtin",
        params={"level": "INFO"},
        controls={
            "level": {
                "label": "Log level",
                "type": "select",
                "default": "INFO",
            }
        },
    )

    with patch(_PATCH_TARGET, return_value=[plugin]):
        response = api_client.get("/api/v1/plugins/effector")

    assert response.status_code == 200
    assert response.json()["data"]["paradigms"] == [
        {
            "id": "logger",
            "plugin_type": "EFFECTOR_LOGGER",
            "display_name": "Logger",
            "description": "Writes decisions to the application log.",
            "version": "1.2.3",
            "source": "builtin",
            "params": {"level": "INFO"},
            "controls": {
                "level": {
                    "label": "Log level",
                    "type": "select",
                    "default": "INFO",
                }
            },
            "name": "Logger",
        }
    ]


def test_unknown_plugin_category_returns_validation_error(api_client):
    with patch(_PATCH_TARGET) as list_plugins:
        response = api_client.get("/api/v1/plugins/unknown")

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "Unknown plugin category 'unknown'" in response.json()["message"]
    list_plugins.assert_not_called()
