from tests.conftest import login


def mutation_headers(csrf):
    return {"Origin": "http://localhost:8000", "X-CSRF-Token": csrf}


def test_dashboard_requires_authentication_and_reads_live_sqlite(client, api_db):
    assert client.get("/api/v1/dashboard").status_code == 401
    login(client)
    first = client.get("/api/v1/dashboard")
    assert first.status_code == 200
    assert len(first.json()["recent_candidates"]) == 2

    _db, store, _first_id, second_id = api_db
    store.dismiss_item(second_id, "too_old", "Old", "other-sub", "private@example.com", "outside")
    live = client.get("/api/v1/dashboard").json()
    candidate = next(row for row in live["recent_candidates"] if row["item_id"] == second_id)
    assert candidate["status"] == "DISMISSED"
    assert candidate["active_dismissal"]["reason_code"] == "too_old"
    assert "actor_sub" not in str(live) and "private@example.com" not in str(live)


def test_app_promotion_moves_candidate_to_phase2_pending_before_telegram_finishes(client, api_db):
    csrf = login(client)
    _db, store, item_id, _second_id = api_db

    response = client.post(
        f"/api/v1/items/{item_id}/phase2",
        headers=mutation_headers(csrf),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["story"]["status"] == "Phase 2 pending"
    assert body["story"]["origin_item_id"] == item_id
    assert "phase2_completion_token_hash" not in body["story"]
    assert "telegram_handoff_url" not in body
    assert body["phase2_job"]["state"] == "queued"
    dashboard = client.get("/api/v1/dashboard").json()
    assert item_id not in [row["item_id"] for row in dashboard["recent_candidates"]]
    assert body["story"]["story_id"] in [row["story_id"] for row in dashboard["stories"]]
    assert store.get_item(item_id)["status"] == "PROMOTED"
