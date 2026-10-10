"""Runtime API exposes plans separately from saved profile mutations."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.runtime_config import router


def test_status_and_review_plan_contract():
    status = dict(running=False, recovery_required=False, accepted_revision="selected", applied_revision=None,
                  generation=None, operation=None)
    plan = dict(plan_id="plan", expected_revision="selected", target_revision="new",
                applied_revision=None, generation=None, running=False, strategy="select_on_start",
                changed_slots=["processing.decoder"], changed_settings=[], session_effect="preserve", errors=[], signal_summary=None)
    requests = []

    def create_plan(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(to_dict=lambda: plan)

    service = SimpleNamespace(status=lambda: status, plan=create_plan)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.container = SimpleNamespace(runtime_configuration=service)
    with TestClient(app) as client:
        assert client.get("/config/runtime").json()["data"] == status
        response = client.post("/config/apply-plan", json={"bindings": [], "expected_revision": "selected"})
        assert response.status_code == 200
        assert response.json()["data"] == plan
        assert requests[-1] == {"bindings": [], "expected_revision": "selected",
                                "refresh_slots": None, "include_host_settings": False, "include_signal_setup": False}
        assert client.post("/config/apply-plan", json={
            "bindings": [], "expected_revision": "selected", "refresh_slots": [],
            "include_host_settings": True,
        }).status_code == 200
        assert requests[-1] == {"bindings": [], "expected_revision": "selected",
                                "refresh_slots": [], "include_host_settings": True, "include_signal_setup": False}
        assert client.post("/config/apply-plan", json={"bindings": [], "unknown": True}).status_code == 422

        def stale(**kwargs):
            raise ProfileRevisionConflict("Configuration changed")
        service.plan = stale
        assert client.post("/config/apply-plan", json={"bindings": []}).status_code == 409
        app.state.container.runtime_configuration = None
        assert client.get("/config/runtime").status_code == 503
