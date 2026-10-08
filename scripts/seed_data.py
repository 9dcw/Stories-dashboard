#!/usr/bin/env python3
"""Create the Stage 1 SQLite database and preserve the current dashboard catalog."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from story_store import StoryStore, canonicalize_url

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "story-data" / "stories.sqlite3"
LEGACY_JSON = ROOT / "data" / "stories.json"

SOURCES = [
    ("DOJ and U.S. Attorneys", "A", "US", "https://www.justice.gov/"),
    ("State insurance departments", "A", "US", "https://content.naic.org/"),
    ("Appellate opinions", "B", "US", "https://www.courtlistener.com/"),
    ("Administrative records", "C", "US", "https://www.sec.gov/"),
    ("Local public safety", "D", "US", "https://www.fbi.gov/"),
    ("Insurance trade press", "D", "US", "https://www.insurancejournal.com/"),
]

CANDIDATES = [
    (0, "DOJ announces insurance-fraud sentencing in staged claim case", "https://www.justice.gov/example/staged-claim-sentencing"),
    (0, "Federal prosecutors charge premium-finance scheme", "https://www.justice.gov/example/premium-finance-charge"),
    (0, "Court filing details alleged claim-document falsification", "https://www.justice.gov/example/claim-documents"),
    (0, "U.S. Attorney reports restitution in insurance case", "https://www.justice.gov/example/restitution-insurance"),
    (1, "State regulator issues market-conduct consent order", "https://content.naic.org/example/market-conduct-order"),
    (1, "Insurance department publishes catastrophe claims bulletin", "https://content.naic.org/example/catastrophe-bulletin"),
    (1, "Regulator opens hearing on unusual coverage dispute", "https://content.naic.org/example/coverage-hearing"),
    (1, "State fraud bureau posts referral and charging summary", "https://content.naic.org/example/fraud-referral"),
    (2, "Appellate court addresses appraisal and claim deadlines", "https://www.courtlistener.com/example/appraisal-deadlines"),
    (2, "Opinion examines coverage for an unusual loss", "https://www.courtlistener.com/example/unusual-loss"),
    (2, "Court order describes disputed insurer investigation", "https://www.courtlistener.com/example/investigation"),
    (2, "State supreme court clarifies liability-policy language", "https://www.courtlistener.com/example/liability-language"),
    (3, "Administrative filing reveals insurer rehabilitation plan", "https://www.sec.gov/example/rehabilitation"),
    (3, "Public comment notice concerns a new insurance rule", "https://www.sec.gov/example/public-comment"),
    (3, "Rate filing shows a change in catastrophe assumptions", "https://www.sec.gov/example/rate-filing"),
    (3, "Regulatory order records premium-reporting findings", "https://www.sec.gov/example/premium-reporting"),
    (4, "Sheriff describes alleged staged collision investigation", "https://www.fbi.gov/example/staged-collision"),
    (4, "Fire marshal release links arson investigation to claim", "https://www.fbi.gov/example/arson-claim"),
    (4, "Prosecutor announces charges involving commercial coverage", "https://www.fbi.gov/example/commercial-coverage"),
    (4, "Public-safety report documents a suspicious loss", "https://www.fbi.gov/example/suspicious-loss"),
    (5, "Insurers respond to a novel business-interruption dispute", "https://www.insurancejournal.com/example/business-interruption"),
    (5, "Industry report tracks an emerging claims practice", "https://www.insurancejournal.com/example/claims-practice"),
    (5, "Coverage counsel discusses a recent liability ruling", "https://www.insurancejournal.com/example/liability-ruling"),
    (5, "Trade publication reports a new underwriting control", "https://www.insurancejournal.com/example/underwriting-control"),
]


def first_url(value: str) -> str:
    candidate = value.strip().split()[0] if value.strip() else ""
    return candidate if candidate.startswith(("http://", "https://")) else ""


def ensure_source(store: StoryStore, name: str, lane: str, jurisdiction: str, url: str, notes: str) -> int:
    canonical_url = canonicalize_url(url)
    for row in store.list_sources():
        if row["name"] == name and row["source_url"] == canonical_url:
            return row["source_id"]
    return store.add_source(name, lane, jurisdiction, url, notes=notes)


def seed(*, include_fixtures: bool = False) -> StoryStore:
    store = StoryStore(DB)
    source_ids = {}
    for name, lane, jurisdiction, url in SOURCES:
        source_ids[name] = ensure_source(store, name, lane, jurisdiction, url, "Stage 1 seed source")

    if LEGACY_JSON.exists():
        legacy = json.loads(LEGACY_JSON.read_text())
        legacy_source = ensure_source(store, "Existing dashboard catalog", "legacy", "mixed", "https://9dcw.github.io/Stories-dashboard/", "Imported from the pre-Stage-1 dashboard snapshot")
        imported = set()
        for index, story in enumerate(legacy.get("stories", [])):
            story_key = story.get("id") or f"legacy-{index}"
            source_url = first_url(story.get("source", "")) or first_url(story.get("origin_url", ""))
            # Do not manufacture clickable legacy.example URLs for stories whose
            # original source URL was not preserved in the old snapshot.
            if not source_url:
                continue
            item = store.add_item(legacy_source, story.get("title") or story_key, source_url, story.get("eventKey"))
            if item.item_id in imported:
                continue
            imported.add(item.item_id)
            story_id = store.promote_item(item, story.get("title") or story_key, story.get("stage") or story.get("status") or "ACTIVE")
            store.update_story_links(story_id, google_doc_url=story.get("doc") or "", telegram_thread_url=story.get("telegramTopicLink") or story.get("telegram") or "", research_folder_url=story.get("phase2Folder") or story.get("phase3Folder") or "")

    if include_fixtures:
        item_ids = []
        for index, (source_index, headline, url) in enumerate(CANDIDATES, start=1):
            source_name = SOURCES[source_index][0]
            item = store.add_item(source_ids[source_name], headline, url, published_at=f"2026-10-{index:02d}", first_seen_at=f"2026-10-{index:02d}T12:00:00Z")
            item_ids.append(item)
        # Intentional tracking and slash variations; these must remain one row each.
        for index in (0, 5, 10):
            source_index, headline, url = CANDIDATES[index]
            source_name = SOURCES[source_index][0]
            store.add_item(source_ids[source_name], headline + " (tracking variation)", url + ("/" if not url.endswith("/") else "") + "?utm_source=seed&utm_campaign=stage1#top", published_at=f"2026-10-{index + 1:02d}")

        for number, item in enumerate((item_ids[0], item_ids[6], item_ids[12], item_ids[18]), start=1):
            story_id = store.promote_item(item, f"Seed promoted story {number}")
            store.update_story_links(
                story_id,
                google_doc_url=f"https://docs.google.com/document/d/stage1-seed-{number}",
                telegram_thread_url=f"https://t.me/c/3984316484/{100 + number}?thread={99 + number}&topic",
                research_folder_url=f"https://drive.google.com/drive/folders/stage1-seed-{number}",
            )
    return store


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-fixtures", action="store_true", help="Add synthetic test candidates; never use for the published catalog")
    args = parser.parse_args()
    store = seed(include_fixtures=args.include_fixtures)
    print(f"Seeded {len(store.list_items())} unique items and {len(store.list_stories())} story projects in {DB}")
