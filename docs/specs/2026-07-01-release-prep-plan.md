# release-prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `release-prep` Claude Code skill + thin `/loop` wrapper that rebuilds a disposable, squashed-by-category `release/*` branch from the working branch (tree byte-identical), regenerates an informative `CHANGES-vs-master.md`, self-reviews, and force-pushes the release branch — never touching the working branch.

**Architecture:** A tested standalone Python CLI (`release_prep.py`) owns the deterministic, safety-critical git mechanics (worktree, squash-by-category, tree-identity verify, header numbers, sense, guarded push). `SKILL.md` orchestrates the 6 phases and owns the agent-judgment parts (incremental classification, table prose, self-review). A `/loop 1h` invocation runs the skill on an interval in a separate terminal; the same skill runs on-demand.

**Tech Stack:** Python 3.10+ (stdlib only: `subprocess`, `json`, `pathlib`, `argparse`), `git`, `gh` (PR text only), `pytest` for RED/GREEN, superpowers:writing-skills for authoring `SKILL.md`.

## Global Constraints

- **Tree-identity invariant (hard):** `git diff <working-head> <release-branch>` MUST be empty after rebuild. Non-empty → abort, do not push.
- **Never modify the working branch** — not its history, not its working tree. Skill executes only inside the dedicated worktree.
- **Push only `release/*`** — `publish()` refuses any branch not prefixed `release/`. Force-push uses `--force-with-lease`.
- **Skill sees only committed state** of the working branch (reads its HEAD sha).
- **No AI attribution anywhere** — commit messages, PR text, code comments. No "Claude/Anthropic/Co-Authored-By" strings. (Project rule.)
- **Skill lives outside the project repo** at `~/.claude/skills/release-prep/` (global, like `reintegrating-master`). It is version-controlled in its own git repo there for TDD checkpoints. The project repo only ever gains this plan, the spec, and the auto-generated `CHANGES-vs-master.md` on the release branch.
- **State lives beside the skill** at `~/.claude/skills/release-prep/state/<repo-slug>.json` (NOT in the worktree).
- Source of truth spec: `docs/specs/2026-07-01-release-prep-loop-design.md`.

---

## File Structure

All under `~/.claude/skills/release-prep/` (its own git repo):

- `release_prep.py` — the deterministic git/state engine + `argparse` CLI. One focused module (split if it outgrows ~300 lines).
- `tests/test_release_prep.py` — pytest, synthetic-git-repo fixtures (RED/GREEN). Self-contained; never touches the real repo.
- `SKILL.md` — the 6-phase orchestration skill (authored via writing-skills).
- `loop.md` — the `/loop` invocation + on-demand usage doc.
- `state/` — runtime state JSONs (gitignored in the skill repo).

`commit_plan` data contract (produced by the agent in Classify, consumed by `rebuild`):
```json
[ {"message": "3. Слои инструкций L0–L3: <саммари>", "files": ["src/states.py", "..."]}, ... ]
```
Ordered by importance. Each file from `master..working` appears in exactly one entry (orphans are caught by the tree-identity verify).

---

### Task 1: Skill scaffold + state I/O + delta

**Files:**
- Create: `~/.claude/skills/release-prep/release_prep.py`
- Create: `~/.claude/skills/release-prep/tests/test_release_prep.py`
- Create: `~/.claude/skills/release-prep/.gitignore`

**Interfaces:**
- Produces:
  - `git(*args, cwd: Path | None = None, check: bool = True) -> str` — run git, return stripped stdout; raise `RuntimeError` on failure when `check`.
  - `load_state(state_path: Path) -> dict` — parse JSON or `{}` if missing.
  - `save_state(state_path: Path, state: dict) -> None` — write pretty UTF-8 JSON, creating parents.
  - `changed_files(repo: Path, base: str, head: str) -> list[str]` — `git diff --name-only base..head`, non-empty lines.

- [ ] **Step 1: Init the skill repo and gitignore**

```bash
mkdir -p ~/.claude/skills/release-prep/tests ~/.claude/skills/release-prep/state
cd ~/.claude/skills/release-prep
git init -q
printf 'state/\n__pycache__/\n*.pyc\n.pytest_cache/\n' > .gitignore
```

- [ ] **Step 2: Write the failing tests**

`tests/test_release_prep.py`:
```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import release_prep as rp


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    rp.git("init", "-q", "-b", "master", cwd=path)
    rp.git("config", "user.email", "t@t", cwd=path)
    rp.git("config", "user.name", "t", cwd=path)
    return path


def _commit(repo: Path, name: str, body: str = "x") -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(body, encoding="utf-8")
    rp.git("add", "--", name, cwd=repo)
    rp.git("commit", "-q", "-m", f"add {name}", cwd=repo)
    return rp.git("rev-parse", "HEAD", cwd=repo)


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state" / "repo.json"
    assert rp.load_state(p) == {}
    rp.save_state(p, {"last_seen_head": "abc", "файл": "рус"})
    assert rp.load_state(p) == {"last_seen_head": "abc", "файл": "рус"}
    # UTF-8 preserved, not escaped
    assert "рус" in p.read_text(encoding="utf-8")


def test_changed_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _commit(repo, "a.txt")
    _commit(repo, "src/b.py")
    _commit(repo, "src/c.py")
    head = rp.git("rev-parse", "HEAD", cwd=repo)
    assert sorted(rp.changed_files(repo, base, head)) == ["src/b.py", "src/c.py"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q`
Expected: FAIL — `AttributeError: module 'release_prep' has no attribute 'git'`.

- [ ] **Step 4: Implement the engine core**

`release_prep.py`:
```python
"""release-prep engine: deterministic git/state mechanics for the release-prep skill."""
import json
import subprocess
from pathlib import Path


def git(*args, cwd=None, check=True):
    r = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8",
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def load_state(state_path: Path) -> dict:
    p = Path(state_path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_state(state_path: Path, state: dict) -> None:
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def changed_files(repo: Path, base: str, head: str) -> list:
    out = git("diff", "--name-only", f"{base}..{head}", cwd=repo)
    return [line for line in out.splitlines() if line]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
cd ~/.claude/skills/release-prep
git add release_prep.py tests/test_release_prep.py .gitignore
git commit -q -m "feat: release-prep engine — git/state I/O + changed_files"
```

---

### Task 2: Rebuild engine + tree-identity verify (crown jewel)

**Files:**
- Modify: `~/.claude/skills/release-prep/release_prep.py`
- Modify: `~/.claude/skills/release-prep/tests/test_release_prep.py`

**Interfaces:**
- Consumes: `git`, `_init_repo`, `_commit` (test helpers from Task 1).
- Produces:
  - `verify_tree_identical(worktree: Path, working_head: str, release_branch: str) -> dict` — `{"ok": bool, "tree_diff": str}`; `ok` iff `git diff working_head release_branch` is empty.
  - `rebuild(worktree: Path, release_branch: str, master: str, working_head: str, commit_plan: list) -> dict` — recreate `release_branch` at `master`, commit each `commit_plan` entry's files in order, return `verify_tree_identical(...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_release_prep.py`:
```python
def _setup_working(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    master = _commit(repo, "README.md", "root")
    _commit(repo, "src/pack.py", "packaging")
    _commit(repo, "src/mcp.py", "mcp")
    _commit(repo, "docs/x.md", "doc")
    working_head = rp.git("rev-parse", "HEAD", cwd=repo)
    return repo, master, working_head


def test_rebuild_is_tree_identical_and_grouped(tmp_path):
    repo, master, working_head = _setup_working(tmp_path)
    plan = [
        {"message": "pack: упаковка", "files": ["src/pack.py"]},
        {"message": "mcp: серверы", "files": ["src/mcp.py", "docs/x.md"]},
    ]
    res = rp.rebuild(repo, "release/test", master, working_head, plan)
    assert res["ok"] is True, res["tree_diff"]
    # exactly 2 commits over master, tree == working
    count = rp.git("rev-list", "--count", f"{master}..release/test", cwd=repo)
    assert count == "2"
    assert rp.git("diff", working_head, "release/test", cwd=repo) == ""


def test_rebuild_detects_orphan_file(tmp_path):
    repo, master, working_head = _setup_working(tmp_path)
    plan = [{"message": "pack", "files": ["src/pack.py"]}]  # omits src/mcp.py, docs/x.md
    res = rp.rebuild(repo, "release/test", master, working_head, plan)
    assert res["ok"] is False
    assert res["tree_diff"] != ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q -k rebuild`
Expected: FAIL — `module 'release_prep' has no attribute 'rebuild'`.

- [ ] **Step 3: Implement rebuild + verify**

Append to `release_prep.py`:
```python
def verify_tree_identical(worktree: Path, working_head: str, release_branch: str) -> dict:
    tree_diff = git("diff", working_head, release_branch, cwd=worktree, check=False)
    return {"ok": tree_diff == "", "tree_diff": tree_diff}


def rebuild(worktree: Path, release_branch: str, master: str,
            working_head: str, commit_plan: list) -> dict:
    # branch + worktree to working content, then move HEAD to master (mixed):
    # worktree stays at working, every diff vs master becomes unstaged.
    git("checkout", "-f", "-B", release_branch, working_head, cwd=worktree)
    git("reset", master, cwd=worktree)  # --mixed
    for commit in commit_plan:
        git("add", "--", *commit["files"], cwd=worktree)
        git("commit", "-q", "-m", commit["message"], cwd=worktree)
    return verify_tree_identical(worktree, working_head, release_branch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q -k rebuild`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills/release-prep
git add release_prep.py tests/test_release_prep.py
git commit -q -m "feat: squash-by-category rebuild + tree-identity verify"
```

---

### Task 3: Header numbers + sense + guarded publish

**Files:**
- Modify: `~/.claude/skills/release-prep/release_prep.py`
- Modify: `~/.claude/skills/release-prep/tests/test_release_prep.py`

**Interfaces:**
- Consumes: `git`, `_init_repo`, `_commit`, `_setup_working` (Tasks 1–2).
- Produces:
  - `header_numbers(repo: Path, master: str, head: str) -> dict` — `{"commits": int, "files": int, "insertions": int, "deletions": int}` over `master..head`.
  - `working_advanced(repo: Path, working_branch: str, last_seen_head) -> tuple` — `(changed: bool, head_sha: str)`.
  - `working_pushed(repo: Path, working_branch: str, remote: str = "origin") -> bool` — True iff `origin/<branch>` == local branch HEAD.
  - `publish(worktree: Path, release_branch: str, remote: str = "origin") -> None` — `--force-with-lease` push; raises `ValueError` if `release_branch` not prefixed `release/`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_release_prep.py`:
```python
import pytest


def test_header_numbers(tmp_path):
    repo, master, head = _setup_working(tmp_path)
    n = rp.header_numbers(repo, master, head)
    assert n["commits"] == 3
    assert n["files"] == 3            # src/pack.py, src/mcp.py, docs/x.md
    assert n["insertions"] >= 3
    assert n["deletions"] == 0


def test_working_advanced(tmp_path):
    repo, master, head = _setup_working(tmp_path)
    changed, sha = rp.working_advanced(repo, "master", head)
    assert sha == head
    assert changed is False
    changed2, _ = rp.working_advanced(repo, "master", "deadbeef")
    assert changed2 is True


def test_publish_refuses_non_release_branch(tmp_path):
    repo, master, head = _setup_working(tmp_path)
    with pytest.raises(ValueError):
        rp.publish(repo, "master")
    with pytest.raises(ValueError):
        rp.publish(repo, "feat/working")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q -k "header or advanced or publish"`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement numbers, sense, publish**

Append to `release_prep.py`:
```python
def header_numbers(repo: Path, master: str, head: str) -> dict:
    commits = int(git("rev-list", "--count", f"{master}..{head}", cwd=repo))
    files = len(changed_files(repo, master, head))
    ins = dels = 0
    numstat = git("diff", "--numstat", f"{master}..{head}", cwd=repo)
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        a, b = parts[0], parts[1]
        ins += 0 if a == "-" else int(a)
        dels += 0 if b == "-" else int(b)
    return {"commits": commits, "files": files, "insertions": ins, "deletions": dels}


def working_advanced(repo: Path, working_branch: str, last_seen_head):
    head = git("rev-parse", working_branch, cwd=repo)
    return (head != last_seen_head, head)


def working_pushed(repo: Path, working_branch: str, remote: str = "origin") -> bool:
    head = git("rev-parse", working_branch, cwd=repo)
    remote_sha = git("rev-parse", f"{remote}/{working_branch}", cwd=repo, check=False)
    return remote_sha == head


def publish(worktree: Path, release_branch: str, remote: str = "origin") -> None:
    if not release_branch.startswith("release/"):
        raise ValueError(f"refuse to push non-release branch: {release_branch}")
    git("push", "--force-with-lease", remote,
        f"{release_branch}:{release_branch}", cwd=worktree)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q`
Expected: PASS (all tests, 7 total).

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills/release-prep
git add release_prep.py tests/test_release_prep.py
git commit -q -m "feat: header numbers, sense (advanced/pushed), guarded publish"
```

---

### Task 4: Worktree ensure + CLI wiring

**Files:**
- Modify: `~/.claude/skills/release-prep/release_prep.py`
- Modify: `~/.claude/skills/release-prep/tests/test_release_prep.py`

**Interfaces:**
- Consumes: all Task 1–3 functions.
- Produces:
  - `ensure_worktree(repo: Path, worktree_path: Path, release_branch: str, master: str) -> Path` — idempotently register a linked worktree at `worktree_path` checked out to `release_branch` (created at `master` if absent). Returns the path.
  - `main(argv: list) -> int` — argparse CLI exposing subcommands: `sense`, `numbers`, `rebuild`, `verify`, `publish`. `rebuild` reads `commit_plan` JSON from `--plan <file>`; commands print JSON results to stdout.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_release_prep.py`:
```python
def test_ensure_worktree_idempotent(tmp_path):
    repo, master, head = _setup_working(tmp_path)
    wt = tmp_path / "wt"
    p1 = rp.ensure_worktree(repo, wt, "release/test", master)
    assert Path(p1).exists()
    # second call must not raise
    p2 = rp.ensure_worktree(repo, wt, "release/test", master)
    assert Path(p2) == Path(wt)


def test_cli_numbers(tmp_path, capsys):
    repo, master, head = _setup_working(tmp_path)
    rc = rp.main(["numbers", "--repo", str(repo), "--master", master, "--head", head])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["commits"] == 3


def test_cli_rebuild_with_plan_file(tmp_path, capsys):
    repo, master, head = _setup_working(tmp_path)
    plan = [
        {"message": "pack", "files": ["src/pack.py"]},
        {"message": "rest", "files": ["src/mcp.py", "docs/x.md"]},
    ]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    rc = rp.main(["rebuild", "--repo", str(repo), "--release", "release/test",
                  "--master", master, "--head", head, "--plan", str(plan_file)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q -k "worktree or cli"`
Expected: FAIL — missing `ensure_worktree` / `main`.

- [ ] **Step 3: Implement ensure_worktree + CLI**

Append to `release_prep.py`:
```python
import argparse
import sys


def ensure_worktree(repo: Path, worktree_path: Path, release_branch: str, master: str) -> Path:
    wt = Path(worktree_path)
    listing = git("worktree", "list", "--porcelain", cwd=repo, check=False)
    if str(wt.resolve()) in listing or wt.exists():
        return wt
    git("worktree", "add", "-B", release_branch, str(wt), master, cwd=repo)
    return wt


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="release_prep")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sense = sub.add_parser("sense")
    p_sense.add_argument("--repo", required=True)
    p_sense.add_argument("--branch", required=True)
    p_sense.add_argument("--last-seen", default=None)

    p_num = sub.add_parser("numbers")
    p_num.add_argument("--repo", required=True)
    p_num.add_argument("--master", required=True)
    p_num.add_argument("--head", required=True)

    p_reb = sub.add_parser("rebuild")
    for a in ("--repo", "--release", "--master", "--head", "--plan"):
        p_reb.add_argument(a, required=True)

    p_ver = sub.add_parser("verify")
    for a in ("--repo", "--release", "--head"):
        p_ver.add_argument(a, required=True)

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--repo", required=True)
    p_pub.add_argument("--release", required=True)
    p_pub.add_argument("--remote", default="origin")

    args = parser.parse_args(argv)

    if args.cmd == "sense":
        changed, head = working_advanced(Path(args.repo), args.branch, args.last_seen)
        print(json.dumps({"advanced": changed, "head": head,
                          "pushed": working_pushed(Path(args.repo), args.branch)}))
    elif args.cmd == "numbers":
        print(json.dumps(header_numbers(Path(args.repo), args.master, args.head)))
    elif args.cmd == "rebuild":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        res = rebuild(Path(args.repo), args.release, args.master, args.head, plan)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    elif args.cmd == "verify":
        res = verify_tree_identical(Path(args.repo), args.head, args.release)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    elif args.cmd == "publish":
        publish(Path(args.repo), args.release, args.remote)
        print(json.dumps({"pushed": args.release}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/.claude/skills/release-prep && python -m pytest tests/ -q`
Expected: PASS (10 total).

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills/release-prep
git add release_prep.py tests/test_release_prep.py
git commit -q -m "feat: ensure_worktree + argparse CLI (sense/numbers/rebuild/verify/publish)"
```

---

### Task 5: Author SKILL.md (6-phase orchestration) via writing-skills

**Files:**
- Create: `~/.claude/skills/release-prep/SKILL.md`

**Interfaces:**
- Consumes: the `release_prep.py` CLI (Tasks 1–4) and the spec.
- Produces: a triggerable skill whose body orchestrates phases Sense → Classify → Rebuild → Document → Self-review → Publish, delegating mechanics to the CLI and owning judgment (classification, table prose per §7 contract, self-review per §7.1).

- [ ] **Step 1: Invoke writing-skills**

Invoke `superpowers:writing-skills` to author the skill. Provide it: the spec path, the CLI contract below, and the RED/GREEN target (Step 4).

- [ ] **Step 2: Write SKILL.md frontmatter + body**

Frontmatter `description` MUST trigger on release-prep intents (RU/EN): «оформи изменения над master», «собери release-ветку / обзор изменений», «подготовь PR над master», "prepare release branch", "actualize CHANGES-vs-master". The body MUST encode, verbatim from the spec:

- **Config block** (top of skill, the tunables): `working_branch` (default `feat/agent-instructions-and-gc-guard`), `release_branch` (`release/agent-stack`), `worktree_path` (`../bg-release`), `master_ref` (`master`), `min_files_per_commit` (3), `interval` (`1h`), `autonomy` (`push-branch` | `local-only`, default `push-branch`), `damper` (off).
- **Phase 1 Sense:** `git fetch`; run `release_prep.py sense --repo <repo> --branch <working> --last-seen <state.last_seen_head>`. If `advanced=false` → **STOP, no-op** (skip Classify…Self-review). If `pushed=false` → push the working branch (data safety) before continuing.
- **Phase 2 Classify (incremental):** delta = `git diff <state.last_rebuild_head>..<head> --name-only` (full `master..head` on first run). Classify ONLY delta files into categories (the table's sections), merge into `state.file_category_map`. Build `commit_plan`: group by category, order by importance, FOLD categories with `< min_files_per_commit` files into a trailing "Сопровождение/прочее" entry. Verify: every `master..head` file is in exactly one entry.
- **Phase 3 Rebuild:** write `commit_plan` to a temp file; `release_prep.py rebuild ... --plan <file>`. If exit≠0 (`ok=false`) → **STOP, do not push**, surface `tree_diff`.
- **Phase 4 Document:** regenerate `docs/CHANGES-vs-master.md` in the worktree per the §7 content contract (every row carries что·почему·зачем·как·нюанс-флага; no fluff; sections + rows ordered by importance; importance glyph in Тип). Header numbers from `release_prep.py numbers`. Add the «дельта с прошлой пересборки» line. Commit the doc onto the release branch (amend into the relevant category commit OR a final `docs:` commit — keep tree-identity by NOT counting the doc against the working tree: the doc is a release-only artifact, so the tree-identity check compares working_head to the release branch BEFORE the doc commit — see Note).
- **Phase 5 Self-review (§7.1):** delta-scoped checklist; one bounded fix-pass; skipped on no-op.
- **Phase 6 Publish:** if `autonomy=push-branch`, `release_prep.py publish`. Then print a ready `gh pr create` command + the doc as PR body. NEVER open the PR.

**Note on tree-identity vs the release-only doc:** `CHANGES-vs-master.md` exists on the release branch but reflects the working branch; if the working branch already tracks its own (stale) copy, the doc differs. Resolve deterministically: the tree-identity verify (Phase 3) runs on the category commits BEFORE the Document commit. The Document commit is the single allowed deviation (a release-only `docs:` commit on top). Encode this explicitly so the invariant stays checkable: `git diff <working_head> <release_branch>~1` (i.e. release minus the doc commit) MUST be empty.

- [ ] **Step 3: Write the CLI contract reference into SKILL.md**

Include a compact table of the 5 subcommands with their flags and JSON outputs (from Task 4), so the orchestrating agent calls them correctly without re-deriving.

- [ ] **Step 4: RED/GREEN validation (skill behavior on a fixture)**

Create a throwaway synthetic repo (same shape as `_setup_working`) with a stale `docs/CHANGES-vs-master.md`. Drive the skill end-to-end against it with `autonomy=local-only` (no push). GREEN criteria:
1. `release/agent-stack` exists with category commits.
2. `git diff <working_head> release/agent-stack~1` is empty (invariant, doc commit excluded).
3. `docs/CHANGES-vs-master.md` header numbers match `release_prep.py numbers`.
4. No push occurred (local-only).
RED first: run with a deliberately incomplete `commit_plan` (omit a file) and confirm the skill STOPS at Phase 3 without pushing.

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills/release-prep
git add SKILL.md
git commit -q -m "feat: release-prep SKILL.md — 6-phase orchestration over the CLI"
```

---

### Task 6: Loop wrapper + on-demand doc + first live dry-run

**Files:**
- Create: `~/.claude/skills/release-prep/loop.md`

**Interfaces:**
- Consumes: the finished skill (Task 5).
- Produces: the exact `/loop` invocation and on-demand usage, plus a verified first real-repo run (no push).

- [ ] **Step 1: Write loop.md**

Document the two run modes:
- **Background:** in a SEPARATE terminal, run `/loop 1h <one-line prompt that invokes release-prep>`. The prompt: «Прогон release-prep: один цикл. Если рабочая ветка не продвинулась — no-op.» Note the cache/cost note: hourly wakes are cheap because Sense no-ops without tokens.
- **On-demand:** `/release-prep` from any terminal for an immediate cycle; with uncommitted changes the skill warns «таблица отражает только закоммиченное».
- The `autonomy` and `damper` tunables and how to flip them.

- [ ] **Step 2: First live dry-run on the real repo (LOCAL-ONLY, no push)**

Run the skill once against the real `background-agent` repo with `autonomy=local-only`:
- worktree created at `../bg-release`.
- `release/agent-stack` built; `git diff feat/agent-instructions-and-gc-guard release/agent-stack~1` empty.
- `docs/CHANGES-vs-master.md` regenerated; eyeball: header numbers match `461`-era reality (recompute live), every section present, rows informative per §7.
Expected: invariant holds; doc is a faithful, fluff-free superset of the current stale table. NO push.

- [ ] **Step 3: Surface to the user for review**

Show the user: the worktree path, `git log --oneline master..release/agent-stack`, and the regenerated table. Get explicit go-ahead before enabling `autonomy=push-branch`.

- [ ] **Step 4: Commit**

```bash
cd ~/.claude/skills/release-prep
git add loop.md
git commit -q -m "docs: loop wrapper + on-demand usage for release-prep"
```

- [ ] **Step 5: Commit the plan completion into the project repo**

```bash
cd /d/dev/sber/background-agent
git add docs/specs/2026-07-01-release-prep-plan.md
git commit -q -m "docs(plan): release-prep implementation plan"
```

---

## Self-Review

**Spec coverage:**
- §3 architecture (3 artifacts, worktree, invocation-≠-execution) → Tasks 4 (worktree), 5 (orchestration), 6 (loop/on-demand). ✓
- §4 six phases → Task 5 encodes all six; mechanics tested in Tasks 1–4. ✓
- §5 squash-by-category + invariant → Task 2 (tested). ✓
- §5.1 adaptive granularity (floor=1, min_files_per_commit fold, ceiling) → Task 5 Classify (folding is agent-side judgment per spec; CLI executes the resulting plan). ✓
- §6 state → Task 1; location refined to beside-skill (noted in Global Constraints). ✓
- §7 table content contract → Task 5 Phase 4 + Task 6 eyeball. ✓
- §7.1 self-review → Task 5 Phase 5. ✓
- §8 invocation modes → Task 6. ✓
- §9 cadence/autonomy/data-safety (push working if unpushed) → Task 3 (`working_pushed`) + Task 5 Phase 1 + Task 6. ✓
- §10 stacked задел → not built (YAGNI); `commit_plan`/category-map structure leaves room. ✓ (noted)
- §11 safety invariants → publish guard (Task 3), tree-identity (Task 2). ✓
- §12 dom of logic (skill + thin loop) → Tasks 5–6. ✓

**Placeholder scan:** No TBD/TODO; all code blocks complete; test code shown. The one judgment-heavy area (table prose authoring) is delegated to writing-skills with an explicit content contract and RED/GREEN fixture — not a placeholder. ✓

**Type consistency:** `commit_plan` entries are `{"message", "files"}` everywhere (Tasks 2, 4, 5). `verify_tree_identical`/`rebuild` return `{"ok", "tree_diff"}` consistently. CLI flag names (`--repo/--release/--master/--head/--plan`) consistent across Task 4 def and Task 5 reference. ✓

**Open item carried from spec §14:** post-merge cleanup of `@feat/...` install commands — belongs to the table content, surfaced in Task 6 eyeball, not a code task.
