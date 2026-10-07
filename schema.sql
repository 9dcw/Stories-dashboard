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
    notes TEXT NOT NULL DEFAULT ''
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
CREATE INDEX IF NOT EXISTS idx_story_projects_updated ON story_projects(updated_at DESC);
