# Repository Standards Conformance — Design Spec

**Date:** 2026-08-02
**Status:** Approved (brainstorming complete; awaiting writing-plans)
**Scope:** Bring `lies` into compliance with all 12 rule files under `~/.claude/rules/` and cut the 0.4.0 release on top of the resulting clean state.

## Goal

The `lies` repository currently conforms to 4 of the 12 rule files. The remaining 5 real gaps (`versioning`, `makefile`, `pre-commit`, `required-files`, `bare-repo-worktree`) are fixed in one PR; the remaining 3 (`claude-code-plugins`, `github-repo-protection`, `opencode-plugins`) are not applicable because `lies` is a Python application, not a Claude Code or OpenCode plugin, and the GitHub repo is private (rulesets require Pro or public).

After the conformance PR merges to `main`, `make release` cuts `v0.4.0` automatically, satisfying the `versioning` rule's release-surface requirement.

## User decisions

- **Scope:** All 5 real gaps addressed in this work.
- **Versioning:** Cut `0.4.0` release now (current code already at `0.4.0` in `pyproject.toml` and `src/lies/__init__.py`).
- **Worktrees:** Enforce all 7 invariants from `bare-repo-worktree.md` (rename dirs, move nested `.claude/worktrees/` up, reset upstream tracking).
- **Release automation:** Full auto-bump + push from `make release`. Bump type derived from conventional commits since last tag. `BUMP=` override supported.
- **Test gating:** `tests/unit/` runs at commit stage; `tests/integration/` runs at pre-push stage.

## Non-goals

- Adding GitHub branch-protection rulesets (private repo, not applicable without Pro upgrade).
- Restructuring `src/` or `tests/` (both already conformant to `repository-layout.md`).
- Converting `lies` into a Claude Code or OpenCode plugin (out of scope).
- Releasing anything other than `0.4.0` from current main (no `0.5.0`-or-higher unless breaking commits are detected).

## Architecture

The conformance PR contains five sequenced commits on `feat/repo-standards-conformance`:

1. **`chore(required-files): add AGENTS.local.md, expand CLAUDE.md, gitignore`** — Section 1
2. **`ci(pre-commit): unit on commit, integration on pre-push`** — Section 2
3. **`chore(worktree): enforce seven invariants, add make worktree-lint`** — Section 3
4. **`feat(release): auto-bump script + make release automation`** — Section 4
5. **`docs(changelog): move Unreleased entries under [0.4.0]`** — Section 5 (preparation)

After fast-forward merge to `main`, the operator runs `make release`, which executes `scripts/release.py`. The script:

- Preflights: cwd clean, branch=main, upstream in sync, `make worktree-lint`, `make check`, `make test`.
- Detects bump from conventional commits since last tag.
- Rewrites `pyproject.toml` `version` and `src/lies/__init__.py` `__version__` if needed.
- Splits `CHANGELOG.md` `[Unreleased]` into `[Unreleased]` (empty) + `[X.Y.Z] - YYYY-MM-DD`.
- Commits as `chore(release): vX.Y.Z`.
- Tags `vX.Y.Z`.
- Pushes `main` and `vX.Y.Z` to `origin`.

```text
feature worktree at $ROOT/feat-repo-standards-conformance/
    │
    ├── commit 1 (required-files)
    ├── commit 2 (pre-commit)
    ├── commit 3 (worktree)
    ├── commit 4 (release automation)
    └── commit 5 (CHANGELOG split)
        │
        ▼  PR + fast-forward merge
        │
$ROOT/main/  (now on 0.4.0 code, [Unreleased] empty, [0.4.0] heading dated)
        │
        ▼  make release
        │
scripts/release.py:
    ├── preflight (worktree-lint, check, test, branch, sync)
    ├── detect bump from commits since v0.2.0 (or 0.0.0 if no tags)
    ├── verify target = 0.4.0 (matches code already at 0.4.0)
    ├── no-op version rewrite (already 0.4.0)
    ├── split CHANGELOG (already split in commit 5)
    ├── commit "chore(release): v0.4.0"
    ├── tag v0.4.0
    └── push origin main v0.4.0
```

## Section 1 — Required files and gitignore

Three additive changes:

| File | Change |
|---|---|
| `AGENTS.local.md` | Create (empty file, ignored). Personal note scratchpad per `required-files.md`. |
| `CLAUDE.md` | Replace `@AGENTS.md` with two lines: `@AGENTS.md` + `@AGENTS.local.md`. Per rule, must contain only these two `@`-references and nothing else. |
| `.gitignore` | Append `AGENTS.local.md` and `.claude/settings.local.json`. Both already excluded via `~/.config/git/ignore` globally, but the rule requires them in the project ignore for portability. |

No `Co-Authored-By:` trailer on the commit.

## Section 2 — Pre-commit stages

Current `.pre-commit-config.yaml` has the test hook tagged `stages: [manual]`. The rule requires a commit that lands in the repo to have passed all gates. Resolution:

| Hook | Stage | Why |
|---|---|---|
| `ruff-check` (lint) | `commit` | Fast, must block bad style |
| `ruff-format` (format) | `commit` | Fast, auto-fixes allowed by `format` target |
| `mypy` (typecheck) | `commit` | Must block type errors |
| `pytest tests/unit/` | `commit` | Fast, gating per rule |
| `pytest tests/integration/` | `pre-push` | Slow; gates pushes but not commits |

Use `pre-commit`'s `default_install_hook_types: [pre-commit, pre-push]`. Remove the `[manual]` qualifier on tests. Add a `pre-push` hook that runs `make features-test` (which already invokes `tests/integration/`).

`make check` (lint + typecheck + format) remains the canonical local target for the commit-stage hooks.

## Section 3 — Worktree invariants

The current state under `~/code/github/MistressFilth/lies/`:
- Bare at `lies.git/`. ✓
- Main sibling at `main/`. ✓
- 11 additional worktrees, several with `dir ≠ branch` (e.g. `feat-lies-mvp/` ↔ `feat/lies-mvp`).
- 5 worktrees nested under `main/.claude/worktrees/` (agent-runtime worktrees).

Action categories:

| Category | Detection | Action |
|---|---|---|
| A. Sibling, dir=branch, tracks origin/branch | `git worktree list --porcelain` matches all three | None — conformant |
| B. Sibling, dir≠branch | Path basename ≠ branch name | Rename directory to match branch name (rule says dir=branch, not the other way) |
| C. Nested under `.claude/worktrees/` | Path starts with `main/.claude/worktrees/` | If branch already has a sibling worktree → remove the nested copy (dup). Else: `git worktree move` to `$ROOT/<branch>/` |
| D. Sibling, no tracking or wrong upstream | `branch.<name>.merge` missing or pointing elsewhere | `git branch --set-upstream-to=origin/<branch>` |
| E. Detached HEAD | `git worktree list` shows `detached` | Attach to expected branch or `git worktree remove` |

**New Makefile target** `make worktree-lint` invokes a verification script (`scripts/worktree_lint.py`) that asserts the seven invariants and exits non-zero on any violation. The release script's preflight runs this.

**Pre-conditions for the rename moves:**
- All worktrees must be clean (`git status --porcelain` empty per worktree).
- No agent sessions running against nested worktrees (operator verifies).

## Section 4 — Makefile `release` automation

**New script:** `scripts/release.py` (Python). The Makefile target is a thin wrapper.

```makefile
.PHONY: release
release: ## Bump version, update CHANGELOG, run gates, push tag.
	$(UV) run python scripts/release.py $(if $(BUMP),--bump $(BUMP),)
```

**Script flow:**

```text
release.py
  ├── preflight
  │     ├── cwd clean (git status --porcelain empty)
  │     ├── branch == main
  │     ├── upstream in sync (git fetch + status clean)
  │     ├── make worktree-lint
  │     ├── make check
  │     └── make test
  ├── detect bump from `git log <last-tag>..HEAD`:
  │     ├── BREAKING CHANGE: footer OR `!` in subject → major
  │     ├── feat: → minor
  │     ├── fix: | refactor: | perf: → patch
  │     └── else → exit 0 ("no qualifying commits")
  ├── apply BUMP= override if provided (else use detected)
  ├── read current version from pyproject.toml + src/lies/__init__.py
  ├── compute target version
  ├── if target != current: rewrite both surfaces
  ├── split CHANGELOG.md:
  │     ├── keep `## [Unreleased]` header (no entries under it)
  │     └── insert `## [X.Y.Z] - <today>` with entries moved down
  ├── git add . && git commit -m "chore(release): vX.Y.Z"
  ├── git tag -a vX.Y.Z -m "Release vX.Y.Z"
  └── git push origin main vX.Y.Z
```

**Bump override:** `--bump major|minor|patch` CLI flag or `make release BUMP=major`. Overrides detection.

**Edge cases:**
- No tags exist → treat `0.0.0` as last tag; first release becomes `0.1.0` for any qualifying commit.
- Current version already equals target → skip file rewrite, still do CHANGELOG + commit + tag.
- Dirty CHANGELOG (entries under `[Unreleased]` already moved) → skip CHANGELOG split, still do commit + tag.

**No `Co-Authored-By:` trailer** in the release commit (per global CLAUDE.md).

## Section 5 — Sequencing the 0.4.0 cut

**Branch:** `feat/repo-standards-conformance` in its own sibling worktree `$ROOT/feat-repo-standards-conformance/`.

**Five commits on the branch (one concern each):**

| # | Type | Subject |
|---|---|---|
| 1 | `chore` | `required-files: add AGENTS.local.md, expand CLAUDE.md, gitignore` |
| 2 | `ci` | `pre-commit: unit on commit, integration on pre-push` |
| 3 | `chore` | `worktree: enforce seven invariants, add make worktree-lint` |
| 4 | `feat` | `release: auto-bump script + make release automation` |
| 5 | `docs` | `changelog: move Unreleased entries under [0.4.0]` |

**Merge to main** via PR (fast-forward). CI must be green (lint + typecheck + format + unit tests). Branch protection: see non-goals.

**Post-merge, operator runs `make release`.** Expected behavior:
- Preflight passes.
- Bump detection: many `feat:` since `v0.2.0` (or first tag) → minor bumps from `0.2.0` → `0.4.0`.
- Target = `0.4.0`, matches code already at `0.4.0` → no file rewrite.
- CHANGELOG already split by commit 5 → no CHANGELOG rewrite.
- Single commit `chore(release): v0.4.0`.
- Tag `v0.4.0`.
- Push `origin main v0.4.0`.

If breaking-change markers exist since `v0.2.0` that were missed, target jumps to `0.5.0` (or `1.0.0` if SemVer dictates). The script handles both.

## Data flow

`scripts/release.py` reads version strings with `tomllib` (Python 3.11+) or `tomli`. Writes via regex on the two known patterns:

```python
# pyproject.toml: version = "0.4.0"
re.sub(r'(?<=version = ")\d+\.\d+\.\d+(?=")', new_version, content)

# src/lies/__init__.py: __version__ = "0.4.0"
re.sub(r'(?<=__version__ = ")\d+\.\d+\.\d+(?=")', new_version, content)
```

`scripts/worktree_lint.py` reads `git worktree list --porcelain` output, parses `<worktree>` / `branch refs/heads/<name>` / `HEAD` triplets, asserts each satisfies the seven invariants from `bare-repo-worktree.md`.

## Error handling

| Failure | Surface |
|---|---|
| Preflight dirty tree | Exit 2 + message: "Working tree dirty; commit or stash before release" |
| Preflight wrong branch | Exit 3 + message: "On branch <X>; release only runs on main" |
| Preflight upstream diverged | Exit 4 + message: "origin/main ahead/behind; pull or push first" |
| Preflight `make worktree-lint` fails | Exit 5 + message: list violating worktrees |
| Preflight `make check` fails | Exit 6 + message: paste ruff/mypy errors |
| Preflight `make test` fails | Exit 7 + message: paste pytest tail |
| No qualifying commits | Exit 0 + message: "no feat/fix/BREAKING since last tag" |
| Bump detection ambiguous (major+minor) | Major wins (most conservative bump) |
| Tag already exists | Exit 8 + message: "vX.Y.Z exists; delete first or use BUMP= to bump further" |
| Push rejected (non-fast-forward) | Exit 9 + message: "origin rejected; check branch protection or fetch first" |

No silent failures. No `try/except: pass`. Each exit code carries a structured message that the operator can act on.

## Testing strategy

### `scripts/release.py` unit tests (`tests/unit/scripts/test_release.py`)

- Detect bump: `feat:` only → minor; `fix:` only → patch; `BREAKING CHANGE:` footer → major; mixed feat+fix → minor; no commits → no-op exit 0.
- Detect bump override: `--bump major` overrides detection.
- Version parse: round-trip current versions from `pyproject.toml` and `src/lies/__init__.py`.
- Version rewrite: target 0.5.0 from 0.4.0 → both files updated correctly.
- CHANGELOG split: extract entries from `[Unreleased]`, emit `[Unreleased]` empty + `[0.4.0] - YYYY-MM-DD` populated.
- Tag-already-exists: refuse and exit 8.

### `scripts/worktree_lint.py` unit tests (`tests/unit/scripts/test_worktree_lint.py`)

- Sibling conformant: pass.
- Dir≠branch: report violation with corrective action.
- Nested `.claude/worktrees/`: report violation.
- Detached HEAD: report violation.
- Missing upstream: report violation.

### Integration test (`tests/integration/test_release.py`)

- Bootstrap a throwaway clone under `tmp_path`.
- Seed conventional commits (`feat:`, `fix:`).
- Run `release.py --bump minor` against the throwaway clone (with `origin` pointing at the throwaway bare).
- Assert: `pyproject.toml` and `src/lies/__init__.py` rewritten; `CHANGELOG.md` split; tag `v0.1.0` exists; tag points at the release commit.

### Manual / e2e

- The actual `v0.4.0` cut is a manual operator step after merge. The unit + integration tests cover the script; the live cut verifies end-to-end on real `origin`.

## Files to add

- `scripts/release.py`
- `scripts/worktree_lint.py`
- `AGENTS.local.md` (empty)
- `tests/unit/scripts/__init__.py`
- `tests/unit/scripts/test_release.py`
- `tests/unit/scripts/test_worktree_lint.py`
- `tests/integration/test_release.py`

## Files to modify

- `CLAUDE.md` — add `@AGENTS.local.md` line.
- `.gitignore` — append two entries.
- `.pre-commit-config.yaml` — remove `[manual]` from tests; split into commit-stage unit and pre-push integration.
- `Makefile` — add `worktree-lint` target; replace stub `release` target with delegation.
- `CHANGELOG.md` — split `[Unreleased]` into empty header + `[0.4.0] - 2026-08-02` (in commit 5).

## Files to rename / move (operator, not script)

- `$ROOT/<dir>/` where `dir ≠ branch` → rename `dir` to match branch.
- `$ROOT/main/.claude/worktrees/<branch>/` → `git worktree move` to `$ROOT/<branch>/` (or remove if sibling duplicate exists).

## Dependencies

- `tomllib` (Python 3.11+) or `tomli` (Python 3.10). Repo targets `>=3.10` per `pyproject.toml`. Add `tomli` to dev dependencies for 3.10 compatibility.
- `pre-commit` already in dev deps.
- No new runtime deps.

## Validation

- 5 conformance commits land on `feat/repo-standards-conformance`.
- PR opened, CI green, fast-forward merged.
- `make worktree-lint` clean.
- `make check` clean.
- `make test` clean.
- `make release` cuts `v0.4.0`, pushes `main` and tag.
- `git tag -l` shows `v0.4.0`.
- `git describe` from main returns `v0.4.0`.
- All 5 previously-gap rule files now conformant.
- 3 not-applicable rule files remain not-applicable (no action).
- 4 already-conformant rule files remain conformant (no regression).

## Documentation sources

- `~/.claude/rules/versioning.md`
- `~/.claude/rules/makefile.md`
- `~/.claude/rules/pre-commit.md`
- `~/.claude/rules/repository-layout.md`
- `~/.claude/rules/required-files.md`
- `~/.claude/rules/agents-md.md`
- `~/.claude/rules/changelog.md`
- `~/.claude/rules/conventional-commits.md`
- `~/.claude/rules/bare-repo-worktree.md`
- `~/.claude/rules/claude-code-plugins.md` (not applicable)
- `~/.claude/rules/github-repo-protection.md` (not applicable)
- `~/.claude/rules/opencode-plugins.md` (not applicable)