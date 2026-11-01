# Originating courts

The issuing court remains in the source registry. `court_origin_identity` separately records courts named in an opinion's origin caption, including Supreme Court review of intermediate appellate courts. Records contain a stable internal court ID, normalized name, jurisdiction, court level, relationships, original evidence text and page, extraction version, and an explicit identified/unknown status.

`reviewed_court` means the court whose decision is directly under review. `trial_court` means the identified trial origin. A direct appeal from a trial court has both relationships; a Supreme Court opinion may identify an intermediate reviewed court and a separate trial court. A county alone does not establish a trial court. Agencies are not classified as courts.

The initial extractor supports full-name federal district/bankruptcy origin captions, US Supreme Court certiorari captions naming federal circuits, Florida county/circuit captions, Florida Supreme Court review-origin endmatter, California superior-court captions and Supreme Court intermediate-court captions, and Texas numbered district/probate/county-court captions and Supreme Court petition-for-review captions. Other formats remain unknown. It deliberately does not resolve courts from narrative citations, judge names, or geographic inference. Florida court IDs currently describe the explicitly named county court unit; judicial-circuit-wide consolidation is not inferred.

Every newly extracted court document is tracked before editorial screening. Existing cached documents have a read-only fallback and can be persisted with:

```bash
python3 scripts/court_origins.py --db /path/to/stories.sqlite3 --backfill
```

The command uses cached text only, with no network requests, model calls, screening, promotion or changes to editorial decisions. It skips records already at the current extraction version. Omit `--backfill` for a report.

The live/export read model exposes per-document `originating_courts` on candidates and a top-level `originating_court_activity` list, including excluded, dismissed, and promoted documents in the activity totals. The list ranks observed documents, not all court filings or unique originating lawsuits. `identified_appellate_docket_count` deduplicates known federal appellate dockets; consolidated dockets can exceed document counts. `documents_without_appellate_identity` makes the remaining uncertainty explicit. Unknown origins are included in coverage totals rather than silently discarded. These observations support investigation of potential direct feeds; they do not enroll any feeds.
