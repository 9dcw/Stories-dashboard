# Source-discovery prompt

Run `python3 scripts/source_discovery.py prompt --target-limit 10` to produce the compact prompt for the next rotation batch. The external search/model adapter must return only a JSON array matching `discovery/pilot_sources.json` fields. Save that response and run:

```bash
python3 scripts/source_discovery.py run --input discoveries.json
```

Discovery is deliberately separate from `scripts/daily_refresh.py`. It rotates target rows in SQLite, validates each proposed listing with the existing RSS/Atom/HTML/JSON collector, stores compact diagnostics, and never enrolls automatically. Review with `source-proposals`, approve explicitly, then enroll.

Validation statuses include `VALID`, `NO_PUBLICATIONS`, `ARCHIVE_OR_NAVIGATION`, and `FAILED`; low publication volume is not by itself a rejection. A source that needs a collector capability not currently implemented should remain proposed with a failed/unsupported result rather than being silently discarded.
