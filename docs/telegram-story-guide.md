# Story development commands

Use these commands inside the story's mapped Telegram topic:

| Command | Effect |
| --- | --- |
| `/story revise <guidance>` | Research, develop the visual narrative, or gather assets in the same story workspace. |
| `/story visuals build` | Read the current narrative table, build changed/missing visuals with up to two isolated agents, upload to Images, and add Drive links to the collateral cells. |
| `/story prompt update <feedback>` | Append versioned global guidance for future first drafts. Later feedback takes precedence. |
| `/story prompt show` | Display the full current initial-development prompt. |

Promotion creates the first narrative. The Google Doc contains your promotion and revision instructions verbatim, followed by the legacy four-column visual-script table. Asset files go in the existing Images folder; collateral cells link files and distinguish retrieval, owner and rights status. Revisions use the existing narrative and focused research, not a package rebuild.

Examples:

`/story revise Open on the dashcam. Find the original footage and clarify what it establishes.`

`/story prompt update Prefer a concrete visual opening and leave unresolved payment questions explicit.`

Prompt updates affect future first drafts only. Ordinary discussion does not change the narrative. Use the dashboard for progress and the story Doc for review. Human Doc edits/comments are protected; conflicting drafts remain saved for resolution.

When the narrative is ready, send `/story visuals build` in its story topic.
The command reads the live Google Doc, including your direct edits. Each row's
Visual, Production / evidence notes, and original Collateral text are saved
verbatim and hashed as its image instructions. Talk-track edits, row order, and
the generated asset receipt do not affect the hash. Keep the `[Visual asset: …]`
receipt when editing or moving a row: it is that visual's stable identity.
Change the image instructions to request a new version, then run the command
again. Unchanged instructions reuse the existing file without another agent or
generation call. Reverting instructions reuses the matching older version.
Previous files and instruction versions remain preserved; new files do not
overwrite them. The exact instructions and build prompt/source URL are also
stored in the Drive file description.

Original illustrations use the configured Hermes image provider. Source photos,
documents and screenshots are retrieved only from verified direct public URLs;
they are never replaced with generated evidence. Video, reference-image edits,
missing data and unclear requests remain visible production gaps in the
collateral cells. Generated images need human visual review; downloaded sources
remain rights uncleared. Narrative updates add clickable Drive links.

Successful rows survive failures. Rerun the same command to reuse them and resume
the rest. A timeout or crash during a paid generation, or a Drive upload with an
uncertain outcome, blocks a blind retry: reconcile the saved local result or Drive
file first to avoid paying twice. Resolve open Doc comments/suggestions before
building; concurrent edits to image instructions stop receipt publication and
preserve completed Drive files. Other narrative cells and human notes are retained.


Visual renderer configuration: readiness and generation resolve the same backend.
An explicit `PHASE2_IMAGE_PROVIDER` overrides the Hermes image setting; otherwise
the Hermes selection is honored. Without a selection, configured FAL is retained,
then available Codex/OpenAI backends are considered. Codex uses the workflow's
pinned chat model instead of the installed adapter's obsolete host model;
`PHASE2_IMAGE_HOST_MODEL` can explicitly override that host. These controls do not
change the image instructions or force a rebuild. Provider image references are
checkpointed before downloading, so download retries reuse the generated output.
Definite preflight failures and explicitly rejected HTTP requests can be retried;
timeouts with no known outcome still require reconciliation. Visual planners also
receive the narrative source registry, and previously skipped plans are reassessed
when that registry changes.


Phase 2 dashboard cards show the verified number of images, videos and PDFs in
that story's Images folder. Click the count to open the folder. During a visual
build, the card also shows how many current instruction rows have saved assets;
older image versions contribute to the folder total but never inflate current
build completion. Blocked rows and rows needing production input are shown
separately. Counts refresh after uploads and approximately every ten minutes for
manual Drive changes; the Phase 2 page checks for updated counts every minute.
Unchecked folders are not shown as empty. If Drive cannot be reached, the last
verified count remains visible with a refresh notice. A narrative revision hides
the previous build's completion fraction until visuals are built again.


Every `/story revise <direction or question>` now launches two isolated,
search-only agents before narrative drafting: one researches the producer's
direction and contrary evidence, and the other searches for dissenting and
concurring opinions in the same case. Both must actually perform web searches.
They retrieve primary sources, verify quoted passages and case identity, and
pass findings and unresolved access gaps to the narrative agent. The narrative
keeps the majority holding distinct from separate-opinion reasoning and records
additional relevant facts beside the affected beat. A failure to locate a
dissent is reported as not located after searching, never proof of unanimity.
Appended PDF opinions can be retrieved by page range. These requirements apply
to existing stories with pinned development prompts as well as new stories;
they do not regenerate visual assets or rewrite existing Docs on deployment.
