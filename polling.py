"""Stage 2 source polling and deterministic SQLite ingestion."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from collectors import Candidate, Validation, collect_feed, collect_html_list, collect_json, recover_candidate_metadata, validate_candidate
from story_store import StoryStore, utc_now


def fetch_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": "insurance-stories-poller/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def collect(source: dict, body: str) -> list[Candidate]:
    collector_type = source.get("collector_type", "")
    if collector_type in {"rss", "atom"}:
        return collect_feed(source, body)
    if collector_type == "html_list":
        return collect_html_list(source, body)
    if collector_type == "json":
        return collect_json(source, body)
    raise ValueError(f"Unsupported collector_type: {collector_type}")


def poll_sources(store: StoryStore, source_ids: list[int] | None = None, fetcher=fetch_url,
                 retry_attempts: int = 1, retry_delay: float = 0.0) -> dict[str, int]:
    sources = store.list_sources()
    if source_ids is not None:
        wanted = set(source_ids)
        sources = [source for source in sources if source["source_id"] in wanted]
    else:
        sources = [source for source in sources if source["active"] and source.get("collector_type") and source.get("poll_url")]
    result = {
        "sources_checked": 0, "sources_failed": 0, "items_seen": 0, "items_inserted": 0,
        "items_skipped_existing": 0, "candidates_extracted": 0, "rejected_navigation_archive": 0,
        "rejected_old": 0, "accepted_publications": 0, "ambiguous": 0,
    }
    validation_cache: dict[str, Validation] = {}
    for source in sources:
        result["sources_checked"] += 1
        checked_at = utc_now()
        try:
            last_error = None
            for attempt in range(max(1, retry_attempts)):
                try:
                    candidates = collect(source, fetcher(source["poll_url"]))
                    break
                except (OSError, TimeoutError) as exc:
                    last_error = exc
                    if attempt + 1 >= max(1, retry_attempts):
                        raise
                    if retry_delay:
                        time.sleep(retry_delay)
            else:
                raise last_error or RuntimeError("poll failed")
            result["items_seen"] += len(candidates)
            result["candidates_extracted"] += len(candidates)
            for candidate in candidates:
                key = f"{candidate.url}|{candidate.headline}|{candidate.published_at}|{source.get('collector_config','{}')}|{checked_at[:10]}"
                validation = validation_cache.get(key)
                if validation is None:
                    cached = store.get_validation_cache(key)
                    if cached:
                        validation = Validation(cached["decision"], cached["reason"], cached["headline"])
                    else:
                        validation = validate_candidate(source, candidate, now=checked_at)
                        if validation.decision == "ambiguous":
                            try:
                                recovered = recover_candidate_metadata(candidate, fetcher(candidate.url))
                                validation = validate_candidate(source, recovered, now=checked_at)
                                candidate = recovered
                            except Exception:
                                pass
                        store.save_validation_cache(key, validation.decision, validation.reason, validation.headline)
                    validation_cache[key] = validation
                if validation.decision == "navigation_archive":
                    result["rejected_navigation_archive"] += 1
                    continue
                if validation.decision == "old":
                    result["rejected_old"] += 1
                    continue
                if validation.decision == "ambiguous":
                    result["ambiguous"] += 1
                    continue
                result["accepted_publications"] += 1
                _, inserted = store.add_item_with_result(source["source_id"], validation.headline or candidate.headline, candidate.url, candidate.published_at)
                result["items_inserted" if inserted else "items_skipped_existing"] += 1
            store.update_source_poll_state(source["source_id"], checked_at=checked_at, success_at=utc_now())
        except Exception as exc:
            result["sources_failed"] += 1
            store.update_source_poll_state(source["source_id"], checked_at=checked_at, error=str(exc))
    return result


def main(argv=None) -> None:
    import argparse

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=root / "story-data" / "stories.sqlite3")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=int)
    group.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(poll_sources(StoryStore(args.db), None if args.all else [args.source]), separators=(",", ":")))


if __name__ == "__main__":
    main()
