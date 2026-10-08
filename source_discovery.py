"""Small-batch recurring source discovery and collector validation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collectors import validate_candidate
from polling import collect
from story_store import StoryStore, utc_now

LANES = {"A", "B", "C", "D"}


def fetch_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": "insurance-stories-source-discovery/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def discovery_prompt(targets: list[dict], batch_size: int = 10) -> str:
    compact = [{"name": t["name"], "lane": t["lane"], "jurisdiction": t["jurisdiction"], "query": t.get("query", "")} for t in targets[:batch_size]]
    return ("Find recurring primary publication sources for these targets. Return ONLY a JSON array of objects with "
            "name, organization, jurisdiction, lane (A/B/C/D), proposed_url, poll_url, collector_type "
            "(rss/atom/html_list/json), collector_config, publication_type, reason. Prefer current listing/feed URLs, "
            "not historical archives. Do not return stories or editorial rankings. Targets: " + json.dumps(compact, separators=(",", ":")))


def _result(status: str, **values) -> dict:
    return {"status": status, **values}


def validate_source(candidate: dict, fetcher=fetch_url) -> dict:
    """Run a bounded listing test through the existing collector dispatch."""
    source = {"source_id": 0, "source_url": candidate["proposed_url"], "poll_url": candidate.get("poll_url") or candidate["proposed_url"],
              "collector_type": candidate.get("collector_type", ""), "collector_config": candidate.get("collector_config") or "{}"}
    try:
        body = fetcher(source["poll_url"])
        publications = collect(source, body)
        accepted, rejected, ambiguous = 0, 0, 0
        examples = []
        for item in publications[:50]:
            decision = validate_candidate(source, item).decision
            if decision == "accepted": accepted += 1
            elif decision in {"navigation_archive", "old"}: rejected += 1
            else: ambiguous += 1
            if len(examples) < 3:
                examples.append({"headline": item.headline, "url": item.url, "published_at": item.published_at, "decision": decision})
        if not publications:
            status = "NO_PUBLICATIONS"
        elif accepted or ambiguous:
            status = "VALID"
        else:
            status = "ARCHIVE_OR_NAVIGATION"
        return _result(status, accessible=True, collector_type=source["collector_type"], publications_extracted=len(publications),
                       accepted=accepted, rejected=rejected, ambiguous=ambiguous, examples=examples)
    except HTTPError as exc:
        status = "INACCESSIBLE" if exc.code in {401, 403, 408, 429} else "INACTIVE" if exc.code == 404 else "FAILED"
        return _result(status, accessible=False, http_status=exc.code, error=str(exc), collector_type=source["collector_type"], publications_extracted=0, examples=[])
    except Exception as exc:
        return _result("FAILED", accessible=False, error=str(exc), collector_type=source["collector_type"], publications_extracted=0, examples=[])


def run_discovery(store: StoryStore, discoveries: list[dict], *, fetcher=fetch_url, validate=True, target_limit=10) -> dict:
    started = utc_now()
    targets = store.select_discovery_targets(target_limit)
    stats = {"targets_checked": len(targets), "discoveries_received": len(discoveries), "proposals_created": 0,
             "sources_validated": 0, "sources_failed": 0, "duplicates_skipped": 0}
    for item in discoveries:
        if item.get("lane") not in LANES:
            continue
        try:
            candidate_id, created = store.propose_source(name=item["name"], organization=item.get("organization", ""),
                jurisdiction=item["jurisdiction"], lane=item["lane"], proposed_url=item["proposed_url"],
                collector_type=item.get("collector_type", ""), poll_url=item.get("poll_url"),
                collector_config=item.get("collector_config", "{}"), publication_type=item.get("publication_type", ""),
                reason=item.get("reason", ""))
        except (KeyError, ValueError):
            stats["sources_failed"] += 1
            continue
        if not created:
            stats["duplicates_skipped"] += 1
            continue
        stats["proposals_created"] += 1
        if validate:
            result = validate_source(item, fetcher)
            store.update_source_candidate(candidate_id, validation_status=result["status"], validation_result=result,
                                          reason=item.get("reason", ""))
            stats["sources_validated"] += 1
            if result["status"] == "FAILED":
                stats["sources_failed"] += 1
    store.record_discovery_run(started_at=started, completed_at=utc_now(), targets_checked=len(targets),
                               proposals_created=stats["proposals_created"], sources_validated=stats["sources_validated"],
                               sources_failed=stats["sources_failed"], summary=stats)
    return stats


def load_json(path: str | Path) -> list[dict]:
    value = json.loads(Path(path).read_text())
    return value if isinstance(value, list) else value.get("sources", [])
