# Three-court pilot — 16 November 2026

Authorized scope: Illinois First District, New Jersey Appellate Division, Pennsylvania Superior Court. No New York additions or changes to editorial screening.

## Collection

`step2_courts.specs()` defines the two retrievable official sources. `state_courts.fetch_listing` dispatches their adapters. Both use a rolling 30-day discovery window. Publication status is retained in the title; the existing deterministic nonprecedential rule and insurance-company screening remain unchanged.

Illinois: only First District records from the official decision table. Collect opinions and Rule 23 orders, excluding explicitly withdrawn rows without a document. Follow ASP.NET pagination with the current hidden form fields; stop once filing dates pass the window. Reject changed schema, wrong districts, unknown types, broken chronology, repeated pages, and page limits. Official document links redirect to the court's Azure storage, included explicitly in its host allowlist. Five districts are inventoried independently so enrollment cannot imply statewide coverage.

Pennsylvania: official public opinion API, `courtType=SUPERIOR`, all pages for the date window. Preserve individual opinions, memoranda, orders, separate concurrences/dissents, and revised documents. Exclude aggregate judgment lists and administrative tables. Preserve publication status. Reject wrong courts, unknown status, unsafe filenames, repeated records, changed totals, incomplete pagination, and page limits. Multiple documents in a case remain distinct evidence.

No archive backfill. Opinion-date coverage does not guarantee detecting later changes to older opinions, and outages longer than 30 days require deliberate recovery.

## Validation

Local audit: 80 Illinois and 283 Pennsylvania documents. Ten latest full-text PDFs per source extracted without failures, plus three precedential documents per source. Sample captions, courts, dates, publication notices, and full-page extraction checked. Illinois live page-two POST also verified. Focused collector, fetch-policy, pipeline, insurance-screening and inventory tests run before activation.

New Jersey: the official opinion listing, publication route, bare hostname, homepage, and candidate RSS/document routes return HTTP 403 from this machine; production also returns 403 for the official listings. No successful full-text audit; do not enroll or mark healthy. Update proposal 5 with current failure evidence. A separately authorized, attributable alternate source or restored official access is required before activation.

## Operations

Deploy a versioned runner derived from the exact currently deployed release, with a manifest of changed files. Back up SQLite with its backup API. Acquire the existing court-daily lock before activation and any pipeline run. Enroll only validated, scoped source proposals through the existing approval/enrollment API, with audit results attached. The user authorized enrollment by requesting step 2.

Run a bounded initial screen of only the two new feeds. Remaining documents flow through the existing daily schedule (07:00 Europe/Berlin). Two future scheduled cycles remain the acceptance observation period; manual fetch checks are not represented as scheduled-cycle success.

Rollback: restore the prior court-runner symlink and disable only the newly enrolled source IDs, retaining all audit and item records. Do not restore a whole database over subsequent user activity. For the app inventory overlay, restore its previous image using the saved deployment record.

## Repeatable audits

Use the existing audit command with the newly supported state scopes:

```bash
python3 scripts/configure_state_courts.py --state IL --state PA --audit-output activation/step2-reaudit.json
```

This command only audits unless `--db` is supplied. The production sources are already enrolled; do not repeat enrollment or run general source setup against production.
