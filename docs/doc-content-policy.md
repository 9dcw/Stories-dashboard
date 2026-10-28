# Visual narrative documents

New narrative documents use the legacy Phase 3 four-column native table: Visual | Talk track (bullet points) | Production / evidence notes | Collateral in Drive / how to obtain it and original owner. Aim for 6–10 beats, at most 20; cells target <=150 words and whole documents <=2,500 words. Keep producer guidance verbatim above the table. Never truncate guidance, evidence, qualifications or asset receipts to meet a cap; necessary excess requires a visible human-review exception. No separate model review stage is required: publication verifies native structure/readback and records deterministic policy checks with Doc ID, revision and counts. Human annotations remain protected.

The following three-column policy continues to apply to legacy pinned package jobs.

# Google Docs editorial tables (tables.v1)

Every research Doc uses a native Google Docs table: Topic | At a glance | Detail / evidence. Short cells target one sentence, at most 25 words. Details add evidence or actions rather than repeating the short version. No nested tables, logs, raw JSON, search diaries, superseded drafts or routine proof-of-work belong in the published table.

| Doc | Soft word cap | Detail words per cell | Rows / topics |
| --- | ---: | ---: | --- |
| Story Hub | 500 | 60 | Story; Current assessment; Progress; Blockers; Next decision |
| Narrative Angles | 1,500 | 120 | Central finding; People and stakes; What happened; Mechanism; Outcome; Contradictions; Research gaps |
| Research Plan | 1,500 | 120 | Central question; Primary records; Fact validation; Visual research; Context; Contradictions; Next actions |
| Evidence & Sources | 1,500 | 120 | Same seven topics as Narrative Angles |
| Video Treatment | 1,500 | 150 | Opening; Setup; Escalation; Turn; Explanation; Payoff; Closing |
| Visual Assets | 800 | 80 | Up to eight priority assets, including essential missing visuals |
| Quality Review | 700 | 80 | Aim for six grouped findings; include all critical/major issues plus a compact content-check row |

The executable source of truth is `phase2/doc_policy.py`. Workflow snapshots pin this policy in generation prompts and schemas. Complete structured evidence remains in SQLite `artifact_versions.output`; publishing selects only editorial_rows. Claims, conflicting evidence, qualifications, citations and rights restrictions must survive synthesis. Semantic review checks this; word counting alone cannot establish evidence completeness.

The complete rendered Doc, including navigation and retained human notes or historical revisions, counts toward the cap. A URL counts as one word. Necessary excess requires a specific policy_exception, displayed prominently as requiring human review. Content is never silently truncated. Exceptions do not grant publication approval.

Quality reviews read current Google Docs, including table cells, and retain actual Doc IDs/revisions, total/managed counts, policy version and deterministic findings in policy_checks. The reviewer must discuss those findings and check editorial content. Readback failure prevents completion. Legacy free-form Docs are flagged on their next review. Annotated Docs get proposed revisions; human edits/comments remain protected. Explicit appended revisions cannot evade the whole-document cap.

Existing completed Docs are not rewritten during deployment. New runs and requested revisions use tables. Runs queued by older API code receive the current publication contract at worker execution; the original workflow hash is retained and the effective contract is hashed in provenance. Research engines and budgets stay pinned. Legacy input snapshots must be retried or newly requested; output checkpoints lacking editorial_rows need a fresh revision, rather than being reformatted by dropping evidence.
