from tests.conftest import login


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
