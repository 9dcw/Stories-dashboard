import copy

import pytest

from api.candidate_page import paginate_candidates

FILTERS = dict(source="", lane="", screening="retained", dismissed="hide", problem="")


def feed():
    return dict(recent_candidates=[dict(item_id=i, headline=f"Candidate {i}", gist=f"Evidence {i}",
                date="2026-11-14", status="NEW", summary_status="COMPLETE", source="Court", lane="B")
                for i in range(125)])


def page(model, **kwargs):
    return paginate_candidates(model, **(dict(view="candidates", query="", filters=FILTERS, limit=50) | kwargs))


def test_pages_cover_full_feed_and_cursor_survives_dismissed_anchor():
    model = feed()
    first = page(copy.deepcopy(model))
    assert len(first["recent_candidates"]) == 50
    assert first["candidate_page"]["total"] == 125
    assert first["candidate_page"]["category_counts"] == dict(candidates=125, problems=0)
    anchor = first["recent_candidates"][-1]["item_id"]
    model["recent_candidates"][anchor]["status"] = "DISMISSED"
    second = page(copy.deepcopy(model), cursor=first["candidate_page"]["next_cursor"])
    third = page(copy.deepcopy(model), cursor=second["candidate_page"]["next_cursor"])
    assert len(third["recent_candidates"]) == 25
    assert third["candidate_page"]["next_cursor"] is None
    assert [r["item_id"] for p in (first, second, third) for r in p["recent_candidates"]] == list(range(124, -1, -1))


def test_search_and_filters_apply_before_pagination():
    model = feed()
    model["recent_candidates"][0]["source"] = "Other source"
    result = page(copy.deepcopy(model), query="Evidence 0")
    assert [r["item_id"] for r in result["recent_candidates"]] == [0]
    assert result["candidate_page"]["sources"] == ["Court", "Other source"]
    result = page(copy.deepcopy(model), filters=FILTERS | dict(source="Other source"))
    assert [r["item_id"] for r in result["recent_candidates"]] == [0]
    model["recent_candidates"][0]["screening"] = dict(decision="EXCLUDED", needs_review=True)
    result = page(copy.deepcopy(model), view="problems", filters=FILTERS | dict(problem="review"))
    assert [r["item_id"] for r in result["recent_candidates"]] == [0]
    assert result["candidate_page"]["category_counts"] == dict(candidates=124, problems=1)


@pytest.mark.parametrize("cursor", ["invalid", "e30=", "WzEsMl0="])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError, match="cursor"):
        page(feed(), cursor=cursor)
