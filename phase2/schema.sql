CREATE TABLE IF NOT EXISTS story_workspaces (
 workspace_id TEXT PRIMARY KEY, root_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'provisioning', resources TEXT NOT NULL DEFAULT '{}',
 chat_id TEXT, thread_id TEXT, first_message_id TEXT,
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
 UNIQUE(chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS workflow_runs (
 run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES story_workspaces,
 story_id INTEGER NOT NULL REFERENCES story_projects, request_id TEXT NOT NULL UNIQUE,
 operation TEXT NOT NULL, target TEXT NOT NULL, instruction TEXT NOT NULL,
 requester TEXT NOT NULL, manifest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 notifications TEXT NOT NULL DEFAULT '[]', tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES story_workspaces,
 artifact_key TEXT NOT NULL, doc_id TEXT UNIQUE, current_version TEXT,
 stale INTEGER NOT NULL DEFAULT 0, render_state TEXT NOT NULL DEFAULT 'empty',
 baseline TEXT NOT NULL DEFAULT '', revision_id TEXT NOT NULL DEFAULT '',
 UNIQUE(workspace_id, artifact_key)
);
CREATE TABLE IF NOT EXISTS step_executions (
 step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs,
 step_key TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 due REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, fence TEXT NOT NULL DEFAULT '',
 inputs TEXT, output_version TEXT, error TEXT NOT NULL DEFAULT '',
 started_at REAL, finished_at REAL, usage TEXT NOT NULL DEFAULT '{}',
 UNIQUE(run_id,step_key,attempt)
);
CREATE TABLE IF NOT EXISTS artifact_versions (
 version_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts,
 version_number INTEGER NOT NULL, step_id TEXT NOT NULL UNIQUE REFERENCES step_executions,
 output TEXT NOT NULL, inputs TEXT NOT NULL, provenance TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'candidate', proposal_doc_id TEXT,
 created_at REAL NOT NULL, UNIQUE(artifact_id,version_number)
);
CREATE TABLE IF NOT EXISTS editorial_decisions (
 decision_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES story_workspaces,
 request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, text TEXT NOT NULL,
 author TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
 supersedes TEXT REFERENCES editorial_decisions
);
CREATE INDEX IF NOT EXISTS idx_steps_due ON step_executions(state,due);
CREATE TABLE IF NOT EXISTS package_assessments (
 step_id TEXT PRIMARY KEY REFERENCES step_executions,
 assessment TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_workspace ON workflow_runs(workspace_id,created_at);

CREATE TABLE IF NOT EXISTS narrative_prompt_versions (
 version INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
 feedback TEXT NOT NULL, prompt TEXT NOT NULL, author TEXT NOT NULL, created_at REAL NOT NULL
);
