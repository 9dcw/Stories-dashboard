from __future__ import annotations

from export_stories import build_read_model
from story_store import StoryStore

PUBLIC_FEEDBACK_FIELDS = (
    "feedback_id", "item_id", "action", "reason_code", "note", "prior_status",
    "resulting_status", "reverts_feedback_id", "created_at",
)


def sanitize_feedback(event: dict | None) -> dict | None:
    if event is None:
        return None
    return {name: event[name] for name in PUBLIC_FEEDBACK_FIELDS}


def build_live_read_model(store: StoryStore) -> dict:
    with store.read_snapshot() as snapshot:
        model = build_read_model(snapshot)
        dismissals = snapshot.get_active_dismissals([
            candidate["item_id"] for candidate in model["recent_candidates"]
        ])
        for candidate in model["recent_candidates"]:
            candidate["active_dismissal"] = sanitize_feedback(
                dismissals.get(candidate["item_id"])
            )
        return model
