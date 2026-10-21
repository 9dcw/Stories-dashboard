# Repository-controlled Phase 2

Phase 2 produces a research and editorial treatment package. It never approves publication or advances a story to Phase 3. New execution is disabled until `PHASE2_ENGINE=repository`; the default remains the existing setup workflow.

## Runtime

The web application accepts manual dashboard requests and explicit Telegram commands. One separately supervised worker owns the durable SQLite queue and launches at most two isolated research subprocesses. All Google and Telegram writes are performed by repository adapters. The installed Hermes revision, model and provider are pinned in `phase2/workflows/initial.v1.json`. Update that file only after compatibility tests. No cron, automatic promotion or unbounded agent delegation is involved.

Use the same immutable repository revision for the API image and host worker. Production currently runs image `5a4e0fc`; `/srv` and the older Hermes working checkout were behind it during the audit. Do not replace the live database with a checkout's tracked copy. New runtime state and source snapshots must not be committed to Git.

The six new tables are initialized idempotently by `WorkflowStore.initialize()`. They retain runs, individual attempts, candidates, published versions, decisions, resources and notification retries. The original `phase2_jobs` table remains intact for legacy callbacks and delivery draining. Repository mode disables its pending-project discovery, preventing double dispatch.

The live Hermes probe discovered that the configured DuckDuckGo backend is search-only: the advertised `web_extract` tool cannot retrieve pages in this installation. Research subprocesses therefore expose `web_search` and the repository's registered `stories_fetch_source` tool. It performs bounded public HTTP GETs with redirect checks, blocks private/reserved addresses and credential URLs, respects the installed website policy, extracts HTML/text/PDF content, and records hashes plus bounded source snapshots in provenance. It does not change Hermes settings or enable shell access. Limits are 12 sources per step, 2 MB per response, 30 PDF pages and 32,000 returned characters per source; truncation and unavailable sources remain explicit.

## Configuration and deployment

1. Back up the **production** database with `scripts/backup_db.py`. Validate a restore and foreign keys. Inventory existing packages with `python3 scripts/migrate_phase2.py --db /srv/stories-dashboard/story-data/stories.sqlite3`. Inventory opens SQLite read-only and makes no network calls.
2. Build the API image using the locked `requirements.lock`. Install `requirements-phase2.txt` in the worker environment only if its dependencies are absent. The audited Hermes environment already includes the Google client, JSON Schema library and authentication dependencies. Do not replace Hermes's provider dependencies unnecessarily.
3. Configure the API and worker for the same database. Add `PHASE2_ENGINE=repository`, a generated 32+ character `PHASE2_SERVICE_SECRET`, explicit comma-separated `PHASE2_ALLOWED_USERS` (no wildcard), and `PHASE2_CHAT_ID=-1003984316484`. The browser's Google login is separate from this service credential.
4. The worker additionally needs `PHASE2_GOOGLE_PARENT_ID` (the metadata-verified Active Stories folder), `PHASE2_GOOGLE_TOKEN` (existing authorized-user credential path), `PHASE2_HERMES_PYTHON`, `PHASE2_HERMES_SOURCE`, and `TELEGRAM_BOT_TOKEN`. Use `/etc/stories/phase2-worker.env`, mode 0600; never commit values. Google credentials refresh in memory, without rewriting the shared credential file. Set `PHASE2_LOCK_DIR` to a persistent directory shared by all worker processes on this host.
5. Install `deploy/stories-phase2.service` after reconciling `/srv` to the tested revision. The API and worker must share local storage; SQLite and file locks are not a multi-host queue. After backup/restore validation, enable WAL using an administrative SQLite connection to this database. Keep backups online through SQLite's backup API.
6. Install `integrations/hermes/stories` as the user plugin `~/.hermes/plugins/stories`, preserving its repository provenance. Explicitly enable `stories` in Hermes's plugin allowlist and enable its `stories` toolset for Telegram selected-artifact reads, preserving unrelated settings. Pass the service secret, allowed users, group ID and `PHASE2_API_URL=http://127.0.0.1:8000/api/v1/internal/phase2/dispatch` to the gateway service. Restart only during the deployment window. No Hermes core changes or second Telegram poller are needed.
7. The public Caddy configuration denies `/api/v1/internal/phase2/*`. The application still requires an authenticated, timestamped HMAC request; route visibility alone is not authorization. Verify this rule in the deployed Caddy configuration, which has local differences from Git.
8. Run a disposable workspace pilot before routing real stories. Verify folder parents, seven Docs, cross-links, early Hub visibility, real Hermes research, readback, topic routing, and a controlled human edit/comment conflict. Then enable new dashboard research and apply the reviewed migration manifest.

The isolated Google renderer pilot is reproducible with `scripts/verify_phase2_google.py --token <credential-path> --parent <verified-parent> --live`. It creates tagged test resources, checks human-edit and annotation preservation, and trashes only its own resources. It does not promote a story or send Telegram messages. This passed against the configured Google account during implementation; a complete gateway/workspace rollout pilot is still required.

Do not blindly run deployment examples from older operational skill references that commit SQLite/JSON, infer identity from broad titles, or let a chat agent rewrite complete workspaces. For migrated workspaces the repository operation interface is authoritative. Legacy Sheets are historical/compatibility indexes, not the workflow ledger.

## Registered workflow

The Hub is rendered from SQLite; the six research steps are `angles`, `plan`, `evidence`, `visuals`, `treatment`, and `review`. Evidence and visuals may run concurrently. Each run stores the exact workflow, prompts, schemas, evidence policy, code hash, input snapshot, dependency versions, decisions and engine selection. Outputs are validated before any render. Each step permits at most one contract-repair response.

Initial limits are two concurrent steps, 30 minutes per step, 40 model iterations, 8,000 output tokens per call, 200,000 total tokens per step and 600,000 per run. Usage limits are checked at model-step and workflow boundaries; a provider call already in flight may exceed a boundary. Costs are reported only when Hermes supplies them, never inferred as zero. Transient failures receive up to three attempts with backoff. Unknown write results and identity errors block for reconciliation.

Leases are renewed every 15 seconds and expire after 90 seconds. Fenced writes reject superseded workers. Database transactions never cover network calls. File locks serialize external writes per workspace, including Hub updates. Research remains independent. Notification retries do not rerun research. Telegram notifications are at-least-once: a crash after send but before recording its result can repeat a notification.

If an upstream artifact changes, descendants become potentially stale; they are never silently regenerated. Existing runs block rather than publishing against changed inputs or decisions. Use an explicit refresh operation for the required dependency chain. A completed workflow means the requested operation completed, not that all facts were verified or an editor approved publication.

## Editorial interface

In the registered topic:

```
/story status
/story start
/story read evidence
/story revise angles Focus on policyholder consequences
/story investigate evidence Find the underlying regulatory order
/story refresh plan
/story review treatment
/story decision approve Lead with the policyholder consequences
/story decision reject Do not imply intent without evidence
/story decision instruction Preserve the strongest opposing account
/story decision question Which figure measures paid claims rather than claimed amounts?
/story supersede <decision-id> Revised editorial direction
/story retry
/story cancel <run-id>
```

Ordinary conversation never constitutes a repository mutation. A tool hook restricts Stories discussion to read-only research, selected-artifact reads and clarification, blocking shell, browser, file-write, scheduling, messaging and delegation bypasses. The inbound plugin verifies the real sender and topic before dispatch; the backend repeats the authorization checks. The event ID is the idempotency key. Narrative decisions are editorial constraints, not evidence.

Every generated Doc has navigation, one named managed section and separate human notes. The renderer reads structural content, checks its previous baseline and sends `requiredRevisionId`. Unexpected tabs, missing ranges, changed managed content, suggestions and unresolved/unexamined comments block replacement. Human notes outside the managed section remain untouched. Headers and URLs are rendered by code, not reconstructed by Hermes.

On a conflict, a candidate is retained in SQLite and a proposed revision Doc is created when Google is available. `/story append <version-id>` explicitly adopts that candidate by appending a new managed revision, retaining old text and annotations. It rejects proposals whose inputs are now outdated. `/story import <artifact>` captures current Doc text as bounded human-authored notes; factual additions remain unverified. Neither action automatically rewrites earlier annotations or accepts their claims as established facts.

`/story retry` cannot override a retained candidate conflict. Resolve or cancel the blocked run first. A topic shared by multiple mapped events requires explicit reconciliation before routing; the bridge refuses to guess a project.

## Existing-package migration

The initial audit found 15 Phase 2 projects: ten lacked stored workspace links, and projects 99/101 shared a Doc and folder despite different subjects. These are identity conflicts, not permission to create replacement topics blindly.

Prepare a reviewed JSON manifest:

```json
{"stories":[{"story_id":98,"root_key":"canonical-root","event_key":"canonical-event","folder_id":"verified-folder","images_id":"verified-images","legacy_doc_id":"verified-original-doc","chat_id":"-1003984316484","thread_id":"verified-thread","first_message_id":"verified-first-message"}]}
```

Apply with `python3 scripts/migrate_phase2.py --db <production-db> --manifest <reviewed.json> --apply`. The command validates Drive MIME types/parents and requires the original article link in the legacy Doc before adoption. It verifies topic routing and refuses cross-root resource reuse. New numbered Docs are created by a queued provisioning run. The original Research & Treatment Doc is preserved, with a bounded unverified excerpt and revision recorded as provenance. Research is not automatically launched during migration. Phase 3 projects and stubs are excluded.

The inventory and reviewed manifest are operational artifacts. Store them with appropriate access controls, outside Git when they contain private links. Missing/ambiguous mappings stay blocked until reconciled with the workbook, registered topic and document contents. A title match alone is insufficient.

## Rollback and monitoring

Stop new worker claims, cancel or drain active work and disable the Stories plugin before reverting `PHASE2_ENGINE` to `legacy`. Retain all new tables and external Docs. Never restore an old database over newer execution state as a code rollback. Ensure legacy pending discovery cannot redispatch migrated pending projects before enabling it.

Monitor blocked runs, expired leases, repeated integration failures, notification attempts, token totals and Google render conflicts. Service logs use bounded diagnostics and IDs; do not log arbitrary provider payloads, Telegram token URLs or OAuth credentials. `/story status` and dashboard cards expose operation state and links. Run `python3 -m pytest -q` and `npm test` before rollout.
