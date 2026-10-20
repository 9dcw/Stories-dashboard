# Daily court-opinion intake

The court runner is `scripts/court_daily.py`. It polls active Lane B sources only.
Non-court sources remain disabled. This is the single scheduled ingestion entry
point; the legacy `daily_refresh.py` and workbook finders are not scheduled by
this change. No emails, Telegram messages, or promotions are sent by this runner.

## Schedule and operations

Production runs daily at **07:00 Europe/Berlin**, including weekends, using the
host user's crontab. The host timezone must remain Europe/Berlin; the installer
checks it. A database-adjacent advisory lock prevents overlapping runs, including
manual starts. Every source and every document failure is isolated. Failed
screenings retry on the next run. Successful screenings reuse cached full text
and are not repeatedly sent to the model. Change `RULE_VERSION` to re-screen
cached documents after a rule change. Revised URLs are ingested as new items;
updates at an unchanged URL are not currently rediscovered automatically.

The dashboard shows the configured schedule, next run, last run state, counts,
and per-source opinion extraction counts. `EMPTY` is visible and makes a run
partial: it may mean no publication, a broken collector, or a changed court page.
It is never treated as evidence of complete court coverage. `RUNNING` remains
visible during a run; a subsequent locked run marks an unfinished predecessor
`INTERRUPTED`. Monitor the host log for failures before the Python runner starts.

## Exclusion policy

There is no insurance-relevance gate, editorial score, age cutoff, missing-date
rejection, or requirement to prove precedent. All other subjects pass.

1. Deterministic case-insensitive matching of this document's own publication
   status notice on page one or an explicit trailing publication-title status:
   nonprecedential, not precedential, unpublished, not for publication, not
   recommended for full-text publication, summary order, and the explicit Fifth
   and Tenth Circuit publication/precedent footnotes. Citation mentions do not
   establish the current opinion's status.
2. Immigration proceedings as the central subject.
3. Violent-crime prosecution, conviction, sentencing, or collateral challenge as
   the central subject. Civil liability, coverage, tort, and civil-rights cases
   involving violence pass. Incidental mentions and uncertainty pass.

The configured cheap Hermes gist model handles subject screening and factual
gisting together, with no tools enabled. Full PDFs are extracted with page
labels. Long opinions are read in complete chunks; an exclusion requires
unanimity across chunks, no uncertainty, and page-verified exact quotations.
Chunk disagreements, unsupported exclusion evidence, and uncertainty pass for
review. Long summaries receive a final synthesis from all partial summaries.
Document/model errors remain visible and are retried, not excluded.

`court_screenings` is independent of editorial item status. Excluded opinions
remain stored with reason, rule version, time, document hash, and evidence. The
screening filter offers retained, all, excluded, needs review, and not screened.
Default view includes pending/failed summaries so they cannot silently disappear.

## Sources and validation

Run `python3 scripts/configure_court_sources.py --db /path/to/stories.sqlite3`
to update the 13 existing court sources without enabling other sources. The
registry is deliberately source-specific: menus and manuals are not opinion
links. Sources use recent official feeds/listings, so initial intake is limited
to what each court currently exposes; this is not a historical backfill. The
Second Circuit adapter submits a 30-day date search, and the Seventh uses a
monthly search. The Eighth and Tenth feeds are especially short-lived: daily
polling is not a guarantee of complete recovery after an outage.

The checked-in `samples/court-source-audit-2026-10-20.json` records all 13 listing
tests and a full-document sample per nonempty source. This is a smoke audit,
not an exhaustive labeled recall benchmark. Keep inspecting excluded samples;
model subject classification can still make mistakes even with correct quotes.

Manual run (use the same configured environment as the scheduled wrapper):

```
python3 scripts/court_daily.py --db /srv/stories-dashboard/story-data/stories.sqlite3
```

For a bounded check use `--limit 10`; `--no-poll` processes existing inventory.
The runner defaults to the existing Hermes gist adapter and model. Environment
variables `STORY_GIST_COMMAND`, `STORY_GIST_MODEL`, and `STORY_GIST_PROVIDER`
allow an explicit deployment configuration. The production wrapper sets
`STORY_GIST_TOOLSETS=none`, so the classifier cannot browse or write externally.
