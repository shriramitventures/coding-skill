#!/usr/bin/env python3
"""project-tracking: a local, per-repository SQLite work log for AI coding agents.

Stdlib only. No network, no paid API. Never edits tracked source files; all state
lives in <repo>/.agent-tracker/tracker.db (override with TRACKER_HOME).

Usage: python tracker.py <command> [options]   (run with -h for the command list)
"""
import argparse
import difflib
import hashlib
import os
import sqlite3
import signal
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MAX_BLOB = 512 * 1024          # text files above this are hashed, not stored
DIFF_LINE_CAP = 2000           # truncate very long diffs
SKIP_DIRS = {".git", ".agent-tracker", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".idea", ".gradle", "target", ".mypy_cache", ".pytest_cache"}

SCHEMA = """
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
"""


# ----------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, cwd, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def find_root(start):
    p = run(["git", "rev-parse", "--show-toplevel"], start) if shutil_which("git") else None
    if p is not None and p.returncode == 0:
        return Path(p.stdout.strip())
    return Path(start).resolve()


def shutil_which(name):
    from shutil import which
    return which(name)


class Ctx:
    def __init__(self, root, home=None):
        self.root = Path(root).resolve()
        self.home = Path(home or os.environ.get("TRACKER_HOME") or self.root / ".agent-tracker")
        self.db_path = self.home / "tracker.db"
        self.is_git = (self.root / ".git").exists() and shutil_which("git") is not None

    def git(self, *args):
        if not self.is_git:
            return None
        p = run(["git", *args], self.root)
        return p.stdout.strip() if p.returncode == 0 else None


def connect(ctx, create=False):
    if not ctx.db_path.exists() and not create:
        sys.exit("Tracker not initialized for this repo. Run: python tracker.py init")
    ctx.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ctx.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)  # idempotent; preserves existing data
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def read_file(ctx, rel):
    """Return (bytes|None). None if missing/unreadable."""
    try:
        return (ctx.root / rel).read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def is_text(data):
    return b"\x00" not in data[:8192]


def store_blob(conn, data):
    """Store content addressed; returns sha. Binary/oversized: hash + size only."""
    sha = sha_bytes(data)
    content = None
    if len(data) <= MAX_BLOB and is_text(data):
        content = zlib.compress(data, 6)
    conn.execute("INSERT OR IGNORE INTO blobs(sha256,size,content) VALUES(?,?,?)", (sha, len(data), content))
    return sha


def load_blob(conn, sha):
    if not sha:
        return None
    r = conn.execute("SELECT content FROM blobs WHERE sha256=?", (sha,)).fetchone()
    return zlib.decompress(r["content"]) if r and r["content"] is not None else None


def list_files(ctx):
    """Tracked + untracked-but-not-ignored files (git), else a filtered walk."""
    if ctx.is_git:
        p = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], ctx.root)
        if p.returncode == 0:
            files = [f for f in p.stdout.split("\0") if f]
            return sorted(f for f in files if not f.startswith(".agent-tracker/") and (ctx.root / f).is_file())
    out = []
    for dp, dns, fns in os.walk(ctx.root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            out.append(os.path.relpath(os.path.join(dp, fn), ctx.root).replace(os.sep, "/"))
    return sorted(out)


def make_diff(before, after, path):
    if before is None and after is None:
        return "", 0, 0
    if (before is not None and not is_text(before)) or (after is not None and not is_text(after)):
        return "(binary or non-text change; content not diffed)", 0, 0
    a = (before or b"").decode("utf-8", "replace").splitlines(keepends=True)
    b = (after or b"").decode("utf-8", "replace").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if len(lines) > DIFF_LINE_CAP:
        lines = lines[:DIFF_LINE_CAP] + [f"\n... diff truncated ({len(lines) - DIFF_LINE_CAP} more lines)\n"]
    return "".join(lines), added, removed


# ----------------------------------------------------------------- sessions
def active_session(conn):
    r = conn.execute("SELECT value FROM meta WHERE key='active_session'").fetchone()
    if r:
        s = conn.execute("SELECT * FROM sessions WHERE id=? AND ended_at IS NULL", (int(r["value"]),)).fetchone()
        if s:
            return s
    return None


def ensure_session(ctx, conn, agent=None, goal=None):
    s = active_session(conn)
    if s:
        return s["id"]
    return start_session(ctx, conn, agent or os.environ.get("TRACKER_AGENT", "unknown"), goal)


def start_session(ctx, conn, agent, goal):
    cur = conn.execute(
        "INSERT INTO sessions(agent,goal,started_at,branch,git_head_start) VALUES(?,?,?,?,?)",
        (agent, goal, now(), ctx.git("rev-parse", "--abbrev-ref", "HEAD"), ctx.git("rev-parse", "HEAD")))
    sid = cur.lastrowid
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('active_session',?)", (str(sid),))
    conn.commit()
    return sid


# ----------------------------------------------------------------- file sync
def upsert_state(conn, ctx, rel, data):
    if data is None:
        conn.execute("""INSERT INTO file_states(path,sha256,size,mtime,deleted,updated_at) VALUES(?,?,?,?,1,?)
                        ON CONFLICT(path) DO UPDATE SET deleted=1,updated_at=excluded.updated_at""",
                     (rel, None, None, None, now()))
        return
    sha = store_blob(conn, data)
    try:
        mt = (ctx.root / rel).stat().st_mtime
    except OSError:
        mt = None
    conn.execute("""INSERT INTO file_states(path,sha256,size,mtime,deleted,updated_at) VALUES(?,?,?,?,0,?)
                    ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,size=excluded.size,
                    mtime=excluded.mtime,deleted=0,updated_at=excluded.updated_at""",
                 (rel, sha, len(data), mt, now()))


def detect(ctx, conn, only=None):
    """Compare filesystem to file_states. Returns list of (path, op, before_data, after_data)."""
    known = {r["path"]: r for r in conn.execute("SELECT * FROM file_states")}
    current = set(only) if only else set(list_files(ctx))
    found = []
    for rel in sorted(current):
        data = read_file(ctx, rel)
        st = known.get(rel)
        if data is None:
            if st and not st["deleted"]:
                found.append((rel, "delete", load_blob(conn, st["sha256"]), None, st["sha256"], None))
            continue
        sha = sha_bytes(data)
        if st is None or st["deleted"]:
            found.append((rel, "create", None, data, None, sha))
        elif st["sha256"] != sha:
            found.append((rel, "modify", load_blob(conn, st["sha256"]), data, st["sha256"], sha))
    if not only:
        for rel, st in known.items():
            if not st["deleted"] and rel not in current:
                found.append((rel, "delete", load_blob(conn, st["sha256"]), None, st["sha256"], None))
    return found


def record(ctx, conn, items, source, session_id, task_id=None, rationale=None):
    for rel, op, before, after, bsha, asha in items:
        diff, add, rem = make_diff(before, after, rel)
        if after is not None:
            asha = store_blob(conn, after)
        conn.execute(
            """INSERT INTO changes(session_id,task_id,ts,path,op,source,before_sha,after_sha,lines_added,
               lines_removed,diff,rationale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, task_id, now(), rel, op, source, bsha, asha, add, rem, diff, rationale))
        upsert_state(conn, ctx, rel, after)
    conn.commit()


def baseline(ctx, conn):
    n = 0
    for rel in list_files(ctx):
        data = read_file(ctx, rel)
        if data is not None:
            upsert_state(conn, ctx, rel, data)
            n += 1
    conn.commit()
    return n


# ----------------------------------------------------------------- commands
def cmd_init(a, ctx):
    fresh = not ctx.db_path.exists()
    conn = connect(ctx, create=True)
    if fresh:
        n = baseline(ctx, conn)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('initialized_at',?)", (now(),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_git_head',?)", (ctx.git("rev-parse", "HEAD") or "",))
        conn.commit()
        print(f"Initialized tracker at {ctx.db_path} (baseline: {n} files).")
    else:
        print(f"Tracker already initialized at {ctx.db_path}; existing data preserved.")
    # keep the tracker out of git WITHOUT touching .gitignore or source files
    if ctx.is_git and ctx.home == ctx.root / ".agent-tracker":
        ex = ctx.root / ".git" / "info" / "exclude"
        try:
            ex.parent.mkdir(parents=True, exist_ok=True)
            text = ex.read_text() if ex.exists() else ""
            if ".agent-tracker/" not in text:
                ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".agent-tracker/\n")
        except OSError:
            pass


def cmd_session(a, ctx):
    conn = connect(ctx)
    if a.action == "start":
        s = active_session(conn)
        if s:
            conn.execute("UPDATE sessions SET ended_at=?,summary=COALESCE(summary,'(auto-closed by new session)') WHERE id=?", (now(), s["id"]))
        # reconcile outside changes first so they are not attributed to this agent
        rec = reconcile(ctx, conn, None, dry=False)
        sid = start_session(ctx, conn, a.agent, a.goal)
        print(f"Session {sid} started (agent={a.agent}). Reconciled: {rec['created']} created, "
              f"{rec['modified']} modified, {rec['deleted']} deleted external changes.")
    else:
        s = active_session(conn)
        if not s:
            sys.exit("No active session.")
        items = detect(ctx, conn)
        if items:
            record(ctx, conn, items, "agent", s["id"])
            print(f"Recorded {len(items)} unlogged change(s) before closing.")
        conn.execute("UPDATE sessions SET ended_at=?,summary=?,git_head_end=? WHERE id=?",
                     (now(), a.summary, ctx.git("rev-parse", "HEAD"), s["id"]))
        conn.execute("DELETE FROM meta WHERE key='active_session'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_git_head',?)", (ctx.git("rev-parse", "HEAD") or "",))
        conn.commit()
        print(f"Session {s['id']} ended.")


def cmd_task(a, ctx):
    conn = connect(ctx)
    sid = ensure_session(ctx, conn, a.agent)
    if a.action == "add":
        cur = conn.execute("INSERT INTO tasks(session_id,title,description,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (sid, a.title, a.description, a.status or "todo", now(), now()))
        conn.commit()
        print(f"Task {cur.lastrowid} added: {a.title}")
    elif a.action == "update":
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (a.id,)).fetchone()
        if not t:
            sys.exit(f"No task {a.id}")
        notes = t["notes"] or ""
        if a.note:
            notes = (notes + "\n" if notes else "") + f"[{now()}] {a.note}"
        conn.execute("UPDATE tasks SET status=COALESCE(?,status),notes=?,updated_at=? WHERE id=?",
                     (a.status, notes, now(), a.id))
        conn.commit()
        print(f"Task {a.id} updated.")
    else:
        q = "SELECT id,status,title,updated_at FROM tasks" + ("" if a.all else " WHERE status IN ('todo','in_progress','blocked')") + " ORDER BY id"
        for r in conn.execute(q):
            print(f"#{r['id']:<4} [{r['status']}] {r['title']}  ({r['updated_at']})")


def cmd_sync(a, ctx):
    """Record changes made since the last sync. Agents call this after editing files."""
    conn = connect(ctx)
    sid = ensure_session(ctx, conn, a.agent)
    only = [p.replace(os.sep, "/") for p in a.paths] if a.paths else None
    items = detect(ctx, conn, only)
    if not items:
        print("No changes to record.")
        return
    record(ctx, conn, items, "agent", sid, a.task, a.why)
    for rel, op, *_ in items:
        print(f"  {op:<6} {rel}")
    print(f"Recorded {len(items)} change(s).")


def cmd_decision(a, ctx):
    conn = connect(ctx)
    sid = ensure_session(ctx, conn, a.agent)
    conn.execute("INSERT INTO decisions(session_id,task_id,ts,title,rationale,alternatives) VALUES(?,?,?,?,?,?)",
                 (sid, a.task, now(), a.title, a.why, a.alternatives))
    conn.commit()
    print("Decision recorded.")


def snapshot_all(ctx, conn):
    """Make sure every current file is stored; returns {path: sha}."""
    out = {}
    for rel in list_files(ctx):
        data = read_file(ctx, rel)
        if data is not None:
            out[rel] = store_blob(conn, data)
    return out


def cmd_checkpoint(a, ctx):
    conn = connect(ctx)
    if a.action == "create":
        sid = ensure_session(ctx, conn, a.agent)
        # log pending edits first so history stays chronological
        items = detect(ctx, conn)
        if items:
            record(ctx, conn, items, "agent", sid)
        files = snapshot_all(ctx, conn)
        dirty = ctx.git("status", "--porcelain")
        cur = conn.execute("INSERT INTO checkpoints(session_id,ts,name,note,git_head,git_dirty) VALUES(?,?,?,?,?,?)",
                           (sid, now(), a.name, a.note, ctx.git("rev-parse", "HEAD"), 1 if dirty else 0))
        cid = cur.lastrowid
        conn.executemany("INSERT INTO checkpoint_files(checkpoint_id,path,sha256) VALUES(?,?,?)",
                         [(cid, p, s) for p, s in files.items()])
        conn.commit()
        print(f"Checkpoint {cid} '{a.name}' saved ({len(files)} files).")
    elif a.action == "list":
        for r in conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM checkpoint_files f WHERE f.checkpoint_id=c.id) n
                                 FROM checkpoints c ORDER BY id DESC LIMIT ?""", (a.limit,)):
            print(f"#{r['id']:<3} {r['ts']}  {r['name']}  ({r['n']} files, git {str(r['git_head'])[:8]})  {r['note'] or ''}")
    elif a.action in ("diff", "restore"):
        c = conn.execute("SELECT * FROM checkpoints WHERE id=?", (a.id,)).fetchone()
        if not c:
            sys.exit(f"No checkpoint {a.id}")
        cp = {r["path"]: r["sha256"] for r in conn.execute("SELECT path,sha256 FROM checkpoint_files WHERE checkpoint_id=?", (a.id,))}
        cur_files = {}
        for rel in list_files(ctx):
            d = read_file(ctx, rel)
            if d is not None:
                cur_files[rel] = sha_bytes(d)
        to_write = [p for p, s in cp.items() if cur_files.get(p) != s]
        to_remove = [p for p in cur_files if p not in cp]
        unrestorable = [p for p in to_write if load_blob(conn, cp[p]) is None]
        print(f"Checkpoint {a.id} '{c['name']}' vs working tree:")
        for p in to_write:
            print(f"  would {'restore' if p in cur_files else 'recreate'}: {p}" + ("  [binary/oversized: cannot restore]" if p in unrestorable else ""))
        for p in to_remove:
            print(f"  not in checkpoint (files created since): {p}" + ("  -> will delete" if a.action == "restore" and a.delete_new else ""))
        if a.action == "diff" or not a.apply:
            if a.action == "restore":
                print("Dry run. Re-run with --apply to write files (a safety checkpoint is created first).")
            return
        sid = ensure_session(ctx, conn, a.agent)
        cmd_checkpoint(argparse.Namespace(action="create", name=f"pre-restore-{a.id}", note="auto safety checkpoint", agent=a.agent), ctx)
        for p in to_write:
            if p in unrestorable:
                continue
            dest = ctx.root / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(load_blob(conn, cp[p]))
        if a.delete_new:
            for p in to_remove:
                try:
                    (ctx.root / p).unlink()
                except OSError:
                    pass
        items = detect(ctx, conn)
        record(ctx, conn, items, "agent", sid, None, f"restore of checkpoint {a.id}")
        print(f"Restored checkpoint {a.id}; {len(items)} change(s) logged.")


def cmd_verify(a, ctx):
    conn = connect(ctx)
    sid = ensure_session(ctx, conn, a.agent)
    status, code, tail = a.status, a.exit_code, a.output
    if a.run:
        try:
            p = subprocess.run(a.cmd, shell=True, cwd=ctx.root, capture_output=True, text=True, timeout=a.timeout)
            code, status = p.returncode, "pass" if p.returncode == 0 else "fail"
            tail = ((p.stdout or "") + (p.stderr or ""))[-2000:]
        except subprocess.TimeoutExpired:
            code, status, tail = None, "fail", f"timed out after {a.timeout}s"
        print(tail[-800:])
    if status is None:
        sys.exit("Provide --status pass|fail|skipped, or use --run.")
    conn.execute("INSERT INTO verifications(session_id,task_id,ts,kind,command,status,exit_code,output_tail) VALUES(?,?,?,?,?,?,?,?)",
                 (sid, a.task, now(), a.kind, a.cmd, status, code, (tail or "")[-2000:]))
    conn.commit()
    print(f"Verification recorded: {a.kind} -> {status}")


def reconcile(ctx, conn, session_id, dry):
    """Bring DB in line with filesystem + git. Attribute unlogged edits to 'external'."""
    last = (conn.execute("SELECT value FROM meta WHERE key='last_git_head'").fetchone() or {"value": ""})["value"]
    head = ctx.git("rev-parse", "HEAD") or ""
    commits = ""
    if last and head and last != head:
        commits = ctx.git("log", "--oneline", "--no-decorate", f"{last}..{head}") or ctx.git("log", "--oneline", "-5") or ""
    items = detect(ctx, conn)
    res = {"created": sum(i[1] == "create" for i in items), "modified": sum(i[1] == "modify" for i in items),
           "deleted": sum(i[1] == "delete" for i in items), "commits": commits, "items": items,
           "head_before": last, "head_after": head}
    if not dry:
        sid = session_id
        if items:
            record(ctx, conn, items, "external", sid, None, "detected during reconcile (not made by tracked agent)")
        conn.execute("INSERT INTO reconcile_log(ts,session_id,git_head_before,git_head_after,commits,created,modified,deleted) VALUES(?,?,?,?,?,?,?,?)",
                     (now(), sid, last, head, commits, res["created"], res["modified"], res["deleted"]))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_git_head',?)", (head,))
        conn.commit()
    return res


def cmd_reconcile(a, ctx):
    conn = connect(ctx)
    s = active_session(conn)
    res = reconcile(ctx, conn, s["id"] if s else None, a.dry_run)
    verb = "Would record" if a.dry_run else "Recorded"
    print(f"{verb}: {res['created']} created, {res['modified']} modified, {res['deleted']} deleted (external).")
    for rel, op, *_ in res["items"]:
        print(f"  {op:<6} {rel}")
    if res["commits"]:
        print("New commits since last reconcile:\n" + "\n".join("  " + l for l in res["commits"].splitlines()))


def cmd_context(a, ctx):
    """Print a briefing for a fresh AI session."""
    conn = connect(ctx)
    out = [f"# Project context: {ctx.root.name}", ""]
    if ctx.is_git:
        out += [f"- Git: branch `{ctx.git('rev-parse', '--abbrev-ref', 'HEAD')}` @ `{(ctx.git('rev-parse', 'HEAD') or '')[:8]}`"]
        st = ctx.git("status", "--porcelain")
        out += [f"- Working tree: {'dirty (' + str(len(st.splitlines())) + ' paths)' if st else 'clean'}"]
    rec = reconcile(ctx, conn, None, dry=True)
    if rec["items"] or rec["commits"]:
        out += ["", "## Drift since last tracked session (not yet reconciled; run `reconcile`)"]
        out += [f"- {op} `{rel}`" for rel, op, *_ in rec["items"][:25]]
        if rec["commits"]:
            out += ["- New commits:"] + [f"  - {l}" for l in rec["commits"].splitlines()[:10]]
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (a.sessions,)).fetchall()
    out += ["", "## Recent sessions"]
    for s in reversed(sessions):
        nch = conn.execute("SELECT COUNT(*) c FROM changes WHERE session_id=?", (s["id"],)).fetchone()["c"]
        out += [f"- #{s['id']} {s['started_at']} [{s['agent']}] goal: {s['goal'] or '-'} | {nch} changes | summary: {s['summary'] or ('(in progress)' if not s['ended_at'] else '-')}"]
    open_t = conn.execute("SELECT * FROM tasks WHERE status IN ('todo','in_progress','blocked') ORDER BY id").fetchall()
    out += ["", "## Open tasks"] + ([f"- #{t['id']} [{t['status']}] {t['title']}" + (f" — {t['notes'].splitlines()[-1]}" if t['notes'] else "") for t in open_t] or ["- none"])
    dec = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (a.decisions,)).fetchall()
    out += ["", "## Recent decisions (why things are the way they are)"]
    out += [f"- {d['ts']} **{d['title']}** — {d['rationale']}" + (f" (alternatives: {d['alternatives']})" if d['alternatives'] else "") for d in reversed(dec)] or ["- none"]
    ver = conn.execute("SELECT * FROM verifications ORDER BY id DESC LIMIT 5").fetchall()
    out += ["", "## Latest verification results"] + ([f"- {v['ts']} {v['kind']}: **{v['status']}**" + (f" (`{v['command']}`)" if v['command'] else "") for v in ver] or ["- none"])
    hot = conn.execute("""SELECT path, COUNT(*) n, MAX(ts) last FROM changes GROUP BY path ORDER BY last DESC LIMIT ?""", (a.files,)).fetchall()
    out += ["", "## Recently changed files"] + ([f"- `{h['path']}` ({h['n']} change(s), last {h['last']})" for h in hot] or ["- none"])
    cp = conn.execute("SELECT * FROM checkpoints ORDER BY id DESC LIMIT 3").fetchall()
    out += ["", "## Checkpoints"] + ([f"- #{c['id']} {c['name']} ({c['ts']})" for c in cp] or ["- none"])
    out += ["", "_Resume by reviewing open tasks, then call `session start` and continue._"]
    print("\n".join(out))


def cmd_history(a, ctx):
    conn = connect(ctx)
    q, args = "SELECT * FROM changes", []
    cond = []
    if a.path:
        cond.append("path=?"); args.append(a.path)
    if a.session:
        cond.append("session_id=?"); args.append(a.session)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(a.limit)
    for r in reversed(conn.execute(q, args).fetchall()):
        print(f"{r['ts']} #{r['id']} s{r['session_id']} {r['source']:<8} {r['op']:<6} {r['path']} +{r['lines_added']}/-{r['lines_removed']}"
              + (f"  — {r['rationale']}" if r['rationale'] else ""))
        if a.diff and r["diff"]:
            print(r["diff"])


def cmd_status(a, ctx):
    conn = connect(ctx)
    s = active_session(conn)
    print(f"DB: {ctx.db_path}")
    print(f"Active session: {('#%d (%s)' % (s['id'], s['agent'])) if s else 'none'}")
    for t in ("sessions", "tasks", "changes", "decisions", "checkpoints", "verifications", "blobs"):
        print(f"  {t}: {conn.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c']}")
    print(f"Unlogged changes on disk: {len(detect(ctx, conn))}")


def cmd_schema(a, ctx):
    print(SCHEMA.strip())


def main():
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave when piped to head/grep -q
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="repo root (default: git toplevel of cwd)")
    ap.add_argument("--agent", default=os.environ.get("TRACKER_AGENT"), help="agent name, e.g. cline, antigravity")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    p = sub.add_parser("session"); p.set_defaults(fn=cmd_session)
    p.add_argument("action", choices=["start", "end"]); p.add_argument("--goal"); p.add_argument("--summary")

    p = sub.add_parser("task"); p.set_defaults(fn=cmd_task)
    p.add_argument("action", choices=["add", "update", "list"]); p.add_argument("title", nargs="?")
    p.add_argument("--id", type=int); p.add_argument("--description"); p.add_argument("--note")
    p.add_argument("--status", choices=["todo", "in_progress", "done", "blocked", "abandoned"]); p.add_argument("--all", action="store_true")

    p = sub.add_parser("sync", help="record file changes made since last sync"); p.set_defaults(fn=cmd_sync)
    p.add_argument("paths", nargs="*"); p.add_argument("--why", help="rationale for these changes"); p.add_argument("--task", type=int)

    p = sub.add_parser("decision"); p.set_defaults(fn=cmd_decision)
    p.add_argument("title"); p.add_argument("--why", required=True); p.add_argument("--alternatives"); p.add_argument("--task", type=int)

    p = sub.add_parser("checkpoint"); p.set_defaults(fn=cmd_checkpoint)
    p.add_argument("action", choices=["create", "list", "diff", "restore"]); p.add_argument("name", nargs="?", default="checkpoint")
    p.add_argument("--id", type=int); p.add_argument("--note"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--apply", action="store_true"); p.add_argument("--delete-new", action="store_true")

    p = sub.add_parser("verify"); p.set_defaults(fn=cmd_verify)
    p.add_argument("kind", help="test|build|lint|manual|..."); p.add_argument("--cmd"); p.add_argument("--run", action="store_true")
    p.add_argument("--status", choices=["pass", "fail", "skipped"]); p.add_argument("--exit-code", type=int); p.add_argument("--output")
    p.add_argument("--task", type=int); p.add_argument("--timeout", type=int, default=600)

    p = sub.add_parser("reconcile"); p.set_defaults(fn=cmd_reconcile); p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("context", help="print briefing for a new AI session"); p.set_defaults(fn=cmd_context)
    p.add_argument("--sessions", type=int, default=5); p.add_argument("--decisions", type=int, default=8); p.add_argument("--files", type=int, default=10)

    p = sub.add_parser("history"); p.set_defaults(fn=cmd_history)
    p.add_argument("--path"); p.add_argument("--session", type=int); p.add_argument("--limit", type=int, default=30); p.add_argument("--diff", action="store_true")

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("schema").set_defaults(fn=cmd_schema)

    a = ap.parse_args()
    if a.command == "task" and a.action == "add" and not a.title:
        ap.error("task add requires a title")
    if a.command == "task" and a.action == "update" and not a.id:
        ap.error("task update requires --id")
    if a.command == "checkpoint" and a.action in ("diff", "restore") and not a.id:
        ap.error("--id required")
    if a.command == "verify" and a.run and not a.cmd:
        ap.error("--run requires --cmd")
    root = Path(a.root).resolve() if a.root else find_root(os.getcwd())
    ctx = Ctx(root)
    a.agent = a.agent or "unknown"
    a.fn(a, ctx)


if __name__ == "__main__":
    main()
