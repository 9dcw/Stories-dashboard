# Incremental appellate court expansion

The coverage inventory is `data/court_inventory.json`, shown under Sources and
Discovery review, and available with `scripts/court_inventory.py --db PATH`.
Use `--outstanding` to list only courts without an active source. The CLI opens
SQLite read-only. Dashboard status is derived from live source activation, not
from a manually maintained completion count. Poll failures remain separate from
activation: active is not a claim of healthy or complete coverage.

## Counting and scope

The baseline contains 112 court families: 13 existing federal appellate courts,
53 state/D.C. highest courts (including the separate Texas and Oklahoma criminal
high courts), 45 intermediate appellate families, and the U.S. Supreme Court.
There are 99 expansion targets in this initial inventory. A family may contain
several districts/divisions; the denominator is **not** an exhaustive count of
individual courts. All 51 state/D.C. structure audits remain open. Check district
counts, reorganizations, lower-court appellate divisions, specialized appellate
courts, and whether there is no intermediate court. Add verified children with
stable IDs and stop counting parent families when replacing them with children.
Never treat absence from the directory as proof that a court does not exist.
Territories are outside this initial scope.

Seed names and URLs derive from Free Law Project courts-db, pinned in the JSON;
its BSD license is preserved in `data/court-directory-LICENSE.txt`. Seed URLs are
unvalidated and may be stale; they are not registered as polling sources.

A source can map to several `court_ids`; several feeds can map to one court.
A court counts active only once. This is feed availability, not exhaustive
publication coverage; the scope column records known gaps. Pilot/outstanding
status is independent of operational activation. Disabled feeds remain outstanding.

## Batch order and acceptance

1. Foundation + pilot: inventory, reporting, exact host allowlists; U.S. Supreme
   Court current-term opinions of the Court and Maine published opinions.
2. Diverse adapter pilot: Washington shared Supreme/Appeals listing, New York
   official RSS, and a structured/search portal selected after inspection.
   Validate both highest and intermediate court attribution. The initial raw
   Washington response did not expose opinion links: do not activate it yet.
3. Simple official feeds/listings: around five state systems per batch, selected
   from source research. Audit state structure as each state enters a batch.
4. Session/search/JavaScript systems: small individual batches; evaluate an
   alternative provider where needed, documenting credentials and limitations.
5. Coverage audit: compare a publication window against official listings,
   record omissions and recovery windows; revisit every partially covered court.

Each batch must record listing URL, host allowlist, document path, court IDs,
publication scope, observed link counts, full-text samples/hashes, exclusions
sample review, duplicate behavior, recovery limitations, and expected initial
volume. Run relevant tests before activation. Activate independent validated
sources as ready; no all-states activation gate. A source smoke test is not a
recall audit. Source adapters must not change `court_exclusions_v2`.

## Pilot operations

Run `scripts/configure_court_pilot.py --audit-output PATH` for a non-mutating
listing/full-text audit. With `--db PATH`, validated sources are registered
without enabling previously disabled sources. `--activate` enables validated
sources individually, even if another pilot source fails. Re-running does not
create duplicates. An empty listing defers activation pending a document sample.

The Supreme Court source rotates to the current October term during each fetch.
It excludes order lists, opinions relating to orders, and in-chambers opinions;
these remain explicit outstanding scope. Prior-term outage recovery is pending.
Maine's current listing exposes approximately 105 documents at pilot time; all
are eligible on first intake under the existing no-age-cutoff policy. This can
create an initial processing backlog. No archive crawl is performed. Neither
source provides guaranteed recovery for decisions removed from its listing.
Full-document fetch failures remain visible and retry using the existing runner.

No changes to criminal, immigration, civil-rights/employment, publication-status,
financial-story exceptions, uncertainty handling, or existing editorial statuses.

## Florida, Texas, California and New York batch (October 29, 2026)

This batch uses official court sources only. No CourtListener sources or
credentials are used. Per-source listing counts, full-text hashes, extraction
failures and timestamps are in `samples/state-court-audit-2026-10-29.json`.

- Florida: Supreme Court plus all six District Courts of Appeal, using the
  official `flcourts-media.flcourts.gov` opinion API. Fetch every page in a
  rolling 30-day discovery window, including all opinion-document types. A
  missing document or truncated pagination is an error, not a silent omission.
- Texas: Supreme Court, Court of Criminal Appeals, and all fifteen Courts of
  Appeals. Use each official release calendar, follow its dated opinion/handdown
  pages in a rolling 30-day window, and extract opinion-document links. Repair
  the official template's literal `coa` server variable using the audited court
  code, preserving both media identifiers. Routine order documents are outside
  these opinion sources. The criminal court has a separate handdown layout.
  The calendar exposes the current quarter; recovery across a quarter boundary
  remains outstanding. The Fifteenth Court of Appeals is included.
- California: Supreme Court and six Court of Appeal districts. The Fourth
  District uses three separately filtered division feeds, so seven courts use
  nine feeds. Parse case titles/dates/PDFs from the official published/citable
  listing and follow all result pages. Unpublished and superior-court appellate
  division listings are outside this batch; no archive crawl is performed.
- New York: Court of Appeals (the state's highest court), four Appellate
  Division departments, and two Appellate Term department entries are inventoried
  as `blocked_access`. Official RSS and opinion/search endpoints returned HTTP
  403 from this host. The user will obtain approved access/a key later. None of
  these feeds is activated, and no alternate provider is used. Appellate Term
  subdivisions still require structure verification after access is arranged.

Official structure references:

- https://www.flcourts.gov/Courts-System/District-Courts-of-Appeal
- https://www.txcourts.gov/about-texas-courts/courts-of-appeals/
- https://appellate.courts.ca.gov/
- https://www.nycourts.gov/reporter/RSS.shtml

The inventory expands the prior intermediate families into individual districts
and adds New York's Appellate Terms. It now contains 141 entries; the denominator
still mixes individual courts in audited states with families in other states.
There are 48 remaining state/D.C. structure audits. These are discovery/coverage
boundaries, not new editorial exclusions. `court_exclusions_v2` and its prompt
are unchanged.

Audit without production writes:

```
python scripts/configure_state_courts.py --audit-output /tmp/audit.json
```

Use `--state FL`, `--state TX`, `--state CA`, or `--court COURT_ID` for independent
batches. With `--db PATH --activate`, each validated source is enabled
independently. Reuse requires a same-day audit and exact source-definition hash.
A failure in one source does not prevent activation of the others. Empty feeds
remain pending until a full-text sample can be validated. The initial exposed
listings contain over two thousand documents; initial intake can create a
substantial screening backlog. Existing full-text screening, retries, review
handling, and editorial statuses are preserved.

The daily runner now accepts repeatable `--source ID` alongside `--limit N`, so
bounded source-specific checks can use the production lock and unchanged rules
without polling or screening unrelated sources. The scheduled default still
polls all active Lane B sources.
