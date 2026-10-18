# Stories Dashboard

Authenticated, mobile-first live dashboard for the insurance story discovery workflow.

Canonical live site: https://vmi3298003.contaboserver.net/

## Stage 1 architecture

- `story-data/stories.sqlite3` is the authoritative SQLite database used by the live Contabo service.
- `data/stories.json` is an optional generated export for offline inspection; it is not served by the live application.
- `story_store.py` contains URL canonicalization and deterministic database operations.
- `scripts/story_cli.py` provides the small manual CLI.
- `scripts/seed_data.py` creates the schema and imports the existing dashboard catalog. Synthetic candidate fixtures are opt-in with `--include-fixtures` and must not be used for a published catalog.
- `.github/workflows/deploy-contabo.yml` deploys the authenticated live application from `main`.
- GitHub Pages deployment has been retired; state-changing actions are served only by the authenticated live API.

Automated feed polling and LLM summarization are intentionally out of scope for Stage 1.

## Stage 2 deterministic polling

Stage 2 adds source polling without changing the SQLite system of record:

- `collectors.py` contains the small RSS/Atom, HTML-list, and JSON collectors.
- `polling.py` normalizes candidates and inserts them through the existing URL canonicalization and uniqueness constraints.
- `polling_sources.py` defines the representative source registry, including official opinion feeds/listings for all twelve Article III appellate circuits plus the Federal Circuit.
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
python3 scripts/story_cli.py update-status ITEM_ID DISMISSED
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

Create an optional local JSON export (not needed for the live dashboard):

```bash
python3 scripts/story_cli.py export --output data/stories.json
```

The live dashboard includes enrolled-source management. It shows active and disabled rows, source IDs, publication/feed URLs, lane, jurisdiction, last successful poll, publications discovered in the last 30 days, and latest errors. Source enable/disable remains a CLI operation.

Candidate cards include an authenticated **Dismiss** action. It records the dismissal in SQLite and supports undo/restore while preserving the audit event. Dismissed items remain in SQLite but are hidden from active candidates by default; **Show dismissed** reveals them for review.

Candidate cards include **Promote to Phase 2**. The authenticated API immediately records the project as `Phase 2 pending` and removes it from Phase 1 investigation. The Telegram handoff then directs the research workflow to resolve the exact `story_root_key` and `event_key`, use the canonical dedicated Stories topic, and create or verify the Phase 2 Drive package under `Insurance Research / Active Stories` (story folder, `Research & Treatment` Doc, `Images` folder), including evidence labels and a read-back of the shared `phase2` row. Only after those artifacts are verified should Telegram call `/api/v1/telegram/phase2-complete` with the story-scoped capability in the handoff. The API changes the state to `Phase 2`; pending and completed work remain out of Phase 1 investigation. The capability must not be exposed outside the private handoff.

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
- PDF source links are accepted as a fallback: up to the first three pages are rendered into a temporary vertically stacked PNG and passed to image-capable gist commands. The PNG is deleted after each item; the stored extraction method is `pdf_first_three_pages_image`.
- `summarize_items.py` processes one item or all pending items. It isolates failures and skips completed summaries unless `--force` is supplied; legacy completed summaries without a display headline are eligible for regeneration.
- The summarizer is intentionally a local command boundary: set `STORY_GIST_COMMAND` to a command that reads the prompt from stdin and writes the JSON result to stdout. No hosted API or new server is added by this repository.
- Image-capable custom commands receive the temporary PNG path in `STORY_GIST_IMAGE`; the bundled `scripts/hermes_gist_command.py` forwards it to `hermes chat --image`.

Examples:

```bash
STORY_GIST_COMMAND='your-local-gist-command' python3 scripts/summarize_items.py --pending
STORY_GIST_COMMAND='your-local-gist-command' python3 scripts/summarize_items.py --item 123 --force
python3 scripts/story_cli.py export --output data/stories.json
```

The machine-readable result is shaped like `{"items_checked":12,"summaries_created":10,"summaries_failed":2}`. Failed extraction or summarization is recorded on the item and does not stop the batch. Candidate cards show the generated display headline prominently, retain the publisher's original headline in an expandable detail, and include source, date, gist, summary state, search support, and source link. `samples/gists-review.json` contains a small review fixture demonstrating the intended compact format; the production review set should be expanded to 30–50 representative candidates before prompt acceptance.

### Source retrieval policies

All polling and gist fetches use `source_fetch.py`, a shared policy layer rather than one hard-coded HTTP client. It supports per-source JSON configuration in `sources.collector_config` and environment-backed credentials without storing secrets in SQLite or Git. Supported credential modes are `bearer_env`, `token_env`, `basic_env`, and `api_key_env`; policies also control per-host pacing, retryable HTTP statuses, `Retry-After`, and endpoint adapters.

Built-in host behavior:

- SEC requests use a contact-bearing User-Agent when `STORIES_SEC_CONTACT_EMAIL` is set and pace requests at 4 per second or slower.
- CourtListener requests use `COURTLISTENER_API_TOKEN` when available, pace requests at 5 per minute, and transform opinion pages into the REST opinion endpoint. The REST JSON's plain-text or HTML opinion fields feed the same bounded gist extractor.

Set credentials only in the local environment, for example:

```bash
export STORIES_SEC_CONTACT_EMAIL='you@example.com'
export COURTLISTENER_API_TOKEN='token-from-your-courtlistener-profile'
```

A source can override the inferred host policy without code changes:

```json
{"fetch":{"auth_type":"bearer_env","credential_env":"EXAMPLE_TOKEN","min_interval":2,"max_attempts":3}}
```

Tokens are never written to the dashboard database, generated JSON, or repository. Missing credentials do not cause a secret to be guessed; the request proceeds unauthenticated and the source's actual HTTP/error result is recorded.

## Stage 4 automated source discovery

Stage 4 is an independent proposal workflow. It does not run as part of daily ingestion and it never enrolls a source without explicit approval.

- `source_discovery.py` rotates small target batches, consumes compact JSON search results, validates listing/feed URLs through the existing collectors, and records diagnostics in SQLite.
- `source_candidates`, `discovery_targets`, and `discovery_runs` persist proposals, prior decisions, and coverage rotation state. Duplicate URLs are suppressed against both `sources` and prior proposals, including rejected proposals.
- `scripts/source_discovery.py` supports `prompt`, `run`, `list`, `approve`, `reject`, `test`, `enroll`, and `coverage`. Equivalent review commands are also available from `scripts/story_cli.py`.
- `discovery/pilot_sources.json` contains the initial 20-institution pilot across lanes A-D; `discovery/coverage_targets.json` supplies rotating target categories.
- The optional JSON export includes `source_proposals` and `coverage`; approval and enrollment remain CLI-only.

```bash
python3 scripts/source_discovery.py run --input discovery/pilot_sources.json
python3 scripts/source_discovery.py list --status PROPOSED
python3 scripts/source_discovery.py coverage
python3 scripts/story_cli.py approve-source CANDIDATE_ID
python3 scripts/story_cli.py enroll-source CANDIDATE_ID
python3 scripts/story_cli.py export --output data/stories.json
```

Discovery failures are recorded per proposal and do not interrupt `scripts/daily_refresh.py`. Enrolled sources are ordinary rows in `sources` and therefore participate in the existing polling, gist, and export workflow. The discovery process performs listing tests only; it does not backfill historical publications.
