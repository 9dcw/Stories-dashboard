The court candidate feed requires documented insurance-company involvement in
addition to the existing subject exclusions. The company may participate in the
underlying claim or dispute without appearing in the caption. Hypothetical
coverage, an injury alone, or an insurer appearing only in a cited case does not
qualify. Every segment of a saved opinion is read; positive involvement requires
a source quotation verified against its page. Uncertainty is excluded for review
and retried by the daily pipeline. Promoted stories and editorial dismissals are
preserved.

`insurance_screening.py` implements policy `insurance_company_v1`. The original
subject-screening version remains separate so previously excluded opinions do
not require another model call solely for this added gate.

To audit existing NEW/PASS candidates, run:

```sh
python scripts/screen_insurance_candidates.py --db /path/stories.sqlite3 --audit /path/audit.jsonl
```

The command reads full cached opinions and writes resumable, evidence-checked
classifications without changing the database. Re-run it if any segments are
incomplete. After a verified SQLite backup, use the same arguments plus `--apply`.
Application refuses incomplete or stale results, takes the court runner lock,
and saves original screening rows to `audit.rollback.json` before committing.
Unreadable existing court candidates are withheld as uncertain, with their prior
diagnostics preserved. No item, document, summary, or story project is deleted.

The change only affects court candidates. Review exclusions with the app's
Advanced > Opinion screening > Excluded filter. Restore a mistaken classification
by reviewing the recorded source evidence and updating its court screening;
the original rows in the rollback file provide a precise reversal record.
