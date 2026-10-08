PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    lane TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'web',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    collector_type TEXT NOT NULL DEFAULT '',
    poll_url TEXT NOT NULL DEFAULT '',
    collector_config TEXT NOT NULL DEFAULT '{}',
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(source_id),
    headline TEXT NOT NULL,
    raw_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    url_hash TEXT NOT NULL UNIQUE,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW', 'PROMOTED', 'IGNORED'))
);

CREATE TABLE IF NOT EXISTS story_projects (
    story_id INTEGER PRIMARY KEY,
    origin_item_id INTEGER NOT NULL UNIQUE REFERENCES items(item_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    google_doc_url TEXT NOT NULL DEFAULT '',
    telegram_thread_url TEXT NOT NULL DEFAULT '',
    research_folder_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE TABLE IF NOT EXISTS candidate_notes (
    item_id INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    display_headline TEXT NOT NULL DEFAULT '',
    gist TEXT NOT NULL DEFAULT '',
    summary_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (summary_status IN ('PENDING', 'COMPLETE', 'FAILED', 'SKIPPED')),
    summary_prompt_version TEXT NOT NULL DEFAULT '',
    summarized_at TEXT,
    extracted_char_count INTEGER,
    input_char_count INTEGER,
    extraction_method TEXT NOT NULL DEFAULT '',
    summary_error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_story_projects_updated ON story_projects(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_notes_status ON candidate_notes(summary_status);
