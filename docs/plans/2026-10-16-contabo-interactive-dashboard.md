# Contabo Interactive Stories Dashboard — Implementation Plan

**Goal:** Replace the Telegram round trip for routine story review with a secure, phone-first, same-origin web application while preserving SQLite as the authoritative database and GitHub Pages as a read-only fallback.

**Architecture:** A single FastAPI process serves the existing static frontend and authenticated JSON API on Contabo. Google Identity Services supplies a Google ID token; the server verifies it and authorizes one configured email plus immutable Google subject. SQLite remains authoritative. Interactive dismiss/restore operations are short transactions with immutable feedback events. Caddy terminates TLS. Docker Compose runs Caddy and one API worker. GitHub Actions tests/builds and later deploys immutable images after a protected-environment approval.

**Constraints:**

- Never mutate the tracked production database from tests.
- Preserve existing CLI, polling, summarization, source discovery, exporter, and GitHub Pages behavior.
- No Google service-account key is required or stored.
- Do not deploy until the final domain, OAuth Web client ID, allowed Google subject, and Contabo deployment values are configured.
- Use test-driven development: each behavioral change begins with a failing test.

## Task 1 — Immutable dismissal feedback and safe SQLite behavior

**Files:**

- Modify: `schema.sql`, `story_store.py`, `scripts/story_cli.py`
- Add: `tests/test_dismissal_feedback.py`

**Schema:** Add `item_feedback_events` with `DISMISS`/`RESTORE` action, reason, optional note, prior/resulting status, actor subject/email, idempotency request ID, optional reverted event, and timestamp. Add indexes and triggers rejecting event updates/deletes.

**Store contracts:**

- `dismiss_item(item_id, reason_code, note, actor_sub, actor_email, request_id)`
- `restore_item(item_id, dismissal_feedback_id, actor_sub, actor_email, request_id)`
- `list_item_feedback(item_id)`
- `get_active_dismissal(item_id)`

Use `BEGIN IMMEDIATE`. Exact retries with the same request ID return the original result; request-ID reuse with different content conflicts. Only `NEW` candidates can be interactively dismissed, and only the active matching dismissal can be restored. The original event is never edited.

Route CLI `update-status ... DISMISSED` through the event path with a local CLI actor while preserving command syntax.

**Tests:** append and state projection, restore event, immutability, idempotency, request conflict, illegal state transitions, unknown item, and migration of an existing database without changing IDs/statuses.

## Task 2 — Reusable read model and production-safe configuration

**Files:**

- Modify: `export_stories.py`, `.env.example`, `.gitignore`
- Add: `api/__init__.py`, `api/config.py`, `api/read_model.py`, `requirements.txt`, `requirements-dev.txt`, `tests/test_runtime_config.py`

Extract `build_read_model(store, generated_at=None)` from the exporter. `export_json()` must remain deterministic and retain its current contract. The live model may expose an active dismissal without actor identity.

Configuration variables:

- `STORIES_DB_PATH`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_ALLOWED_EMAIL`
- `GOOGLE_ALLOWED_SUB`
- `SESSION_SECRET`
- `PUBLIC_ORIGIN`
- `SESSION_MAX_AGE_SECONDS`

Production startup fails closed for missing/placeholders. Tests must reject the repository production DB path.

## Task 3 — Authenticated FastAPI application

**Files:**

- Add: `api/auth.py`, `api/models.py`, `api/main.py`
- Add: `tests/test_api_auth.py`, `tests/test_api_dashboard.py`, `tests/test_api_feedback.py`, `tests/test_api_security.py`

**Public endpoints:**

- `GET /api/v1/health`
- `GET /api/v1/config`
- `POST /api/v1/auth/google`

**Session endpoints:**

- `GET /api/v1/auth/session`
- `POST /api/v1/auth/logout`

**Authenticated endpoints:**

- `GET /api/v1/dashboard`
- `GET /api/v1/items/{item_id}/feedback`
- `POST /api/v1/items/{item_id}/dismiss`
- `POST /api/v1/items/{item_id}/restore`

Verify Google token signature, audience, issuer, expiry, email verification, exact configured email, and exact configured subject. Set a short-lived `HttpOnly; Secure; SameSite=Strict` signed session cookie. State-changing requests require same-origin `Origin` and `X-CSRF-Token`. No CORS. Disable docs in production. Never log Google credentials or expose actor identifiers.

**Tests:** valid/invalid identity, wrong email/sub, cookie flags, tampered/expired session, missing/bad CSRF, wrong origin, unauthenticated API, exact mutation responses, idempotent retry, and live dashboard visibility.

## Task 4 — Phone-first interactive frontend

**Files:**

- Modify: `index.html`, `app.js`, `styles.css`, `sw.js`, `manifest.json`
- Add: `package.json`, `tests/js/app.test.js`, `tests/js/service-worker.test.js`

Support two explicit modes:

- Contabo interactive mode from `/api/v1/config`, requiring login and loading `/api/v1/dashboard`.
- GitHub Pages static mode loading `./data/stories.json` and retaining read-only/manual Telegram links.

Interactive candidate flow:

1. Tap a thumb-friendly Dismiss button.
2. Choose a reason (`not_relevant`, `duplicate`, `too_old`, `weak_source`, `other`).
3. Optionally add a bounded note.
4. Submit one request with a generated UUID.
5. Remove the card only after success.
6. Show an Undo snackbar linked to the exact dismissal event.
7. Show a persistent Restore button under “Show dismissed.”
8. On failure, retain the card and show a retryable error.

Never queue writes offline. A 401 returns to the login gate; it must not silently fall back to static data.

Update the service worker so every `/api/` request is network-only and API failures never return cached HTML.

## Task 5 — Container, Caddy, backups, and CI/CD

**Files:**

- Add: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `deploy/Caddyfile`, `deploy/entrypoint.sh`, `deploy/README.md`, `scripts/backup_db.py`
- Add: `.github/workflows/test.yml`, `.github/workflows/deploy-contabo.yml`
- Retain: `.github/workflows/pages.yml`

The image runs as a non-root user, excludes `story-data/`, `.env`, `.git`, and generated production data, and starts one Uvicorn worker. Compose mounts the persistent DB from the host and does not publish Uvicorn directly. Caddy owns ports 80/443, automatic TLS, proxying, compression, and security headers/CSP.

Before migrations/deployment, create a timestamped online SQLite backup outside the image, run integrity and foreign-key checks, and compare row/status counts. Never copy a database from the image into persistent storage.

CI runs Python/JS tests, builds the image, verifies secrets/database are absent from layers, and smoke-tests a temporary database. Deployment builds a SHA-tagged GHCR image, uses a restricted SSH deploy identity and pinned host key, backs up the DB, updates only the image reference, health-checks HTTPS, and rolls code back on failure. Production uses a protected GitHub Environment requiring phone approval.

## Task 6 — Verification and controlled cutover

Before opening the PR:

- Run all Python and JavaScript tests.
- Build and smoke-test the container with a temporary DB.
- Verify existing export is deterministic.
- Verify the tracked database has not changed unexpectedly.
- Perform spec review, security/code-quality review, and integration review.

Production cutover remains blocked until these are supplied/configured:

- Final domain and DNS pointed at Contabo
- Ports 80/443 available
- Google OAuth Web client ID authorized for the exact HTTPS origin
- Allowed Google immutable `sub`
- Strong session secret stored only on Contabo
- Restricted deployment user/key, pinned host fingerprint, deploy/data/backup paths
- Brief initial writer pause for backup and migration

After cutover, verify on the phone: Google login, dismiss, undo, persistent restore, logout, unauthenticated rejection, TLS, and read-only GitHub Pages fallback.
