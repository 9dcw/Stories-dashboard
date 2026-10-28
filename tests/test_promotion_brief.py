from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api import auth
from api.read_model import build_live_read_model
from phase2.context import build_context
from phase2.store import WorkflowStore
from tests.conftest import login


def test_brief_survives_recovery_and_is_in_first_research_context(api_db):
    path, store, item, _ = api_db
    brief = "Test whether ordered refunds were actually paid."
    story = store.request_phase2(item, "Refunds", research_brief=brief)
    workflow = WorkflowStore(path)
    workflow.initialize()
    workflow.recover_pending()
    provision = workflow.claim()
    assert provision["step_key"] == "provision"
    inputs = build_context(workflow, dict(provision, step_key="narrative"))
    assert inputs["research_brief"] == brief
    assert build_live_read_model(store)["stories"][0]["research_brief"] == brief
    assert store.request_phase2(item, "Refunds") == story
    assert store.request_phase2(item, "Refunds", research_brief=brief) == story
    with pytest.raises(ValueError, match="original research brief"):
        store.request_phase2(item, "Refunds", research_brief="Different direction")
    assert store.get_story(story)["research_brief"] == brief


def test_brief_api_is_optional_validated_and_saved_before_queue(api_db, runtime_config, monkeypatch, valid_claims):
    config = replace(runtime_config, phase2_engine="repository", phase2_service_secret="k" * 48, phase2_allowed_users="42")
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_a, **_kw: dict(valid_claims))
    with TestClient(create_app(config), base_url="https://localhost") as client:
        headers = {"Origin": config.public_origin, "X-CSRF-Token": login(client)}
        url = f"/api/v1/items/{api_db[2]}/phase2"
        assert client.post(url, json={"research_brief": "x" * 4001}, headers=headers).status_code == 422
        assert not api_db[1].list_stories()
        response = client.post(url, json={"research_brief": "  Why does this matter?  "}, headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()["story"]["research_brief"] == "Why does this matter?"
        assert response.json()["phase2_job"]["state"] == "queued"
        assert client.post(f"/api/v1/items/{api_db[3]}/phase2", headers=headers).status_code == 202
