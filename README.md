# Stories Dashboard

Mobile-first, read-only GitHub Pages UI for the insurance story discovery workflow.

Live site: https://9dcw.github.io/Stories-dashboard/

## Stage 1 architecture

- `story-data/stories.sqlite3` is the authoritative local SQLite database.
- `data/stories.json` is a generated read model consumed by GitHub Pages.
- `story_store.py` contains URL canonicalization and deterministic database operations.
- `scripts/story_cli.py` provides the small manual CLI.
- `scripts/seed_data.py` creates the schema and imports the existing dashboard catalog. Synthetic candidate fixtures are opt-in with `--include-fixtures` and must not be used for a published catalog.
- `scripts/refresh.sh` regenerates the JSON snapshot from SQLite.
- GitHub Pages deploys the committed snapshot; it does not read Google Sheets or SQLite.

Automated feed polling and LLM summarization are intentionally out of scope for Stage 1.

## Stage 2 deterministic polling

Stage 2 adds source polling without changing the SQLite system of record:

- `collectors.py` contains the small RSS/Atom, HTML-list, and JSON collectors.
- `polling.py` normalizes candidates and inserts them through the existing URL canonicalization and uniqueness constraints.
- `polling_sources.py` defines the representative ten-source registry.
- `scripts/configure_sources.py` installs or refreshes those registry entries idempotently.
- `scripts/poll_sources.py` emits one compact JSON result and isolates failures by source.

Configure the representative source set after first-time setup:

```bash
python3 scripts/seed_data.py
python3 scripts/configure_sources.py
```

Poll one source or all active configured sources:

```bash
python3 scripts/poll_sources.py --source SOURCE_ID
python3 scripts/poll_sources.py --all
```

Example result:

```json
{"sources_checked":10,"sources_failed":1,"items_seen":84,"items_inserted":7,"items_skipped_existing":77}
```

Source polling fields are stored in `sources`: `collector_type`, `poll_url`, `collector_config`, `last_checked_at`, `last_success_at`, and `last_error`. Existing Stage 1 databases are upgraded in place when `StoryStore` opens them. A successful poll clears the previous error; a failed source records its error and does not prevent other sources from running.

Collectors fetch listing/feed metadata only. They do not fetch article bodies, summarize content, score stories, or perform semantic deduplication. Separate publications of the same event remain separate items.


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
  https://www.nj.gov/dobi/pressreleases.htm \
  --collector-type html_list \
  --poll-url https://www.nj.gov/dobi/pressreleases.htm
```

For the representative Stage 2 set, use `python3 scripts/configure_sources.py`; it is idempotent and will refresh collector settings for existing matching registry entries.

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

The Pages read model also contains an enrolled-source management section. It includes active and disabled rows, source IDs, publication/feed URLs, lane, jurisdiction, last successful poll, publications discovered in the last 30 days, latest errors, and Telegram share links. The links only prepopulate a message for manual review; they do not implement a bot, token, authentication, or backend action.

Disable or enable sources through the existing CLI:

```bash
python3 scripts/story_cli.py disable-source SOURCE_ID
python3 scripts/story_cli.py enable-source SOURCE_ID
python3 scripts/story_cli.py export --output data/stories.json
```

The disable/enable operations only change `sources.active`. Disabling stops future `--all` polling while preserving all existing items and story records. The dashboard always exports both active and disabled sources.

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

## Stage 3 semantic gists

Stage 3 adds a bounded, per-item semantic compression layer without scoring or promotion:

- `candidate_notes` stores the generated `display_headline` and gist, `summary_status`, `gist_v2` prompt version, extraction counts/method, timestamps, and errors.
- `gist.py` fetches ordinary HTML, removes common navigation/site furniture, builds a deterministic bounded input (default maximum: 12,000 characters), and parses the single-call JSON response containing both `display_headline` and `gist`.
- `summarize_items.py` processes one item or all pending items. It isolates failures and skips completed summaries unless `--force` is supplied; legacy completed summaries without a display headline are eligible for regeneration.
- The summarizer is intentionally a local command boundary: set `STORY_GIST_COMMAND` to a command that reads the prompt from stdin and writes the JSON result to stdout. No hosted API or new server is added by this repository.

Examples:

```bash
STORY_GIST_COMMAND='your-local-gist-command' python3 scripts/summarize_items.py --pending
STORY_GIST_COMMAND='your-local-gist-command' python3 scripts/summarize_items.py --item 123 --force
python3 scripts/story_cli.py export --output data/stories.json
```

The machine-readable result is shaped like `{"items_checked":12,"summaries_created":10,"summaries_failed":2}`. Failed extraction or summarization is recorded on the item and does not stop the batch. Candidate cards show the generated display headline prominently, retain the publisher's original headline in an expandable detail, and include source, date, gist, summary state, search support, and source link. `samples/gists-review.json` contains a small review fixture demonstrating the intended compact format; the production review set should be expanded to 30–50 representative candidates before prompt acceptance.

## Stage 4 automated source discovery

Stage 4 is an independent proposal workflow. It does not run as part of daily ingestion and it never enrolls a source without explicit approval.

- `source_discovery.py` rotates small target batches, consumes compact JSON search results, validates listing/feed URLs through the existing collectors, and records diagnostics in SQLite.
- `source_candidates`, `discovery_targets`, and `discovery_runs` persist proposals, prior decisions, and coverage rotation state. Duplicate URLs are suppressed against both `sources` and prior proposals, including rejected proposals.
- `scripts/source_discovery.py` supports `prompt`, `run`, `list`, `approve`, `reject`, `test`, `enroll`, and `coverage`. Equivalent review commands are also available from `scripts/story_cli.py`.
- `discovery/pilot_sources.json` contains the initial 20-institution pilot across lanes A-D; `discovery/coverage_targets.json` supplies rotating target categories.
- The Pages export now includes `source_proposals` and `coverage`, rendered in a separate read-only review section. Approval and enrollment remain CLI-only.

```bash
python3 scripts/source_discovery.py run --input discovery/pilot_sources.json
python3 scripts/source_discovery.py list --status PROPOSED
python3 scripts/source_discovery.py coverage
python3 scripts/story_cli.py approve-source CANDIDATE_ID
python3 scripts/story_cli.py enroll-source CANDIDATE_ID
python3 scripts/story_cli.py export --output data/stories.json
```

Discovery failures are recorded per proposal and do not interrupt `scripts/daily_refresh.py`. Enrolled sources are ordinary rows in `sources` and therefore participate in the existing polling, gist, and export workflow. The discovery process performs listing tests only; it does not backfill historical publications.
