#!/usr/bin/env python3
"""Validate that Phase 3 dashboard cards use their verified Phase 2 topic links."""
import csv
import io
import json
import os
import re
from urllib.request import Request, urlopen

SHEET_ID = os.environ.get("DASHBOARD_SHEET_ID", "11EsVxXvsVnr5y73pml5UPcVdAhkT1VZBAxsIV_kjbow")
DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "stories.json")


def sheet_csv(sheet_name):
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
        f"?tqx=out:csv&sheet={sheet_name}"
    )
    request = Request(url, headers={"User-Agent": "stories-dashboard-telegram-validation/1.0"})
    with urlopen(request, timeout=30) as response:
        return list(csv.DictReader(io.TextIOWrapper(response, encoding="utf-8-sig")))


def direct_topic_link(link):
    if not link or "/start" in link or "?start=" in link or "?text=" in link:
        return False
    return bool(
        re.fullmatch(r"https://t\.me/c/[^/]+/\d+\?thread=\d+&topic", link)
        or re.fullmatch(r"https://t\.me/mehyanbot/\d+/\d+", link)
    )


def index(rows):
    result = {}
    for row in rows:
        root = row.get("story_root_key", "").strip()
        event = row.get("event_key", "").strip()
        if root:
            result[("root", root)] = row
        if event:
            result[("event", event)] = row
    return result


with open(DATA_FILE, encoding="utf-8") as handle:
    dashboard = json.load(handle)

phase2 = index(sheet_csv("phase2"))
registry = index(sheet_csv("telegram_topic_registry"))
errors = []
checked = 0

for story in dashboard.get("stories", []):
    if story.get("stage") != "Phase 3":
        continue
    checked += 1
    root = story.get("id", "").strip()
    event = story.get("eventKey", "").strip()
    phase2_row = phase2.get(("root", root)) or phase2.get(("event", event))
    registry_row = registry.get(("root", root)) or registry.get(("event", event))
    label = root or event or "<unnamed Phase 3 story>"

    if not phase2_row:
        errors.append(f"{label}: no exact Phase 2 row")
        continue
    if not registry_row:
        errors.append(f"{label}: no exact telegram_topic_registry row")
        continue

    phase2_link = phase2_row.get("telegram_topic_link", "").strip()
    registry_link = registry_row.get("telegram_topic_link", "").strip()
    dashboard_link = story.get("telegramTopicLink", "").strip()

    if not direct_topic_link(phase2_link):
        errors.append(f"{label}: Phase 2 link is not a direct topic link: {phase2_link or '<blank>'}")
    if not direct_topic_link(registry_link):
        errors.append(f"{label}: registry link is not a direct topic link: {registry_link or '<blank>'}")
    if phase2_link != registry_link:
        errors.append(f"{label}: Phase 2 and registry links differ")
    if dashboard_link != registry_link:
        errors.append(f"{label}: dashboard link does not equal registry link")
    if story.get("telegramTopicId", "").strip() != registry_row.get("telegram_topic_id", "").strip():
        errors.append(f"{label}: dashboard topic ID does not equal registry")
    if story.get("telegramFirstMessageId", "").strip() != registry_row.get("first_message_id", "").strip():
        errors.append(f"{label}: dashboard first message ID does not equal registry")

if errors:
    print("Telegram link validation failed:")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)

print(f"Validated {checked} Phase 3 dashboard record(s): each uses the exact verified Phase 2/registry topic link.")
