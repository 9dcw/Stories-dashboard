"""Filter the full collection before returning a bounded candidate page."""
import base64
import json

LANES = {"A": "Enforcement & regulatory actions", "B": "Courts & legal opinions",
         "C": "Public agencies & public-interest news", "D": "Insurance industry & risk news"}


def problems(row):
    if row["status"] in {"DISMISSED", "PROMOTED", "IGNORED"}:
        return []
    screening = row.get("screening") or {}
    return (["summary"] if row["summary_status"] == "FAILED" else []) + (
        ["review"] if screening.get("needs_review") or screening.get("decision") == "ERROR" else [])


def in_category(row, view):
    if view == "problems":
        return bool(problems(row))
    return view == "candidates" and row["status"] not in {"DISMISSED", "PROMOTED", "IGNORED"} and (
        row["summary_status"] != "FAILED" and (row.get("screening") or {}).get("decision") != "EXCLUDED")


def matches(row, view, query, filters):
    screening = row.get("screening") or {}
    decision = screening.get("decision")
    if view == "candidates":
        dismissed = row["status"] == "DISMISSED"
        if row["status"] in {"PROMOTED", "IGNORED"} or row["summary_status"] == "FAILED" and not dismissed:
            return False
        if filters["dismissed"] == "hide" and dismissed or filters["dismissed"] == "only" and not dismissed:
            return False
        if filters["source"] and row["source"] != filters["source"] or filters["lane"] and row["lane"] != filters["lane"]:
            return False
        mode = filters["screening"]
        if mode == "retained" and decision == "EXCLUDED" or mode == "EXCLUDED" and decision != "EXCLUDED":
            return False
        if mode == "review" and not screening.get("needs_review") and decision != "ERROR":
            return False
        if mode == "pending" and row.get("screening"):
            return False
    elif view != "problems" or not problems(row) or filters["problem"] and filters["problem"] not in problems(row):
        return False
    haystack = " ".join([v for v in row.values() if isinstance(v, str)] +
                        [row["source"], LANES.get(row["lane"], ""), screening.get("reason") or ""]).lower()
    return query.strip().lower() in haystack


def paginate_candidates(model, *, view, query, filters, limit, cursor=""):
    rows = model["recent_candidates"]
    counts = {view: sum(in_category(row, view) for row in rows) for view in ("candidates", "problems")}
    sources = sorted({row["source"] for row in rows if row["status"] not in {"PROMOTED", "IGNORED"}})
    key = lambda row: (row["date"] or "", row["item_id"])
    selected = sorted((row for row in rows if matches(row, view, query, filters)), key=key, reverse=True)
    total = len(selected)
    if cursor:
        try:
            after = json.loads(base64.urlsafe_b64decode(cursor).decode())
            if not isinstance(after, list) or len(after) != 2 or not isinstance(after[0], str) or type(after[1]) is not int:
                raise ValueError("Invalid cursor")
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid candidate cursor") from error
        selected = [row for row in selected if key(row) < tuple(after)]
    page = selected[:limit]
    next_cursor = (base64.urlsafe_b64encode(json.dumps(key(page[-1])).encode()).decode()
                   if len(selected) > limit else None)
    model["recent_candidates"] = page
    model["candidate_page"] = dict(total=total, next_cursor=next_cursor, category_counts=counts, sources=sources)
    return model
