# Integration guide

The tracker is agent-agnostic: any agent that can run a shell command can use it. What differs per tool is *how you make the agent run it reliably*. Agent products change their config conventions often, so confirm file locations against your installed version's docs; the commands below do not change.

## Install (any tool)

```bash
unzip project-tracking-skill.zip -d ~/.agent-skills/
cd /path/to/your/repo
python3 ~/.agent-skills/project-tracking/scripts/tracker.py init
bash ~/.agent-skills/project-tracking/scripts/smoke_test.sh     # optional self-test
```

Requires Python 3.8+ and (optionally) Git. Convenience alias:

```bash
alias track='python3 ~/.agent-skills/project-tracking/scripts/tracker.py'
```

## Cline

Cline follows project instructions from a `.clinerules` file or folder in the repo root. Add (adjust the path):

```markdown
# Project tracking (mandatory)
At the start of every task run:
  TRACKER_AGENT=cline python3 ~/.agent-skills/project-tracking/scripts/tracker.py init
  TRACKER_AGENT=cline python3 ~/.agent-skills/project-tracking/scripts/tracker.py context
and read the output before planning. Then run `session start --goal "<task>"`.
After each batch of file edits run `sync --why "<reason>"`.
Record non-obvious choices with `decision "<title>" --why "<rationale>"`.
Run tests via `verify test --cmd "<test command>" --run`.
At the end run `session end --summary "<done / remaining>"`.
Never edit .agent-tracker/ directly.
```

If your Cline version supports hooks or workflows, a good mapping is: *task start* -> `init` + `context` + `session start`; *after file write/replace* -> `sync`; *task complete* -> `session end`. The rules text above works without hook support. Because rules are advisory, `sync` is idempotent and `session start`, `session end` and `checkpoint create` also catch unlogged edits, so a skipped call is repaired by the next one.

Note: `.clinerules` is a project file you choose to add; the tracker itself never writes it.

## Antigravity

Antigravity supports workspace rules, workflows and skills (commonly under a `.agent/` folder). Either:

1. Copy `project-tracking/` into the skills location your version uses (commonly `.agent/skills/`) so `SKILL.md` is discovered automatically; or
2. Add a workspace rule containing the same instruction block as the Cline example, with `TRACKER_AGENT=antigravity`.

Keep rules short and imperative; the `SKILL.md` body is the canonical procedure.

## Other agents (Claude Code, Cursor, Aider, Continue, ...)

Put the same instruction block in that tool's project-instructions file (`CLAUDE.md`, rules files, `CONVENTIONS.md`, etc.). If the tool has post-edit hooks, run `tracker.py sync` from them. Use a distinct `TRACKER_AGENT` per tool; `context` shows which agent did what.

## Optional Git hooks (capture human work automatically)

Keeps the DB reconciled even when no agent is running. Add to `.git/hooks/post-commit` and `.git/hooks/post-checkout` (local only, never committed):

```bash
#!/bin/sh
python3 ~/.agent-skills/project-tracking/scripts/tracker.py reconcile >/dev/null 2>&1 || true
```

## Data and privacy

- Stored in `.agent-tracker/tracker.db`. Diffs and snapshots contain file contents, so the DB can include secrets present in tracked files. Do not share it; it is excluded from Git by default.
- Move it with `TRACKER_HOME=/path`. Back up with `sqlite3 tracker.db ".backup out.db"`.
- To start over, delete `.agent-tracker/`; nothing else in the repo is affected.

## Limits (honest scope)

- This reproduces Cursor-style *observable* behavior (history, checkpoints, restore, context) using Git plus filesystem snapshots. It does not read any IDE's private internal state, and it cannot see edits until `sync` or `reconcile` runs.
- Attribution is by timing: changes seen by `sync` are `agent`, changes found by `reconcile` are `external`. A human edit made between your edit and your `sync` is attributed to the agent.
- Renames are not detected: a rename appears as delete plus create.
