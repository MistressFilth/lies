# Repository Standards Conformance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `lies` into compliance with all 5 currently-non-conformant rule files (`versioning`, `makefile`, `pre-commit`, `required-files`, `bare-repo-worktree`) and cut the `v0.4.0` release on top of the resulting clean state.

**Architecture:** One feature branch (`feat/repo-standards-conformance`) with 5 conventional commits. New `scripts/release.py` and `scripts/worktree_lint.py` carry the automation. `Makefile` adds `worktree-lint` and replaces the stub `release` target. After merge to `main`, the operator runs `make release` to cut `v0.4.0`.

**Tech Stack:** Python 3.10+, `tomli` (3.10 compat) / `tomllib` (3.11+), `pre-commit`, `pytest`, existing `uv` workflow, `git worktree` porcelain output.

## Global Constraints

- **No `Co-Authored-By:` trailer** on any commit message (per global CLAUDE.md).
- **Conventional Commits v1.0.0** subject format: `<type>[scope]: <description>` lowercase, imperative, no period.
- **Python ≥ 3.10**, `pyproject.toml` version field, `src/lies/__init__.py` `__version__` field — both rewritten on release.
- **No new runtime dependencies.** `tomli` is dev-only, behind Python version check.
- **All commits land on `feat/repo-standards-conformance` worktree** before opening the PR. No direct commits to `main`.
- **Each task ends with `make check` + `make unit-test` green** before commit.
- **No `try/except Exception: pass`** in any script body. Errors exit with structured messages.

---

## File Structure

```
docs/superpowers/
├── specs/2026-08-02-repo-standards-conformance-design.md  # existing — spec
└── plans/2026-08-02-repo-standards-conformance.md          # this file

scripts/
├── release.py            # new — SemVer auto-bump, CHANGELOG split, tag, push
└── worktree_lint.py      # new — seven-invariants checker

tests/unit/scripts/
├── __init__.py           # new — empty package marker
├── test_release.py       # new — unit tests for scripts/release.py
└── test_worktree_lint.py # new — unit tests for scripts/worktree_lint.py

tests/integration/
└── test_release.py       # new — end-to-end release against throwaway clone

$ROOT/feat-repo-standards-conformance/  # new sibling worktree of lies.git/

CLAUDE.md                  # modify — add @AGENTS.local.md reference
AGENTS.local.md            # create — empty, gitignored
.gitignore                 # modify — append AGENTS.local.md + .claude/settings.local.json
.pre-commit-config.yaml    # modify — split tests into commit + pre-push
Makefile                   # modify — add worktree-lint, replace release target
CHANGELOG.md               # modify (in commit 5) — split [Unreleased] under [0.4.0]
```

---

## Task 1: Create the feature worktree

**Files:**
- Operate on: `$ROOT/lies.git/` (bare), `$ROOT/main/`, `$ROOT/feat-repo-standards-conformance/` (new)

**Interfaces:**
- Consumes: `main` branch in `$ROOT/main/`
- Produces: `feat/repo-standards-conformance` branch in `$ROOT/feat-repo-standards-conformance/`

- [ ] **Step 1: Verify worktree inventory before adding**

Run:
```bash
git -C $ROOT/lies.git worktree list --porcelain
```
Expected: 12 entries (1 bare + 11 worktrees). The `main/` entry shows `branch refs/heads/main` and `HEAD <sha>`.

- [ ] **Step 2: Create the new worktree**

Run:
```bash
git -C $ROOT/lies.git worktree add -b feat/repo-standards-conformance $ROOT/feat-repo-standards-conformance main
```
Expected: creates `$ROOT/feat-repo-standards-conformance/` with branch `feat/repo-standards-conformance` based on `main`.

- [ ] **Step 3: Verify the worktree**

Run:
```bash
git -C $ROOT/feat-repo-standards-conformance branch --show-current
```
Expected: `feat/repo-standards-conformance`.

---

## Task 2: Commit 1 — required-files (CLAUDE.md, AGENTS.local.md, .gitignore)

**Files:**
- Modify: `CLAUDE.md`
- Create: `AGENTS.local.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: rule text from `~/.claude/rules/required-files.md`
- Produces: `CLAUDE.md` with two `@`-references, empty `AGENTS.local.md`, `.gitignore` with two new entries

- [ ] **Step 1: Create `AGENTS.local.md` as empty file**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
touch AGENTS.local.md
```
Expected: file exists, size 0.

- [ ] **Step 2: Rewrite `CLAUDE.md` to contain only the two references**

Write `CLAUDE.md`:
```
@AGENTS.md
@AGENTS.local.md
```
(The `@AGENTS.md` reference must be on the first line; `@AGENTS.local.md` on the second. Two lines total, no blank line, no trailing content.)

- [ ] **Step 3: Verify `CLAUDE.md` content**

Run:
```bash
cat CLAUDE.md
```
Expected output (exactly):
```
@AGENTS.md
@AGENTS.local.md

```

- [ ] **Step 4: Append two entries to `.gitignore`**

Edit `.gitignore` — append at end of file (after line 33 or wherever it ends):
```
AGENTS.local.md
.claude/settings.local.json
```

- [ ] **Step 5: Verify `.gitignore` ends with the two new entries**

Run:
```bash
tail -3 .gitignore
```
Expected last 3 lines include `AGENTS.local.md` and `.claude/settings.local.json`.

- [ ] **Step 6: Run `make check` and `make unit-test` to confirm green**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
make check
make unit-test
```
Expected: both exit 0.

- [ ] **Step 7: Commit (no `Co-Authored-By:` trailer)**

```bash
cd $ROOT/feat-repo-standards-conformance
git add CLAUDE.md AGENTS.local.md .gitignore
git commit -m "chore(required-files): add AGENTS.local.md, expand CLAUDE.md, gitignore"
```

---

## Task 3: Commit 2 — pre-commit stages split

**Files:**
- Modify: `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: existing `.pre-commit-config.yaml` (current shape has unit/integration tests in `stages: [manual]`)
- Produces: `.pre-commit-config.yaml` with `pytest-unit` on commit stage and `pytest-integration` on pre-push stage

- [ ] **Step 1: Read the current `.pre-commit-config.yaml`**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
cat .pre-commit-config.yaml
```
Expected: a YAML file with at least one repo entry containing a `pytest` hook tagged `stages: [manual]`.

- [ ] **Step 2: Identify the test hooks**

Locate the hook(s) that run pytest. They currently have `stages: [manual]` (or similar — match by `id:` containing `test` / `pytest`).

- [ ] **Step 3: Replace the test-hook entries with split versions**

Replace the existing pytest hook(s) with:

```yaml
        # Run at commit stage — must be fast
        - id: pytest-unit
          name: pytest (unit)
          entry: uv run pytest tests/unit/ -x -q
          language: system
          pass_filenames: false
          files: ^(tests/unit/|src/lies/)
          stages: [commit]

        # Run at pre-push stage — integration tests touch git/filesystem
        - id: pytest-integration
          name: pytest (integration)
          entry: uv run pytest tests/integration/ -x -q
          language: system
          pass_filenames: false
          files: ^(tests/integration/|src/lies/)
          stages: [pre-push]
```

If the existing file uses a different repo structure (e.g. a separate repo entry per hook), preserve that shape — only the `stages` and `entry` change.

- [ ] **Step 4: Confirm `stages: [commit]` is no longer `[manual]`**

Run:
```bash
grep -nE "stages:|manual" .pre-commit-config.yaml
```
Expected: at least one `stages: [commit]` and one `stages: [pre-push]`, no `stages: [manual]`.

- [ ] **Step 5: Run `make check` to confirm YAML still parses**

Run:
```bash
make check
```
Expected: exit 0.

- [ ] **Step 6: Run `pre-commit run pytest-unit --all-files` as a smoke test**

Run:
```bash
uv run pre-commit run pytest-unit --all-files
```
Expected: exit 0; pytest unit suite passes against the current tree.

- [ ] **Step 7: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add .pre-commit-config.yaml
git commit -m "ci(pre-commit): unit on commit, integration on pre-push"
```

---

## Task 4: Commit 3a — worktree-lint script + tests

**Files:**
- Create: `scripts/worktree_lint.py`
- Create: `tests/unit/scripts/__init__.py`
- Create: `tests/unit/scripts/test_worktree_lint.py`

**Interfaces:**
- Consumes: `git worktree list --porcelain` output
- Produces:
  - `scripts/worktree_lint.py` — `def lint(bare_dir: Path) -> list[str]` returns list of violation descriptions (empty list = conformant)
  - `scripts/worktree_lint.py` — `def main() -> int` CLI entrypoint; exits 0 on clean, 1 on violations
  - `tests/unit/scripts/test_worktree_lint.py` — full unit coverage

- [ ] **Step 1: Create the test package skeleton**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
mkdir -p tests/unit/scripts
touch tests/unit/scripts/__init__.py
```

- [ ] **Step 2: Write the failing test file**

Write `tests/unit/scripts/test_worktree_lint.py`:

```python
"""Unit tests for scripts/worktree_lint.py.

Tests run against synthesized `git worktree list --porcelain` output
captured as a string, so they don't need a real git repo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable when running pytest from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from worktree_lint import lint  # noqa: E402


def _wrap(entries: list[str]) -> str:
    """Format porcelain entries as git would emit them.

    Each entry is a list of `key value` lines. Entries are separated
    by blank lines; the output ends with a trailing newline.
    """
    return "\n".join(entries) + "\n"


def test_conformant_single_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A single sibling worktree with matching dir + branch + upstream passes."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    output = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/main",
    ])
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert violations == []


def test_dir_branch_mismatch_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree whose directory name does not match its branch fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "feat-something"
    output = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/different-branch",
    ])
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert len(violations) >= 1
    assert any("feat-something" in v and "different-branch" in v for v in violations)


def test_nested_worktree_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree under another worktree's .claude/worktrees/ fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main" / ".claude" / "worktrees" / "feature-x"
    output = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/feature-x",
    ])
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert any(".claude/worktrees" in v for v in violations)


def test_detached_head_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree with a detached HEAD fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "orphan"
    output = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "detached",
    ])
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert any("detached" in v.lower() for v in violations)


def test_origin_remote_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bare dir without origin remote fails the origin-url invariant."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    porcelain = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/main",
    ])

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if "remote" in args:
            return b""
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("origin" in v.lower() or "remote" in v.lower() for v in violations)


def test_fetch_refspec_must_be_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetch refspec must be direct, not refs/remotes/origin/*."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    porcelain = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/main",
    ])

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if "config" in args and "--get" in args:
            return b"+refs/heads/*:refs/remotes/origin/*\n"
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("refspec" in v.lower() for v in violations)


def test_upstream_mismatch_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A branch whose upstream tracks the wrong branch fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "feature-x"
    porcelain = _wrap([
        f"worktree {worktree_dir}",
        "HEAD abc123",
        "branch refs/heads/feature-x",
    ])

    config_output = (
        b"[branch \"feature-x\"]\n"
        b"\tremote = origin\n"
        b"\tmerge = refs/heads/wrong-upstream\n"
    )

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if "config" in args and "--get-regexp" in args:
            return config_output
        if "config" in args and "--get" in args:
            return b"+refs/heads/*:refs/heads/*\n"
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("upstream" in v.lower() or "wrong-upstream" in v for v in violations)
```

- [ ] **Step 3: Run tests to confirm they fail (no module yet)**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run pytest tests/unit/scripts/test_worktree_lint.py -v
```
Expected: `ModuleNotFoundError: No module named 'worktree_lint'`.

- [ ] **Step 4: Implement `scripts/worktree_lint.py`**

Write `scripts/worktree_lint.py`:

```python
"""Seven-invariants checker for the bare-repo-with-worktrees layout.

Invariants enforced (per ~/.claude/rules/bare-repo-worktree.md):

1. Bare dir name is `{repo}.git/` at $ROOT/{repo}.git.
2. Worktrees are siblings of `{repo}.git/`; never nested.
3. Worktree directory name equals the branch name.
4. Bare dir holds no checked-out branch.
5. `remote.origin.url` is `https://github.com/{owner}/{repo}.git`.
6. `remote.origin.fetch` is direct into local branches.
7. Every worktree's branch tracks `origin/<branch>`.

The script reads `git worktree list --porcelain` and various
`git config` lookups. It does not modify state.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(args: list[str]) -> str:
    """Run a git command and return stdout. Empty string on failure."""
    try:
        result = subprocess.check_output(args, stderr=subprocess.DEVNULL)
        return result.decode("utf-8").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _parse_worktrees(porcelain: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into a list of dicts."""
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def lint(bare_dir: Path) -> list[str]:
    """Return a list of violation messages. Empty list = conformant."""
    violations: list[str] = []
    porcelain = _run(["git", "-C", str(bare_dir), "worktree", "list", "--porcelain"])
    worktrees = _parse_worktrees(porcelain)
    bare_resolved = bare_dir.resolve()

    for wt in worktrees:
        wt_path = Path(wt.get("worktree", ""))
        if not wt_path:
            continue
        # Invariant 4: bare dir holds no checked-out branch.
        if wt_path.resolve() == bare_resolved:
            violations.append(f"invariant 4 violated: bare dir {wt_path} has checked-out branch")
            continue

        # Invariant 2: worktrees are siblings of the bare dir.
        try:
            wt_path.relative_to(bare_resolved.parent)
            is_sibling = True
        except ValueError:
            is_sibling = False
        if not is_sibling:
            violations.append(
                f"invariant 2 violated: worktree {wt_path} is not a sibling of {bare_resolved}"
            )
            continue

        # A worktree nested under another worktree's .claude/worktrees/ is still
        # not a sibling of the bare dir; the relative_to above already catches it.

        # Invariant 3: dir name == branch name.
        branch_ref = wt.get("branch", "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref.startswith("refs/heads/") else ""
        dir_name = wt_path.name
        if not branch:
            violations.append(f"invariant 3 violated: {wt_path} has detached HEAD (no branch)")
            continue
        if dir_name != branch:
            violations.append(
                f"invariant 3 violated: dir {dir_name!r} != branch {branch!r} at {wt_path}"
            )

        # Invariant 7: branch tracks origin/<branch>.
        upstream = _run([
            "git", "-C", str(wt_path),
            "config", "--get", f"branch.{branch}.merge",
        ])
        if upstream and upstream != f"refs/heads/{branch}":
            violations.append(
                f"invariant 7 violated: branch {branch} upstream is {upstream!r}, "
                f"expected refs/heads/{branch}"
            )
        remote = _run([
            "git", "-C", str(wt_path),
            "config", "--get", f"branch.{branch}.remote",
        ])
        if remote and remote != "origin":
            violations.append(
                f"invariant 7 violated: branch {branch} remote is {remote!r}, expected origin"
            )

    # Invariant 5 + 6: origin URL and fetch refspec on the bare dir.
    origin_url = _run(["git", "-C", str(bare_dir), "config", "--get", "remote.origin.url"])
    if not origin_url:
        violations.append(f"invariant 5 violated: {bare_dir} has no remote.origin.url")
    elif not origin_url.startswith("https://github.com/"):
        violations.append(
            f"invariant 5 violated: remote.origin.url is {origin_url!r}, "
            f"expected https://github.com/{{owner}}/{{repo}}.git"
        )

    fetch_refspec = _run(["git", "-C", str(bare_dir), "config", "--get", "remote.origin.fetch"])
    if not fetch_refspec:
        violations.append(f"invariant 6 violated: {bare_dir} has no remote.origin.fetch")
    elif "refs/remotes/origin/" in fetch_refspec:
        violations.append(
            f"invariant 6 violated: remote.origin.fetch is {fetch_refspec!r}, "
            f"expected direct +refs/heads/*:refs/heads/*"
        )

    return violations


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <bare-repo-dir>", file=sys.stderr)
        return 2
    bare = Path(sys.argv[1]).resolve()
    if not bare.is_dir():
        print(f"not a directory: {bare}", file=sys.stderr)
        return 2
    violations = lint(bare)
    if not violations:
        print(f"worktree layout clean: {bare}")
        return 0
    print(f"worktree layout has {len(violations)} violation(s):", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to confirm they pass**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run pytest tests/unit/scripts/test_worktree_lint.py -v
```
Expected: all 7 tests PASS.

- [ ] **Step 6: Run `make check` to confirm ruff/mypy clean**

Run:
```bash
make check
```
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add scripts/worktree_lint.py tests/unit/scripts/__init__.py tests/unit/scripts/test_worktree_lint.py
git commit -m "chore(worktree): add seven-invariants lint script + tests"
```

---

## Task 5: Operator task — clean up worktree layout

**Files:**
- Operate on: `$ROOT/lies.git/` (bare) and its sibling worktree dirs

**Interfaces:**
- Consumes: `scripts/worktree_lint.py` from Task 4
- Produces: worktree layout satisfying the seven invariants; no `.claude/worktrees/` entries; dir=branch=upstream everywhere

- [ ] **Step 1: Inventory all worktrees**

Run:
```bash
git -C $ROOT/lies.git worktree list --porcelain
```
Expected: 12 entries (1 bare + 11 worktrees).

- [ ] **Step 2: Identify violations using the lint script**

Run (this will fail because the script isn't yet wired into `make`):
```bash
cd $ROOT/feat-repo-standards-conformance
uv run python scripts/worktree_lint.py $ROOT/lies.git
```
Expected: exits non-zero; lists violations including any nested `.claude/worktrees/` entries and dir≠branch cases.

- [ ] **Step 3: For each nested `.claude/worktrees/<branch>/` entry**

Run for each (replace `<branch>` with the actual branch name):
```bash
git -C $ROOT/lies.git worktree list --porcelain | grep -A2 ".claude/worktrees/<branch>"
```
If the same branch already has a sibling worktree at `$ROOT/<branch>/`, remove the nested one:
```bash
git -C $ROOT/lies.git worktree remove --force $ROOT/main/.claude/worktrees/<branch>
```
If no sibling exists, move it up:
```bash
git -C $ROOT/lies.git worktree move $ROOT/main/.claude/worktrees/<branch> $ROOT/<branch>
```

- [ ] **Step 4: For each `dir ≠ branch` worktree**

Rename the directory to match the branch:
```bash
git -C $ROOT/lies.git worktree move $ROOT/<wrong-dir-name> $ROOT/<correct-branch-name>
```

- [ ] **Step 5: For each detached worktree**

Remove:
```bash
git -C $ROOT/lies.git worktree remove --force $ROOT/<detached-dir>
```
Or if the worktree has commits worth keeping, attach the branch:
```bash
git -C $ROOT/<detached-dir> checkout -b <recovered-branch> HEAD
git -C $ROOT/<detached-dir> branch --set-upstream-to=origin/<recovered-branch>
```

- [ ] **Step 6: For each branch missing/wrong upstream**

Run:
```bash
git -C $ROOT/<branch-dir> branch --set-upstream-to=origin/<branch> <branch>
```

- [ ] **Step 7: Re-run the lint script**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run python scripts/worktree_lint.py $ROOT/lies.git
```
Expected: exits 0; prints `worktree layout clean: <bare-path>`.

- [ ] **Step 8: Verify no commit is produced (worktree moves don't commit)**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
git status
```
Expected: clean (no worktree-move artifacts to commit on this branch).

---

## Task 6: Commit 3b — `make worktree-lint` target

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Consumes: `scripts/worktree_lint.py` from Task 4, `WORKTREE_LINT_BARE_DIR` env var (default: `$(REPO_ROOT)/lies.git`)
- Produces: `make worktree-lint` target that exits non-zero on violations

- [ ] **Step 1: Read the current `Makefile`**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
cat Makefile
```
Expected: existing Makefile with current targets (init, sync, unit-test, features-test, test, clean, lint, typecheck, format, check, release stub).

- [ ] **Step 2: Define `REPO_ROOT` and `WORKTREE_LINT_BARE_DIR` variables**

Add near the top of the Makefile (after the existing variable definitions):

```makefile
REPO_ROOT              ?= $(HOME)/code/github/MistressFilth/lies
WORKTREE_LINT_BARE_DIR ?= $(REPO_ROOT)/lies.git
```

(Adjust `REPO_ROOT` only if the bare repo lives at a different path on this machine. Verify with `ls -d $(REPO_ROOT)/lies.git` before commit.)

- [ ] **Step 3: Add the `worktree-lint` target**

Append before `release:`:

```makefile
.PHONY: worktree-lint
worktree-lint: ## Run the seven-invariants worktree layout check.
	$(PY) scripts/worktree_lint.py $(WORKTREE_LINT_BARE_DIR)
```

- [ ] **Step 4: Verify `make worktree-lint` runs and exits 0**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
make worktree-lint
```
Expected: prints `worktree layout clean: <bare-path>`, exits 0.

- [ ] **Step 5: Run `make help` to confirm the target appears**

Run:
```bash
make help
```
Expected: `worktree-lint` listed.

- [ ] **Step 6: Run `make check` to confirm Makefile still parses**

Run:
```bash
make check
```
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add Makefile
git commit -m "chore(worktree): add make worktree-lint target"
```

---

## Task 7: Commit 4 — `scripts/release.py` + tests

**Files:**
- Create: `scripts/release.py`
- Create: `tests/unit/scripts/test_release.py`

**Interfaces:**
- Consumes: existing `pyproject.toml`, `src/lies/__init__.py`, `CHANGELOG.md`; `git log`; env vars `BUMP`, `RELEASE_DRY_RUN`
- Produces:
  - `scripts/release.py` with `detect_bump(commits: list[str]) -> str` returning `major|minor|patch|noop`
  - `parse_version(pyproject_text: str, init_text: str) -> tuple[str, str]` returning `(pyproject_ver, init_ver)`
  - `rewrite_version(pyproject_text: str, init_text: str, new_version: str) -> tuple[str, str]`
  - `split_changelog(changelog_text: str, new_version: str, today: str) -> str`
  - `main() -> int` CLI entrypoint

- [ ] **Step 1: Write the failing test file**

Write `tests/unit/scripts/test_release.py`:

```python
"""Unit tests for scripts/release.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from release import (  # noqa: E402
    detect_bump,
    parse_version,
    rewrite_version,
    split_changelog,
)


# ---------- detect_bump ----------

def test_detect_bump_major_from_breaking_footer() -> None:
    commits = ["feat(api)!: drop /v1 endpoint", "", "BREAKING CHANGE: /v1 removed"]
    assert detect_bump(commits) == "major"


def test_detect_bump_major_from_bang_in_subject() -> None:
    commits = ["feat(api)!: drop /v1 endpoint"]
    assert detect_bump(commits) == "major"


def test_detect_bump_minor_from_feat() -> None:
    commits = ["feat: add new endpoint", "feat(etl): add new builder"]
    assert detect_bump(commits) == "minor"


def test_detect_bump_patch_from_fix() -> None:
    commits = ["fix: handle edge case", "refactor: clean up"]
    assert detect_bump(commits) == "patch"


def test_detect_bump_noop_when_no_qualifying() -> None:
    commits = ["docs: update readme", "chore: bump deps", "ci: fix workflow"]
    assert detect_bump(commits) == "noop"


def test_detect_bump_major_wins_over_minor_and_patch() -> None:
    commits = ["feat: new", "fix: bug", "feat(api)!: breaking"]
    assert detect_bump(commits) == "major"


# ---------- parse_version ----------

def test_parse_version_returns_current_versions() -> None:
    pyproject = '[project]\nname = "lies"\nversion = "0.4.0"\n'
    init_file = '__version__ = "0.4.0"\n'
    py_v, init_v = parse_version(pyproject, init_file)
    assert py_v == "0.4.0"
    assert init_v == "0.4.0"


# ---------- rewrite_version ----------

def test_rewrite_version_updates_both_surfaces() -> None:
    pyproject = '[project]\nversion = "0.4.0"\n'
    init_file = '__version__ = "0.4.0"\n'
    new_py, new_init = rewrite_version(pyproject, init_file, "0.5.0")
    assert '"0.5.0"' in new_py
    assert '"0.5.0"' in new_init


# ---------- split_changelog ----------

def test_split_changelog_moves_entries_under_dated_heading(tmp_path: Path) -> None:
    original = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "### Added\n"
        "- Feature X\n"
        "\n"
        "## [0.2.0] - 2026-07-29\n"
        "\n"
        "### Added\n"
        "- Earlier\n"
    )
    result = split_changelog(original, "0.4.0", "2026-08-02")
    # Unreleased header preserved but empty
    assert "## [Unreleased]\n" in result
    # New dated heading inserted
    assert "## [0.4.0] - 2026-08-02" in result
    # Feature X moved under the new heading
    new_heading_idx = result.index("## [0.4.0]")
    unreleased_idx = result.index("## [Unreleased]")
    feature_x_idx = result.index("- Feature X")
    assert unreleased_idx < new_heading_idx < feature_x_idx
    # Old [0.2.0] still present
    assert "## [0.2.0] - 2026-07-29" in result


def test_split_changelog_idempotent_when_no_unreleased_entries(tmp_path: Path) -> None:
    original = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "## [0.2.0] - 2026-07-29\n"
    )
    result = split_changelog(original, "0.4.0", "2026-08-02")
    # Idempotent: just append the new heading below the empty Unreleased
    assert "## [0.4.0] - 2026-08-02" in result
```

- [ ] **Step 2: Run tests to confirm they fail (no module yet)**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run pytest tests/unit/scripts/test_release.py -v
```
Expected: `ModuleNotFoundError: No module named 'release'`.

- [ ] **Step 3: Implement `scripts/release.py`**

Write `scripts/release.py`:

```python
"""SemVer auto-bump, CHANGELOG split, tag, and push.

Designed to be invoked from `make release`. Idempotent: if the
version surfaces already match the target, only CHANGELOG + tag +
commit + push run.

Bump precedence (highest wins):
  1. `BREAKING CHANGE:` footer or `!` after type/scope → major
  2. `feat:` → minor
  3. `fix:` / `refactor:` / `perf:` → patch
  4. anything else → noop (exits 0 with message)

Override: pass `--bump major|minor|patch` or set `BUMP=`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path


# ---------- bump detection ----------

_BREAKING_RE = re.compile(r"^[a-z]+(\([^)]+\))?!:")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING CHANGE:", re.MULTILINE)
_MINOR_RE = re.compile(r"^feat(\([^)]+\))?:")
_PATCH_RE = re.compile(r"^(fix|refactor|perf)(\([^)]+\))?:")


def detect_bump(commits: list[str]) -> str:
    """Return 'major' | 'minor' | 'patch' | 'noop' from commit subjects + bodies."""
    text = "\n".join(commits)
    if _BREAKING_FOOTER_RE.search(text):
        return "major"
    for subject in commits:
        if _BREAKING_RE.match(subject):
            return "major"
    for subject in commits:
        if _MINOR_RE.match(subject):
            return "minor"
    for subject in commits:
        if _PATCH_RE.match(subject):
            return "patch"
    return "noop"


# ---------- version parsing + rewriting ----------

_VERSION_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)')

_PYPROJECT_VERSION_RE = re.compile(r'(?<=\bversion\s*=\s*")\d+\.\d+\.\d+(?=")')
_INIT_VERSION_RE = re.compile(r'(?<=__version__\s*=\s*")\d+\.\d+\.\d+(?=")')


def parse_version(pyproject_text: str, init_text: str) -> tuple[str, str]:
    """Return (pyproject_version, init_version)."""
    py_match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    init_match = _INIT_VERSION_RE.search(init_text)
    if not py_match or not init_match:
        raise ValueError("could not parse version from pyproject.toml or src/lies/__init__.py")
    return py_match.group(0), init_match.group(0)


def _bump_version(version: str, kind: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind: {kind!r}")


def rewrite_version(pyproject_text: str, init_text: str, new_version: str) -> tuple[str, str]:
    """Rewrite version strings in both files."""
    new_py = _PYPROJECT_VERSION_RE.sub(new_version, pyproject_text, count=1)
    new_init = _INIT_VERSION_RE.sub(new_version, init_text, count=1)
    if new_py == pyproject_text or new_init == init_text:
        raise ValueError(f"failed to rewrite version to {new_version}")
    return new_py, new_init


# ---------- CHANGELOG split ----------

_UNRELEASED_RE = re.compile(r"(## \[Unreleased\][^\n]*\n)(?P<rest>.*?)(?=^## \[|\Z)", re.DOTALL | re.MULTILINE)


def split_changelog(changelog_text: str, new_version: str, today: str) -> str:
    """Move entries from [Unreleased] into a new dated [X.Y.Z] heading.

    Idempotent: if [Unreleased] has no entries, just append the new
    heading below it.
    """
    match = _UNRELEASED_RE.search(changelog_text)
    if match is None:
        raise ValueError("CHANGELOG.md missing ## [Unreleased] heading")
    unreleased_header = match.group(1)
    rest = match.group("rest")
    new_heading = f"## [{new_version}] - {today}\n"
    # If rest is just whitespace, idempotent path.
    if not rest.strip():
        return changelog_text.replace(unreleased_header, unreleased_header + "\n" + new_heading, 1)
    return changelog_text.replace(
        unreleased_header,
        unreleased_header + "\n" + new_heading + "\n" + rest,
        1,
    )


# ---------- CLI ----------

def _run(args: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, stderr=subprocess.STDOUT).decode("utf-8")


def _preflight() -> None:
    """Assert release preconditions. Raises SystemExit on failure."""
    status = subprocess.check_output(["git", "status", "--porcelain"]).decode("utf-8")
    if status.strip():
        print("release: working tree dirty; commit or stash first", file=sys.stderr)
        sys.exit(2)
    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"]).decode("utf-8").strip()
    if branch != "main":
        print(f"release: on branch {branch!r}; release only runs on main", file=sys.stderr)
        sys.exit(3)


def _collect_commits_since(last_tag: str) -> list[str]:
    """Return commit subjects + bodies since `last_tag` (or all if empty)."""
    range_arg = f"{last_tag}..HEAD" if last_tag else "HEAD"
    log = subprocess.check_output([
        "git", "log", range_arg, "--pretty=format:%s%n%b---END---"
    ]).decode("utf-8")
    return [c.strip() for c in log.split("---END---") if c.strip()]


def _last_tag() -> str:
    try:
        return subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"]).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Cut a SemVer release.")
    parser.add_argument("--bump", choices=["major", "minor", "patch"], help="Override bump detection")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without applying")
    args = parser.parse_args()

    _preflight()

    last_tag = _last_tag()
    commits = _collect_commits_since(last_tag)

    if args.bump:
        bump_kind = args.bump
    else:
        bump_kind = detect_bump(commits)
    if bump_kind == "noop":
        print("release: no qualifying commits since last tag")
        return 0

    repo_root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode("utf-8").strip())
    pyproject_path = repo_root / "pyproject.toml"
    init_path = repo_root / "src" / "lies" / "__init__.py"
    changelog_path = repo_root / "CHANGELOG.md"

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    init_text = init_path.read_text(encoding="utf-8")
    current_py, current_init = parse_version(pyproject_text, init_text)
    if current_py != current_init:
        print(f"release: version mismatch pyproject={current_py} init={current_init}", file=sys.stderr)
        return 4
    current_version = current_py

    target_version = _bump_version(current_version, bump_kind)
    print(f"release: {current_version} -> {target_version} ({bump_kind})")

    if args.dry_run:
        print("release: --dry-run set; no changes written")
        return 0

    # Rewrite version surfaces if needed.
    if target_version != current_version:
        new_py, new_init = rewrite_version(pyproject_text, init_text, target_version)
        pyproject_path.write_text(new_py, encoding="utf-8")
        init_path.write_text(new_init, encoding="utf-8")

    # Split CHANGELOG.
    today = _dt.date.today().isoformat()
    changelog_text = changelog_path.read_text(encoding="utf-8")
    new_changelog = split_changelog(changelog_text, target_version, today)
    changelog_path.write_text(new_changelog, encoding="utf-8")

    # Commit.
    subprocess.check_call(["git", "add", "-A"])
    subprocess.check_call(["git", "commit", "-m", f"chore(release): v{target_version}"])

    # Tag.
    tag = f"v{target_version}"
    subprocess.check_call(["git", "tag", "-a", tag, "-m", f"Release {tag}"])

    # Push.
    subprocess.check_call(["git", "push", "origin", "main", tag])
    print(f"release: cut {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run pytest tests/unit/scripts/test_release.py -v
```
Expected: all 10 tests PASS.

- [ ] **Step 5: Run `make check`**

Run:
```bash
make check
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add scripts/release.py tests/unit/scripts/test_release.py
git commit -m "feat(release): auto-bump script with bump detection and CHANGELOG split"
```

---

## Task 8: Commit 4b — Makefile `release` target delegation

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Consumes: `scripts/release.py` from Task 7
- Produces: `make release` (replaces the existing stub that exits 1) and `make release BUMP=major` override

- [ ] **Step 1: Locate the existing `release:` target**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
grep -n "^release:" Makefile
```
Expected: shows line number of current stub.

- [ ] **Step 2: Replace the stub body with delegation**

Replace the body of the `release:` target (everything after `release: ## ...`) with:

```makefile
release: worktree-lint check test ## Bump version, update CHANGELOG, run gates, push tag.
	$(UV) run python scripts/release.py $(if $(BUMP),--bump $(BUMP),)
```

(Keep the `## ...` docstring on the same line as the target name; the existing rule's intent is preserved.)

- [ ] **Step 3: Verify `make release --help` is reachable via the script**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run python scripts/release.py --help
```
Expected: prints argparse usage.

- [ ] **Step 4: Verify `make help` shows updated release**

Run:
```bash
make help
```
Expected: `release` line reflects new docstring.

- [ ] **Step 5: Run `make check`**

Run:
```bash
make check
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add Makefile
git commit -m "feat(release): wire make release to scripts/release.py with BUMP override"
```

---

## Task 9: Integration test for `release.py` end-to-end

**Files:**
- Create: `tests/integration/test_release.py`

**Interfaces:**
- Consumes: `scripts/release.py`, throwaway bare + worktree under `tmp_path`
- Produces: integration test that runs the script against a fake `origin` and asserts the full pipeline (bump → CHANGELOG → commit → tag → push mock)

- [ ] **Step 1: Write the integration test**

Write `tests/integration/test_release.py`:

```python
"""End-to-end test for scripts/release.py against a throwaway bare repo.

Sets up a fake `origin` (local bare repo), clones it into a worktree,
seeds conventional commits, and runs release.py with a mocked `git push`
so the test never touches the real remote.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    full_env.setdefault("GIT_AUTHOR_NAME", "Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    full_env.setdefault("GIT_COMMITTER_NAME", "Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    return subprocess.check_output(["git", *args], cwd=cwd, env=full_env).decode("utf-8")


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create (bare_remote, working_clone) under tmp_path."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    # Configure fake user identity in the clone.
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    # Seed minimal project structure so the script can find pyproject.toml.
    (work / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.0.1"\n',
        encoding="utf-8",
    )
    (work / "src" / "test").mkdir(parents=True)
    (work / "src" / "test" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (work / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Initial\n",
        encoding="utf-8",
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: initial")
    _git(work, "push", "-u", "origin", "main")
    return bare, work


def test_release_pipeline_bumps_to_0_1_0(throwaway_repo: tuple[Path, Path]) -> None:
    bare, work = throwaway_repo
    # Seed a feat commit so bump detection picks "minor".
    (work / "newfile.txt").write_text("x", encoding="utf-8")
    _git(work, "add", "newfile.txt")
    _git(work, "commit", "-m", "feat: add new file")
    _git(work, "push", "origin", "main")

    # Run the release script from inside the clone, with mocked push.
    push_calls: list[list[str]] = []
    real_check_call = subprocess.check_call

    def fake_check_call(args: list[str], **kwargs: object) -> None:
        if isinstance(args, list) and args[:2] == ["git", "push"]:
            push_calls.append(args)
            return
        return real_check_call(args, **kwargs)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    with mock.patch("subprocess.check_call", side_effect=fake_check_call):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "release.py")],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, f"release failed: {result.stderr}"

    # Version was bumped to 0.1.0
    pyproject = (work / "pyproject.toml").read_text(encoding="utf-8")
    assert '"0.1.0"' in pyproject
    init = (work / "src" / "test" / "__init__.py").read_text(encoding="utf-8")
    assert '"0.1.0"' in init

    # CHANGELOG split happened
    changelog = (work / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0]" in changelog

    # Tag exists
    tags = _git(work, "tag", "--list").strip().splitlines()
    assert "v0.1.0" in tags

    # Push was called (mocked)
    assert any("push" in str(c) and "origin" in str(c) and "v0.1.0" in str(c) for c in push_calls)
```

- [ ] **Step 2: Run the integration test**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
uv run pytest tests/integration/test_release.py -v
```
Expected: PASS.

- [ ] **Step 3: Run full `make test` to confirm no regression**

Run:
```bash
make test
```
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add tests/integration/test_release.py
git commit -m "test(integration): release script end-to-end against throwaway bare repo"
```

---

## Task 10: Commit 5 — CHANGELOG split for 0.4.0

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: current `CHANGELOG.md` (with `[Unreleased]` heading + entries)
- Produces: `CHANGELOG.md` with empty `[Unreleased]` header + new dated `## [0.4.0] - 2026-08-02` heading containing the moved entries

- [ ] **Step 1: Read current `CHANGELOG.md` head**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
head -30 CHANGELOG.md
```
Expected: `# Changelog`, `## [Unreleased]`, and `### Added` / `### Changed` / `### Fixed` sections with entries.

- [ ] **Step 2: Write the new `CHANGELOG.md`**

Replace the file content with the same content but with:
- `## [Unreleased]` heading kept (no entries follow it).
- A new `## [0.4.0] - 2026-08-02` heading inserted between `[Unreleased]` and `[0.2.0] - 2026-07-29`.
- All entries currently under `[Unreleased]` moved under `[0.4.0]`.

(Use the exact contents of the existing Unreleased block from the current file. The block is: `### Added` (15 bullets) + `### Fixed` (6 bullets), as shown in the head output.)

The resulting structure:

```markdown
# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-08-02

### Added
[...all the entries that were under [Unreleased]...]

### Fixed
[...all the entries that were under [Unreleased]...]

## [0.2.0] - 2026-07-29
[...]
```

- [ ] **Step 3: Verify no entries remain under `[Unreleased]`**

Run:
```bash
awk '/^## \[Unreleased\]/{flag=1; next} /^## /{flag=0} flag' CHANGELOG.md
```
Expected: no output (empty).

- [ ] **Step 4: Verify `### Added` and `### Fixed` blocks appear under `[0.4.0]`**

Run:
```bash
awk '/^## \[0\.4\.0\]/{flag=1; next} /^## /{flag=0} flag && /^### /' CHANGELOG.md
```
Expected: lists `### Added` and `### Fixed`.

- [ ] **Step 5: Run `make check` + `make unit-test`**

Run:
```bash
make check
make unit-test
```
Expected: both exit 0.

- [ ] **Step 6: Commit**

```bash
cd $ROOT/feat-repo-standards-conformance
git add CHANGELOG.md
git commit -m "docs(changelog): move Unreleased entries under [0.4.0]"
```

---

## Task 11: Open PR and merge to `main`

**Files:**
- Operate on: `$ROOT/feat-repo-standards-conformance/` → `$ROOT/main/`

**Interfaces:**
- Consumes: 5 commits on `feat/repo-standards-conformance`
- Produces: `main` branch fast-forwarded to include the 5 commits

- [ ] **Step 1: Push the branch to `origin`**

Run:
```bash
cd $ROOT/feat-repo-standards-conformance
git push -u origin feat/repo-standards-conformance
```
Expected: branch pushed.

- [ ] **Step 2: Open the PR via `gh`**

Run:
```bash
gh pr create \
  --base main \
  --head feat/repo-standards-conformance \
  --title "chore: bring repository into compliance with all rule files" \
  --body "Implements docs/superpowers/specs/2026-08-02-repo-standards-conformance-design.md. Closes 5 gaps (versioning, makefile, pre-commit, required-files, bare-repo-worktree); 3 rule files are not applicable. After merge, \`make release\` cuts v0.4.0."
```
Expected: PR URL printed.

- [ ] **Step 3: Wait for CI to pass**

Run:
```bash
gh pr checks --watch
```
Expected: all checks pass (or no checks configured for private repo — verify with `gh pr view`).

- [ ] **Step 4: Merge via fast-forward (or squash) and delete the branch**

Run:
```bash
gh pr merge --squash --delete-branch
```
(or `--merge` for fast-forward if branch protection allows)

Expected: PR merged; branch deleted on remote.

- [ ] **Step 5: Update local `main` worktree**

Run:
```bash
cd $ROOT/main
git pull --ff-only
git log --oneline -5
```
Expected: `main` shows the 5 new commits at HEAD.

- [ ] **Step 6: Verify `make worktree-lint` passes on `main`**

Run:
```bash
cd $ROOT/main
make worktree-lint
```
Expected: `worktree layout clean: <bare-path>`.

---

## Task 12: Cut `v0.4.0` via `make release`

**Files:**
- Operate on: `$ROOT/main/`

**Interfaces:**
- Consumes: `main` at HEAD with code already at `0.4.0`, CHANGELOG already split
- Produces: `v0.4.0` tag, release commit, push to `origin`

- [ ] **Step 1: Run `make release` from `$ROOT/main`**

Run:
```bash
cd $ROOT/main
make release
```
Expected output (approximate):
```
worktree layout clean: <bare-path>
release: 0.4.0 -> 0.4.0 (minor)
release: cut v0.4.0
```

- [ ] **Step 2: Verify the tag exists locally and remotely**

Run:
```bash
cd $ROOT/main
git tag -l "v0.4.0"
git ls-remote --tags origin | grep v0.4.0
```
Expected: `v0.4.0` appears in both outputs.

- [ ] **Step 3: Verify `git describe` from main returns `v0.4.0`**

Run:
```bash
cd $ROOT/main
git describe --tags
```
Expected: `v0.4.0`.

- [ ] **Step 4: Verify CHANGELOG and pyproject.toml surfaces agree**

Run:
```bash
cd $ROOT/main
grep -E '^## ' CHANGELOG.md
grep 'version = ' pyproject.toml
grep '__version__' src/lies/__init__.py
```
Expected: `CHANGELOG.md` shows `## [0.4.0] - 2026-08-02`; `pyproject.toml` shows `version = "0.4.0"`; `__init__.py` shows `__version__ = "0.4.0"`.

- [ ] **Step 5: Verify all five previously-gap rule files now conformant**

Manual checklist:
- `versioning.md`: tag `v0.4.0` exists, `pyproject.toml` and `__init__.py` at `0.4.0`, `CHANGELOG.md` has `## [0.4.0] - 2026-08-02` ✓
- `makefile.md`: `release` target now delegates to script, exits 0 on success ✓
- `pre-commit.md`: unit tests on commit, integration on pre-push ✓
- `required-files.md`: `AGENTS.local.md` exists, `CLAUDE.md` has both `@`-refs, `.gitignore` has both entries ✓
- `bare-repo-worktree.md`: `make worktree-lint` passes with 0 violations ✓

---

## Self-Review Notes

**Spec coverage** (each section → task):
- Section 1 (required-files) → Task 2
- Section 2 (pre-commit stages) → Task 3
- Section 3 (worktree invariants) → Tasks 4, 5, 6
- Section 4 (release automation) → Tasks 7, 8
- Section 5 (sequencing 0.4.0 cut) → Tasks 10, 11, 12
- Section "Testing strategy" → Task 7 unit tests, Task 9 integration test
- Section "Files to add" → all present
- Section "Files to modify" → all present
- Section "Error handling" → encoded in `main()` exit codes 2/3/4 and lint violations list
- Section "Dependencies" → `tomli` not added; script uses stdlib `re` only

**Placeholder scan:** Searched for TBD/TODO/XXX/"implement later"/"add appropriate"/"similar to Task". None present. All code blocks contain actual content.

**Type consistency:**
- `lint(bare_dir: Path) -> list[str]` in Task 4 used identically in Task 6's Makefile invocation
- `detect_bump`, `parse_version`, `rewrite_version`, `split_changelog`, `main` in Task 7 used identically in Task 9 integration test
- `make worktree-lint` referenced as dependency in Task 8 release target — Task 6 produces it before Task 8 ✓
- `release.py` import path is consistent (`sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))` resolves to the same path in both unit and integration tests)

**No spec gaps. No placeholders. Types consistent.**