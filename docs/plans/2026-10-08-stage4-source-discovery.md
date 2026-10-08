# Stage 4 Automated Source Discovery and Coverage Expansion Implementation Plan

> **For Hermes:** Use this plan as the implementation checklist.

**Goal:** Add an independent, persistent source-discovery workflow that proposes validated recurring publication sources, supports explicit approval/enrollment, and exports compact review and coverage data to the existing dashboard.

**Architecture:** SQLite remains authoritative. New proposal and rotation tables live beside `sources`; discovery uses small rotating target batches and an injectable discovery input (JSON/search-adapter output), validates candidates through the existing collectors, and never loads the full registry into model context. Enrollment updates the existing `sources` registry idempotently. The existing daily polling path remains unchanged and independent.

**Tech Stack:** Python 3, SQLite, existing RSS/Atom/HTML/JSON collectors, argparse, fixture-backed pytest tests, generated JSON/GitHub Pages UI.

---

### Task 1: Add persistent discovery schema and store methods

**Files:** `schema.sql`, `story_store.py`, `tests/test_source_discovery.py`

Add `source_candidates`, `discovery_targets`, and `discovery_runs` tables with normalized URL uniqueness, proposal statuses, validation fields, rotation state, and poll-health counters. Add migrations for existing databases plus methods to create/list/update candidates, claim rotating targets, record runs, enroll approved candidates idempotently, and return coverage statistics. Test persistence, duplicate suppression, rejected-source suppression, and rerun-safe enrollment.

### Task 2: Implement deterministic source discovery and validation

**Files:** `source_discovery.py`, `scripts/source_discovery.py`, `tests/test_source_discovery.py`

Implement four lanes, target rotation, compact discovery prompt, JSON discovery input, URL normalization, registry/proposal checks, accessibility and collector extraction tests, headline/date/navigation diagnostics, and candidate proposal persistence. Keep validation independent of editorial relevance and isolate per-source failures. Test RSS, HTML, JSON, unsupported collector, archive-only, and current-publication cases with fixture fetchers.

### Task 3: Add discovery CLI review and approval operations

**Files:** `scripts/story_cli.py`, `scripts/source_discovery.py`, `README.md`

Add commands to list proposals, approve, reject, test, enroll, run discovery, show coverage, and print the prompt. Require explicit approval before enrollment. Enroll through the existing `sources` table and ensure a second enrollment returns the same source id.

### Task 4: Export source review and coverage read models

**Files:** `export_stories.py`, `tests/test_source_discovery.py`, `README.md`

Extend the generated JSON with compact `source_proposals` and `coverage` sections derived from SQLite. Include name, organization, jurisdiction, lane, URL, collector/publication type, validation status, explanation, and coverage health. Preserve existing story/candidate shape and deterministic export behavior.

### Task 5: Add dashboard review section

**Files:** `index.html`, `app.js`, `styles.css`

Add a separate read-only source-discovery review section showing proposal rows and coverage counters. Keep approval/enrollment CLI-only and avoid changing the existing story candidate behavior. Test with a generated fixture JSON and browser-level smoke checks where available.

### Task 6: Add initial 20-institution pilot and operational docs

**Files:** `discovery/pilot_sources.json`, `discovery/coverage_targets.json`, `discovery/source-discovery-prompt.md`, `README.md`, tests

Seed at least 20 distinct institutions across all four lanes and multiple jurisdictions. Run the pilot against the real network with bounded requests, persist compact results, and export the proposal queue for review. Document independent scheduling, failure isolation, CLI approval/enrollment, and the requirement not to backfill history.

### Task 7: Verify integration and regressions

Run focused Stage 4 tests, the complete test suite, a local approval/enrollment/poll/export flow, and the pilot command. Confirm newly enrolled sources use the existing polling path, discovery failures do not stop polling, and the dashboard JSON contains proposals and coverage without altering existing story data.
