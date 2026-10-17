import uuid


def headers(csrf):
    return {"Origin": "http://localhost:8000", "X-CSRF-Token": csrf}


def test_dismiss_feedback_restore_and_idempotent_retry(authenticated, api_db):
    client, csrf = authenticated
    _db, store, item_id, _ = api_db
    request_id = str(uuid.uuid4())
    body = {"reason_code": "not_relevant", "note": "Not our beat", "request_id": request_id}

    dismissed = client.post(f"/api/v1/items/{item_id}/dismiss", json=body, headers=headers(csrf))
    assert dismissed.status_code == 201
    result = dismissed.json()
    assert result["item"] == {"item_id": item_id, "status": "DISMISSED"}
    assert result["feedback"]["action"] == "DISMISS"
    assert result["feedback"]["reason_code"] == "not_relevant"
    assert "actor_sub" not in dismissed.text
    assert "actor_email" not in dismissed.text
    assert "request_id" not in dismissed.text
    replay = client.post(f"/api/v1/items/{item_id}/dismiss", json=body, headers=headers(csrf))
    assert replay.status_code == 200
    assert replay.json() == result

    feedback = client.get(f"/api/v1/items/{item_id}/feedback")
    assert feedback.status_code == 200
    assert feedback.json() == [result["feedback"]]

    restore_id = str(uuid.uuid4())
    restored = client.post(f"/api/v1/items/{item_id}/restore", json={
        "dismissal_feedback_id": result["feedback"]["feedback_id"], "request_id": restore_id,
    }, headers=headers(csrf))
    assert restored.status_code == 201
    assert restored.json()["item"] == {"item_id": item_id, "status": "NEW"}
    assert restored.json()["feedback"]["action"] == "RESTORE"
    assert store.get_item(item_id)["status"] == "NEW"

    # The dismissal replay is reconstructed from its immutable event, not the
    # item's now-restored state.
    dismissal_after_restore = client.post(
        f"/api/v1/items/{item_id}/dismiss", json=body, headers=headers(csrf)
    )
    assert dismissal_after_restore.status_code == 200
    assert dismissal_after_restore.json() == result

    second_dismiss = client.post(f"/api/v1/items/{item_id}/dismiss", json={
        "reason_code": "duplicate", "note": "Again", "request_id": str(uuid.uuid4()),
    }, headers=headers(csrf))
    assert second_dismiss.status_code == 201

    # Likewise a restore replay remains byte-for-byte equivalent even after a
    # later dismissal changes the current item state and active dismissal.
    restore_replay = client.post(f"/api/v1/items/{item_id}/restore", json={
        "dismissal_feedback_id": result["feedback"]["feedback_id"], "request_id": restore_id,
    }, headers=headers(csrf))
    assert restore_replay.status_code == 200
    assert restore_replay.json() == restored.json()


def test_feedback_validation_and_store_errors_have_stable_codes(authenticated, api_db):
    client, csrf = authenticated
    _db, _store, item_id, _ = api_db
    endpoint = f"/api/v1/items/{item_id}/dismiss"
    assert client.post(endpoint, json={"reason_code": "manual_cli", "note": "", "request_id": str(uuid.uuid4())}, headers=headers(csrf)).status_code == 422
    assert client.post(endpoint, json={"reason_code": "other", "note": "x" * 1001, "request_id": str(uuid.uuid4())}, headers=headers(csrf)).status_code == 422
    assert client.post(endpoint, json={"reason_code": "other", "note": "", "request_id": "not-a-uuid"}, headers=headers(csrf)).status_code == 422
    assert client.post("/api/v1/items/99999/dismiss", json={"reason_code": "other", "note": "", "request_id": str(uuid.uuid4())}, headers=headers(csrf)).status_code == 404

    good = {"reason_code": "other", "note": "", "request_id": str(uuid.uuid4())}
    assert client.post(endpoint, json=good, headers=headers(csrf)).status_code == 201
    conflict = dict(good, note="changed")
    assert client.post(endpoint, json=conflict, headers=headers(csrf)).status_code == 409
    assert client.post(endpoint, json={"reason_code": "other", "note": "", "request_id": str(uuid.uuid4())}, headers=headers(csrf)).status_code == 409
