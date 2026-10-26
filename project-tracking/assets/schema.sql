CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL DEFAULT 'unknown',
  goal TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  summary TEXT,
  branch TEXT,
  git_head_start TEXT,
  git_head_end TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'todo'
    CHECK (status IN ('todo','in_progress','done','blocked','abandoned')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Content-addressed snapshots. content is NULL for binary/oversized files (hash only).
CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  content BLOB
);

-- Last known state of every tracked file; the baseline for diffing and reconciling.
CREATE TABLE IF NOT EXISTS file_states (
  path TEXT PRIMARY KEY,
  sha256 TEXT,
  size INTEGER,
  mtime REAL,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  task_id INTEGER REFERENCES tasks(id),
  ts TEXT NOT NULL,
  path TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('create','modify','delete')),
  source TEXT NOT NULL CHECK (source IN ('agent','external')),
  before_sha TEXT,
  after_sha TEXT,
  lines_added INTEGER NOT NULL DEFAULT 0,
  lines_removed INTEGER NOT NULL DEFAULT 0,
  diff TEXT,
  rationale TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_path ON changes(path);
CREATE INDEX IF NOT EXISTS idx_changes_session ON changes(session_id);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  task_id INTEGER REFERENCES tasks(id),
  ts TEXT NOT NULL,
  title TEXT NOT NULL,
  rationale TEXT NOT NULL,
  alternatives TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  ts TEXT NOT NULL,
  name TEXT NOT NULL,
  note TEXT,
  git_head TEXT,
  git_dirty INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoint_files (
  checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (checkpoint_id, path)
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  task_id INTEGER REFERENCES tasks(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  command TEXT,
  status TEXT NOT NULL CHECK (status IN ('pass','fail','skipped')),
  exit_code INTEGER,
  output_tail TEXT
);

CREATE TABLE IF NOT EXISTS reconcile_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_id INTEGER,
  git_head_before TEXT,
  git_head_after TEXT,
  commits TEXT,
  created INTEGER NOT NULL DEFAULT 0,
  modified INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0
);
