-- Peach pipeline SQLite / libSQL / Turso schema (namevids queue only)

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  description TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS upload_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL CHECK (provider IN ('namevids', 'vidara', 'doodstream')),
  name TEXT NOT NULL DEFAULT 'default',
  login TEXT,
  secret TEXT NOT NULL,
  is_enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  metadata TEXT NOT NULL DEFAULT '{}',
  last_used_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (provider, name)
);

CREATE INDEX IF NOT EXISTS upload_accounts_pick_idx
  ON upload_accounts (provider, is_enabled, priority, id);

CREATE TABLE IF NOT EXISTS parser_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  source_origin TEXT NOT NULL DEFAULT 'https://a.ebalka.love',
  current_page INTEGER,
  total_pages INTEGER,
  backlog_complete INTEGER NOT NULL DEFAULT 0,
  hiden_current_page INTEGER,
  hiden_total_pages INTEGER,
  hiden_complete INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO parser_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  video_path TEXT NOT NULL,
  preview_path TEXT,
  poster_path TEXT,
  duration_seconds INTEGER,
  title_ru TEXT NOT NULL DEFAULT '',
  description_ru TEXT NOT NULL DEFAULT '',
  title_en TEXT NOT NULL DEFAULT '',
  description_en TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  category_en TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  tags_en TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'parsed'
    CHECK (status IN ('parsed', 'uploading', 'published', 'failed', 'skipped')),
  pre_filecode TEXT,
  full_filecode TEXT,
  namevids_file_id TEXT,
  namevids_account_id INTEGER REFERENCES upload_accounts (id),
  vidara_account_id INTEGER REFERENCES upload_accounts (id),
  host_provider TEXT NOT NULL DEFAULT 'vidara',
  dood_expires_at TEXT,
  error_message TEXT,
  parsed_at TEXT NOT NULL DEFAULT (datetime('now')),
  published_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS videos_status_idx ON videos (status, parsed_at);
CREATE INDEX IF NOT EXISTS videos_published_idx ON videos (published_at DESC);

INSERT OR IGNORE INTO app_settings (key, value, description) VALUES
  ('ebalka.source_origin', 'https://a.ebalka.love', 'ebalka mirror'),
  ('parser.primary_source', 'ebalka', 'Catalog source (ebalka only)'),
  ('namevids.caption_link', 'https://telegram.dog/jesovixxx', 'Caption TG link'),
  ('caption.template', '#{tags}\n▶ {link}', 'Legacy caption template');
