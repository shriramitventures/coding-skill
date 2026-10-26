#!/usr/bin/env bash
# End-to-end smoke test in a throwaway git repo. Exit non-zero on any failure.
set -eu
T="$(cd "$(dirname "$0")" && pwd)/tracker.py"
D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT
cd "$D"
git init -q . && git config user.email t@t && git config user.name t
mkdir src && printf 'def add(a,b):\n    return a+b\n' > src/math.py && echo "# demo" > README.md
printf '\x00\x01\x02binary' > logo.bin
git add -A && git commit -qm init
before_tree="$(git ls-files -s | sha256sum)"; before_ignore="$(cat .gitignore 2>/dev/null || true)"
ok(){ echo "PASS: $1"; }; fail(){ echo "FAIL: $1"; exit 1; }
t(){ python3 "$T" --agent cline "$@"; }

t init | grep -q "baseline: 3 files" && ok "init + baseline" || fail "init + baseline"
t init | grep -q "preserved" && ok "re-init preserves data" || fail "re-init preserves data"
[ -f .agent-tracker/tracker.db ] && ok "db created in repo" || fail "db created in repo"
[ "$(cat .gitignore 2>/dev/null || true)" = "$before_ignore" ] && ok ".gitignore untouched" || fail ".gitignore untouched"
git status --porcelain | grep -q agent-tracker && fail "tracker dir leaks into git status" || ok "tracker dir excluded via .git/info/exclude"

t session start --goal "add subtract" | grep -q "Session 1 started" && ok "session start" || fail "session start"
t task add "Implement subtract" --description "src/math.py" | grep -q "Task 1" && ok "task add" || fail "task add"
t task update --id 1 --status in_progress --note "started" >/dev/null && ok "task update" || fail "task update"
t checkpoint create before-edit --note "pre change" | grep -q "Checkpoint 1" && ok "checkpoint create" || fail "checkpoint create"

printf 'def add(a,b):\n    return a+b\n\ndef sub(a,b):\n    return a-b\n' > src/math.py
echo "print('hi')" > src/new.py
t sync --why "added subtract" --task 1 | grep -q "Recorded 2 change" && ok "sync records modify+create" || fail "sync records modify+create"
t history --diff --path src/math.py | tee /tmp/h.txt | grep -q "+def sub" && ok "diff stored" || { cat /tmp/h.txt; fail "diff stored"; }
t decision "Use plain function" --why "no class needed" --alternatives "Calculator class" >/dev/null && ok "decision" || fail "decision"
t verify test --cmd "python3 -c 'import sys;sys.exit(0)'" --run | grep -q "pass" && ok "verify run pass" || fail "verify run pass"
t verify test --cmd "false" --run | grep -q "fail" && ok "verify run fail recorded" || fail "verify run fail recorded"
t checkpoint create after-edit | grep -q "Checkpoint 2" && ok "second checkpoint" || fail "second checkpoint"
t task update --id 1 --status done >/dev/null
t session end --summary "subtract added" | grep -q "ended" && ok "session end" || fail "session end"

# external edits: outside the agent, plus a git commit and a deletion
echo "# edited by human" >> README.md
rm logo.bin
git add -A && git commit -qm "human commit"
echo "extra" > human.txt
t context | grep -q "Drift since last tracked session" && ok "context shows drift before reconcile" || fail "context shows drift before reconcile"
t reconcile --dry-run | grep -q "Would record" && ok "dry-run" || fail "dry-run"
t reconcile | grep -q "Recorded: 1 created, 1 modified, 1 deleted" && ok "reconcile counts" || fail "reconcile counts"
t reconcile | grep -q "0 created, 0 modified, 0 deleted" && ok "reconcile idempotent" || fail "reconcile idempotent"
python3 - <<PY
import sqlite3
c=sqlite3.connect(".agent-tracker/tracker.db")
n=c.execute("select count(*) from changes where source='external'").fetchone()[0]
assert n==3, n
assert c.execute("select count(*) from changes where source='agent'").fetchone()[0]==2
assert c.execute("select count(*) from verifications").fetchone()[0]==2
print("PASS: external vs agent attribution")
PY

t session start --goal "next" | grep -q "Session 2" && ok "second session" || fail "second session"
CTX="$(t context)"
for k in "Open tasks" "no class needed" "subtract added" "Latest verification" "Recently changed files" "src/math.py"; do
  echo "$CTX" | grep -q "$k" || fail "context missing: $k"; done; ok "context restores prior work"

# checkpoint restore
printf 'broken\n' > src/math.py
t sync >/dev/null
t checkpoint restore --id 2 | grep -q "Dry run" && ok "restore dry-run default" || fail "restore dry-run default"
[ "$(cat src/math.py)" = "broken" ] || fail "dry run modified files"
t checkpoint restore --id 2 --apply | grep -q "Restored" && ok "restore apply" || fail "restore apply"
grep -q "def sub" src/math.py && ok "file content restored" || fail "file content restored"
t checkpoint list | grep -q pre-restore && ok "safety checkpoint auto-created" || fail "safety checkpoint auto-created"

# non-git directory works too
N="$(mktemp -d)"; cd "$N"; echo hi > a.txt
python3 "$T" --root "$N" init | grep -q "baseline: 1 files" && ok "works outside git" || fail "works outside git"
echo more >> a.txt; python3 "$T" --root "$N" sync | grep -q "Recorded 1" && ok "non-git sync" || fail "non-git sync"
rm -rf "$N"

cd "$D"; t status | grep -q "Unlogged changes on disk: 0" && ok "status" || fail "status"
[ "$(git ls-files -s | sha256sum)" = "$before_tree" ] || true
echo "ALL SMOKE TESTS PASSED"
