# Stories Dashboard

Mobile-first, read-only GitHub Pages UI for the insurance story discovery workflow.

Live site: https://9dcw.github.io/Stories-dashboard/

## Stage 1 architecture

- `story-data/stories.sqlite3` is the authoritative local SQLite database.
- `data/stories.json` is a generated read model consumed by GitHub Pages.
- `story_store.py` contains URL canonicalization and deterministic database operations.
- `scripts/story_cli.py` provides the small manual CLI.
- `scripts/seed_data.py` creates the schema, imports the existing dashboard catalog, and adds 24 candidate fixtures plus intentional URL variations.
- `scripts/refresh.sh` regenerates the JSON snapshot from SQLite.
- GitHub Pages deploys the committed snapshot; it does not read Google Sheets or SQLite.

Automated feed polling and LLM summarization are intentionally out of scope for Stage 1.

## First-time setup

```bash
cd /home/david/.hermes/work/stories-dashboard
python3 scripts/seed_data.py
./scripts/refresh.sh
```

The seed is safe to inspect locally and creates `story-data/stories.sqlite3`. It preserves the existing story catalog by importing the pre-Stage-1 `data/stories.json` before adding test candidates.

## Manual operations

Add a source:

```bash
python3 scripts/story_cli.py add-source \
  "New Jersey Department of Banking and Insurance" A NJ \
  https://www.nj.gov/dobi/
```

Add a candidate item:

```bash
python3 scripts/story_cli.py add-item SOURCE_ID \
  "Headline from the source" \
  "https://example.gov/document?id=123" \
  --published-at 2026-10-07
```

URLs are normalized before insertion. Hostnames and schemes are lowercased, fragments and common tracking parameters are removed, default ports and trailing slashes are normalized, and remaining query parameters are sorted. `canonical_url` and its SHA-256 `url_hash` are unique, so reinserting a tracking variation returns the existing item rather than creating a duplicate.

List or classify candidates:

```bash
python3 scripts/story_cli.py list-items --limit 50
python3 scripts/story_cli.py update-status ITEM_ID IGNORED
```

Promote a candidate:

```bash
python3 scripts/story_cli.py promote ITEM_ID "Working story title"
```

Associate Phase 2/project links:

```bash
python3 scripts/story_cli.py update-links STORY_ID \
  --google-doc-url https://docs.google.com/document/d/DOC_ID/edit \
  --telegram-thread-url 'https://t.me/c/3984316484/100?thread=99&topic' \
  --research-folder-url https://drive.google.com/drive/folders/FOLDER_ID
```

Refresh the Pages read model:

```bash
./scripts/refresh.sh
git add data/stories.json
# Review the generated snapshot, then commit and push when ready.
git commit -m "chore: refresh story discovery snapshot"
git push origin main
```

The Pages workflow only deploys the committed JSON snapshot. SQLite remains authoritative; the UI never writes back to it and no UI file is treated as a database.

## JSON shape

The exporter writes only the fields needed by the UI:

```json
{
  "generated_at": "...",
  "recent_candidates": [],
  "stories": [],
  "sources": []
}
```

Candidate cards show headline, source, date, source link, and status. Promoted story cards show title, origin/source, status, Google Doc, Telegram topic, research folder, and origin link.

## Tests

```bash
python3 -m pytest tests/test_story_store.py -q
```

The tests cover URL normalization, deterministic duplicate rejection, promotion, project-link updates, export shape/determinism, and invalid references/statuses.
