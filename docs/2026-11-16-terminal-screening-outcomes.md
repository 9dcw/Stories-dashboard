# Terminal screening outcomes

The user requested that documents be processed or marked failed, with no deferred backlog.

- Unscreened active court candidates get one ordinary full-document screening attempt.
- A model/extraction exception or uncertain classification becomes `court_screenings.decision=ERROR`, `terminal_failure=true`, `retry_policy=manual_only`, with its reason, evidence, and available content retained. The summary status is FAILED. Such rows are excluded from automatic selection even if policy versions change.
- Already attempted uncertain outcomes can be closed using `scripts/finalize_court_failures.py --db ... --apply`. The command takes the existing daily lock and archives the original screening and summary status in `court_failure_audit` before making changes. Dry-run is the default. Promoted, dismissed, ignored and inactive-source items are untouched.
- Excluded opinions without a generated gist receive SKIPPED rather than misleadingly remaining PENDING. Existing generated text is preserved by migration.
- A configured processing cap gives remaining scoped items explicit RUN_LIMIT_REACHED failures; it does not silently defer them. Normal daily runs have no cap. Do not use a zero-limit pipeline run for collection diagnostics; use `fetch_listing`/`audit_source` without mutating production.
- Runs with failures complete as COMPLETE_WITH_FAILURES, with pending=0. Actual unhandled run/process interruption remains an interruption, never a claim of completed screening.
- Failed documents appear in Problems. Retrying requires an explicit repair/reset; no scheduled retry loop is retained.

The release is based on the exact step-2 production runner. Database backup, deployment checksums, prior runner/image identifiers and outcome counts are saved alongside the deployment. Do not roll back by restoring an entire database over subsequent user activity.
