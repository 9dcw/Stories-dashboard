# Directing a story after v1 in Telegram

Use `/story` commands in the story’s registered Telegram topic. The topic selects the story; you do not add a story ID. Send each command as a separate message, using the lowercase artifact names below.

Once the first research package lands, the working cycle is **read → save direction → request specific work → inspect the result → refresh affected documents**. Ordinary chat can help you discuss the package, but it does not save editorial decisions or launch changes.

## First, inspect v1

```text
/story status
/story read review
/story read evidence
/story read treatment
```

`status` shows operation state, document IDs, current versions, stale flags, and retained proposals. `read` returns the selected artifact’s published structured version, its stale flag, and its Google Doc link. It does not read unsaved or unimported edits from the live Doc.

| Artifact name | Document | Use it to direct… |
| --- | --- | --- |
| `angles` | 01 Narrative Angles | The central question, perspective, and narrative frame |
| `plan` | 02 Research Plan | Reporting priorities, sources to pursue, and gaps |
| `evidence` | 03 Evidence & Sources | Factual support, primary records, conflicting accounts |
| `treatment` | 04 Video Treatment | Story structure, scenes, and how findings are presented |
| `visuals` | 05 Visual Assets | Source-backed visual candidates and asset research |
| `review` | 06 Quality Review | Weaknesses, evidence gaps, and readiness for human review |

The 00 Story Hub is maintained by the system. It is not a target for research operations.

## Save direction versus commission work

**Decisions persist your editorial position. They do not launch research or rewrite the package.** They update the Hub and currently mark every existing research artifact potentially stale.

```text
/story decision approve Lead with the consequences for policyholders whose claims were delayed.
/story decision reject Drop the deliberate-fraud angle unless primary records support intent.
/story decision instruction Distinguish allegations, findings, and verified outcomes throughout.
/story decision question How many affected policyholders actually received payment?
/story decision note The claimant interview is a reporting lead; its factual claims still need verification.
```

| Decision | Meaning |
| --- | --- |
| `approve` | Endorse an editorial direction; this does not approve publication |
| `reject` | Record an angle or approach to avoid |
| `instruction` | Save a standing constraint for subsequent work |
| `question` | Record an unresolved question; follow with `investigate` to commission research |
| `note` | Save human context; it is not verified evidence |

Keep the returned `decision_id`. To replace a decision later, preserving its history and kind:

```text
/story supersede <decision-id> Focus on delayed payments rather than denied claims; the records do not establish denial.
```

Replace angle-bracket placeholders with actual IDs. Superseding also marks existing artifacts stale.

## Commission the next pass

| Command | What it queues |
| --- | --- |
| `/story investigate evidence <instruction>` | A scoped evidence investigation |
| `/story investigate visuals <instruction>` | A scoped visual-source investigation |
| `/story revise <artifact> <instruction>` | A directed revision of one research artifact |
| `/story refresh <artifact> [instruction]` | A new version of one artifact using current inputs and decisions |
| `/story review treatment [instruction]` | The `review` step; the target scopes its review context |
| `/story create <artifact> [instruction]` | Creation of a missing artifact; it does nothing if one already has a current version |

`revise` and `investigate` require instructions. Investigations accept only `evidence` or `visuals`. Other operations accept the six research artifact names. Reviewing generates a Quality Review; it does not itself rewrite the target document. Visual investigation researches assets; it is not a command to generate images or acquire rights.

Give the task a question, a source priority, and an acceptance criterion. For example:

```text
/story investigate evidence Find the underlying regulatory order and any subsequent compliance filings. Establish how many policyholders were owed money, how many were paid, and the dates covered. Separate amounts ordered from amounts actually paid. Cite the relevant passages and leave unavailable figures unresolved.
```

For a change to presentation:

```text
/story revise treatment Open with the documented consequence for a policyholder, then explain the mechanism. Use only supported scenes; list missing interview material as reporting needs.
```

For a visual follow-up:

```text
/story investigate visuals Find original public records and attributable visual sources that can explain the payment timeline. Record source URLs and rights uncertainties; do not assume public availability grants reuse rights.
```

## Refresh the documents affected by your changes

**Each post-v1 operation queues one research step. Refresh does not rebuild the whole dependency chain.** Updating an upstream artifact marks its descendants potentially stale. Wait for each upstream operation to finish before sending dependent work, then inspect `/story status`.

The dependencies are:

```text
angles → plan
angles + plan → evidence
plan → visuals
angles + evidence + visuals → treatment
angles + plan + evidence + visuals + treatment → review
```

### Example: close a factual gap without changing the angle

Send these one at a time, waiting for each operation to finish:

```text
/story investigate evidence Establish whether the ordered refunds were paid. Prioritize compliance filings and distinguish orders from completed payments.
/story read evidence
/story refresh treatment Reflect the new refund evidence and preserve any remaining uncertainty.
/story review treatment Check that payment claims match the evidence and unresolved gaps are visible.
/story status
```

Evidence changes affect treatment and review; they do not by themselves require a new plan or visual search. If you also record a new decision, every existing research artifact is marked stale, so account for those flags too.

### Example: change the story’s central angle

Save the direction, then work through the package in dependency order. Wait for each step to complete. Evidence and visuals can run alongside each other once the plan is complete; wait for both before treatment.

```text
/story decision instruction Center the story on the gap between a refund order and money reaching policyholders. Do not equate an order with payment.
/story revise angles Develop the refund-to-payment gap as the main angle and state what would disprove it.
/story refresh plan
/story investigate evidence Trace the order through compliance records and establish actual payments; identify missing records explicitly.
/story refresh visuals
/story refresh treatment
/story review treatment
/story status
```

Read the intermediate results and adjust instructions as needed. A completed operation means that requested work finished, not that the entire package is current, every fact is verified, or publication is approved.

## Google Doc edits and blocked work

Editing a Doc or leaving a comment does not automatically become a saved research instruction. Use `decision` for durable editorial direction and an explicit operation to request work.

```text
/story import evidence
```

`import` captures current Doc text as bounded human-authored notes. It does not verify factual additions or automatically resolve a rendering conflict. For a selected excerpt, use `/story decision note <text>` instead.

If a proposed revision conflicts with human edits or annotations, inspect the retained proposal through `status`. After reviewing it, you can explicitly adopt it:

```text
/story append <version-id>
```

Append adds a new managed revision while retaining older text and annotations. An outdated proposal is rejected. Use the proposal’s `version_id`, not a run ID or Google Doc ID.

| Situation | Action |
| --- | --- |
| Blocked run after a recoverable failure, with no retained candidate conflict | Fix the cause, then `/story retry` |
| Retained candidate conflicts with human edits | Inspect it, then explicitly append if appropriate; retry cannot override it |
| Work should stop | `/story cancel <run-id>`; completed documents and proposals remain |
| Wrong or ambiguous topic mapping | Have the mapping reconciled; the command cannot safely guess a story |

`retry` retries the latest blocked run; it is not a “do more research” command. `/story start` starts the initial package and returns the existing non-cancelled initial run when one already exists. Use scoped operations after v1.

Avoid resending a write command just because it takes time: a new Telegram message can queue another operation. Check `status` first. Save direction before launching dependent work; decisions or changed inputs arriving mid-run can block publication of that run’s result.

## Verification basis

Checked on 25 October 2026 against the worker service’s configured release `14d8058b6d0abf6ae1bdbd6ddf462f53235a4886`: `phase2/telegram.py`, `phase2/store.py`, `phase2/workflows/initial.v1.json`, and `docs/phase2-workflow.md`. This guide documents the implementation; no live research, Telegram messages, or document mutations were performed to verify it.

In Telegram, use `/story help` for the command reference or `/story help research` for post-v1 examples. Help is served by the gateway plugin without a research backend request.
