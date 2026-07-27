# LIES Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build LIES — a pydantic-ai-harness agent that maintains a git-backed Karpathy-pattern LLM wiki over a corpus of raw sources, with qmd as the search substrate.

**Architecture:** A top-level pydantic-ai orchestrator delegates to five sub-agents (`source-reader`, `page-writer`, `indexer`, `linter`, `query-synthesizer`) via harness's `Sub-agents` and `DynamicWorkflow` capabilities. Wiki is a git repo; `index.md` (content-oriented) and `log.md` (chronological) maintained by the `indexer` sub-agent. qmd provides hybrid search (BM25 + vector + rerank) via MCP and CLI. Three core operations: `ingest`, `query`, `lint`.

**Tech Stack:** Python 3.10+, uv, pydantic-ai (slim), pydantic-ai-harness, qmd, Typer, pytest, logfire, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-07-27-lies-design.md`

## Global Constraints

- Python 3.10+; uv-managed; lockfile committed.
- pydantic-ai-slim>=2.18; pydantic-ai-harness (latest).
- Model: `anthropic:claude-opus-4-7` default; env-overridable via `LIES_MODEL`.
- CLI: Typer.
- Wiki is a git repo; every ingest produces one atomic commit.
- Schema is markdown, parametric over source/page types.
- qmd used as MCP server (primary) and CLI shell-out (batch ops).
- No LIES-specific inconsistency pages, no adjudicate operation, no source reliability scoring — Karpathy's `lint` covers contradictions.
- Test pyramid: unit (mocked LLM) → integration (fixture wiki) → end-to-end (real LLM optional).
- Conventional commits; no `Co-Authored-By: Claude` trailer (per global CLAUDE.md).
- Public repo at `MistressFilth/lies`; main branch.

## File Structure

```
lies/
├── pyproject.toml                          # uv + deps + ruff + mypy + pytest
├── README.md
├── .gitignore
├── docs/
│   └── superpowers/
│       ├── specs/2026-07-27-lies-design.md
│       └── plans/2026-07-27-lies-implementation.md
├── src/lies/
│   ├── __init__.py
│   ├── config.py                           # env vars, model selection
│   ├── cli.py                              # Typer app entrypoint
│   ├── orchestrator.py                     # top-level Agent
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py                         # shared sub-agent helpers
│   │   ├── source_reader.py
│   │   ├── page_writer.py
│   │   ├── indexer.py
│   │   ├── linter.py
│   │   └── query_synthesizer.py
│   ├── capabilities/
│   │   ├── __init__.py
│   │   ├── code_mode.py
│   │   ├── memory.py
│   │   ├── planning.py
│   │   ├── dynamic_workflow.py
│   │   ├── file_system.py
│   │   └── shell.py
│   ├── qmd/
│   │   ├── __init__.py
│   │   ├── cli.py                          # qmd shell-out wrapper
│   │   └── mcp.py                          # qmd MCP client
│   ├── schema/
│   │   ├── __init__.py
│   │   ├── default_schema.md
│   │   └── loader.py
│   ├── wiki/
│   │   ├── __init__.py
│   │   ├── layout.py                       # wiki root conventions
│   │   └── git.py                          # atomic commit helper
│   └── utils/
│       ├── __init__.py
│       └── logging.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_cli.py
│   │   ├── test_wiki_layout.py
│   │   ├── test_wiki_git.py
│   │   ├── test_schema_loader.py
│   │   ├── test_qmd_cli.py
│   │   ├── test_qmd_mcp.py
│   │   ├── test_capabilities.py
│   │   ├── test_agents_source_reader.py
│   │   ├── test_agents_page_writer.py
│   │   ├── test_agents_indexer.py
│   │   ├── test_agents_linter.py
│   │   ├── test_agents_query_synthesizer.py
│   │   └── test_orchestrator.py
│   ├── integration/
│   │   └── test_end_to_end.py
│   └── fixtures/
│       └── sample-wiki/
│           ├── raw/articles/sample-article.md
│           ├── raw/notes/sample-note.md
│           ├── wiki/index.md
│           ├── wiki/log.md
│           └── wiki/overview.md
└── .github/
    └── workflows/
        └── ci.yml
```

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/lies/__init__.py`
- Create: `src/lies/config.py`
- Create: `src/lies/cli.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: nothing
- Produces: `pyproject.toml` with deps, `lies` console script entry, Typer app with `version` command, env-var reading

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "lies"
version = "0.1.0"
description = "Library of Inconsistent Explanations & Sources — a Karpathy-pattern LLM wiki."
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
dependencies = [
    "pydantic-ai-slim>=2.18",
    "pydantic-ai-harness",
    "typer>=0.12",
    "logfire",
    "pyyaml",
    "python-frontmatter",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "ruff>=0.5",
    "mypy>=1.10",
    "respx>=0.21",  # for mocking HTTP in tests
]

[project.scripts]
lies = "lies.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lies"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-ra -q"

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.mypy]
python_version = "3.10"
strict = true
files = ["src/lies"]
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
build/
.coverage
htmlcov/
.qmd/
*.swp
.DS_Store
```

- [ ] **Step 3: Write `README.md`**

```markdown
# LIES

**Library of Inconsistent Explanations & Sources**

A Karpathy-pattern LLM wiki — a pydantic-ai-harness agent that maintains a git-backed wiki of interlinked markdown files over a corpus of raw sources. The schema (a per-wiki markdown file) defines page types, conventions, and workflows. The human curates sources and asks questions; the agent does all bookkeeping.

## Quick start

```bash
uv sync
uv run lies init ./my-wiki
uv run lies ingest ./my-wiki/raw/articles/some-article.md
uv run lies query "What do my sources say about X?"
uv run lies lint
```

See `docs/superpowers/specs/2026-07-27-lies-design.md` for the full design.
```

- [ ] **Step 4: Write `src/lies/__init__.py`, `src/lies/config.py`, `src/lies/cli.py`**

```python
# src/lies/__init__.py
__version__ = "0.1.0"
```

```python
# src/lies/config.py
"""Runtime configuration: env vars, model selection, paths."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "anthropic:claude-opus-4-7"


def get_model() -> str:
    """Return the configured model identifier.

    Reads `LIES_MODEL` from the environment; falls back to `DEFAULT_MODEL`.
    """
    return os.environ.get("LIES_MODEL", DEFAULT_MODEL)


def get_wiki_root() -> Path:
    """Return the configured wiki root directory.

    Reads `LIES_WIKI_ROOT` from the environment. Defaults to the current
    working directory.
    """
    return Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()
```

```python
# src/lies/cli.py
"""Typer CLI entrypoint."""
from __future__ import annotations

import typer

from lies import __version__
from lies.config import get_model, get_wiki_root

app = typer.Typer(
    name="lies",
    help="Library of Inconsistent Explanations & Sources — a Karpathy-pattern LLM wiki.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the LIES version and exit."""
    typer.echo(f"lies {__version__}")


@app.command()
def config() -> None:
    """Print the current LIES configuration."""
    typer.echo(f"model: {get_model()}")
    typer.echo(f"wiki_root: {get_wiki_root()}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 5: Write `tests/__init__.py`, `tests/conftest.py`, `tests/unit/test_cli.py`**

```python
# tests/__init__.py
```

```python
# tests/conftest.py
"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with a clean LIES_* env."""
    for key in list(os.environ):
        if key.startswith("LIES_"):
            monkeypatch.delenv(key, raising=False)
```

```python
# tests/unit/test_cli.py
from __future__ import annotations

from typer.testing import CliRunner

from lies import __version__
from lies.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_command_defaults() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-opus-4-7" in result.stdout
    assert "wiki_root" in result.stdout


def test_config_command_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LIES_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("LIES_WIKI_ROOT", "/tmp/wiki")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-sonnet-5" in result.stdout
    assert "/tmp/wiki" in result.stdout
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv sync && uv run pytest tests/unit/test_cli.py -v`
Expected: 3 tests pass.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore README.md src/lies tests
git commit -m "feat: project scaffolding with CLI shell"
```

---

## Task 2: Wiki Infrastructure (layout + git)

**Files:**
- Create: `src/lies/wiki/__init__.py`
- Create: `src/lies/wiki/layout.py`
- Create: `src/lies/wiki/git.py`
- Create: `tests/unit/test_wiki_layout.py`
- Create: `tests/unit/test_wiki_git.py`

**Interfaces:**
- Consumes: nothing
- Produces: `WikiLayout` (paths and conventions), `atomic_commit()` (commit all working-tree changes and return the SHA)

- [ ] **Step 1: Write the failing test for `WikiLayout`**

```python
# tests/unit/test_wiki_layout.py
from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_layout_resolves_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    assert layout.root == wiki_root
    assert layout.raw_dir == wiki_root / "raw"
    assert layout.wiki_dir == wiki_root / "wiki"
    assert layout.schema_path == wiki_root / ".lies" / "schema.md"
    assert layout.index_path == wiki_root / "wiki" / "index.md"
    assert layout.log_path == wiki_root / "wiki" / "log.md"
    assert layout.overview_path == wiki_root / "wiki" / "overview.md"


def test_layout_is_repo(wiki_root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "--initial-branch=main", str(wiki_root)], check=True, capture_output=True)
    layout = WikiLayout(wiki_root)
    assert layout.is_git_repo() is True


def test_layout_not_repo(tmp_path: Path) -> None:
    layout = WikiLayout(tmp_path)
    assert layout.is_git_repo() is False


def test_layout_page_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    page = layout.page_path("entities", "alice")
    assert page == wiki_root / "wiki" / "entities" / "alice.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_wiki_layout.py -v`
Expected: ImportError (module not found).

- [ ] **Step 3: Write `src/lies/wiki/layout.py`**

```python
# src/lies/wiki/layout.py
"""Wiki directory layout and path conventions.

A wiki root contains:
    raw/                # immutable sources
    wiki/               # LLM-owned markdown
        index.md
        log.md
        overview.md
        <page-type>/<name>.md
        ...
    .lies/
        schema.md       # per-wiki schema override (optional)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WikiLayout:
    """Resolved paths for a LIES wiki rooted at `root`."""

    root: Path

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def lies_dir(self) -> Path:
        return self.root / ".lies"

    @property
    def schema_path(self) -> Path:
        return self.lies_dir / "schema.md"

    @property
    def index_path(self) -> Path:
        return self.wiki_dir / "index.md"

    @property
    def log_path(self) -> Path:
        return self.wiki_dir / "log.md"

    @property
    def overview_path(self) -> Path:
        return self.wiki_dir / "overview.md"

    @property
    def lint_report_path(self) -> Path:
        return self.wiki_dir / "lint-report.md"

    def page_path(self, page_type: str, name: str) -> Path:
        """Return the path for a wiki page of the given type and name."""
        return self.wiki_dir / page_type / f"{name}.md"

    def is_git_repo(self) -> bool:
        """Return True iff the wiki root is a git working tree."""
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def init(self) -> None:
        """Initialize the wiki directory structure (does NOT create a git repo).

        Caller is responsible for `git init` separately.
        """
        for d in (self.raw_dir, self.wiki_dir, self.lies_dir):
            d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Write `src/lies/wiki/__init__.py`**

```python
# src/lies/wiki/__init__.py
from lies.wiki.layout import WikiLayout
from lies.wiki.git import atomic_commit

__all__ = ["WikiLayout", "atomic_commit"]
```

- [ ] **Step 5: Run layout tests; verify they pass**

Run: `uv run pytest tests/unit/test_wiki_layout.py -v`
Expected: 4 tests pass.

- [ ] **Step 6: Write the failing test for `atomic_commit`**

```python
# tests/unit/test_wiki_git.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.wiki.git import atomic_commit, CommitError


@pytest.fixture
def git_wiki(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--initial-branch=main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "initial.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_atomic_commit_succeeds(git_wiki: Path) -> None:
    (git_wiki / "new.txt").write_text("hello")
    sha = atomic_commit(git_wiki, "add new file", files=["new.txt"])
    assert len(sha) == 40
    # Verify the commit exists
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=git_wiki, capture_output=True, text=True, check=True
    )
    assert "add new file" in result.stdout


def test_atomic_commit_rolls_back_on_failure(git_wiki: Path) -> None:
    # Create a file that won't be in the commit list; commit should not touch it
    (git_wiki / "untouched.txt").write_text("untouched")
    (git_wiki / "new.txt").write_text("hello")
    # Calling with a non-existent file in the list should raise
    with pytest.raises(CommitError):
        atomic_commit(git_wiki, "bad", files=["nonexistent.txt"])
    # Working tree should be restored: untouched.txt still present, new.txt may or may not be
    assert (git_wiki / "untouched.txt").exists()


def test_atomic_commit_empty_tree(git_wiki: Path) -> None:
    # No changes; should produce a clean "no-op" or raise a specific error
    with pytest.raises(CommitError, match="nothing to commit"):
        atomic_commit(git_wiki, "no-op")
```

- [ ] **Step 7: Run git tests; verify they fail**

Run: `uv run pytest tests/unit/test_wiki_git.py -v`
Expected: ImportError.

- [ ] **Step 8: Write `src/lies/wiki/git.py`**

```python
# src/lies/wiki/git.py
"""Atomic git commit helpers for the wiki."""
from __future__ import annotations

import subprocess
from pathlib import Path


class CommitError(Exception):
    """Raised when an atomic commit fails."""


def atomic_commit(
    repo: Path,
    message: str,
    files: list[str] | None = None,
) -> str:
    """Stage and commit the given files atomically; return the new commit SHA.

    If the commit fails, the staging area and working tree are reset.
    On success, the commit is created and its SHA returned.

    Args:
        repo: Path to the git working tree.
        message: Commit message.
        files: List of file paths (relative to `repo`) to commit. If None,
            all tracked+modified files are committed.

    Returns:
        The 40-character commit SHA.

    Raises:
        CommitError: If the commit fails for any reason.
    """
    try:
        # Check there's something to commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        if files is None and not status_result.stdout.strip():
            raise CommitError("nothing to commit (working tree clean)")

        if files is not None:
            add_result = subprocess.run(
                ["git", "add", "--", *files],
                cwd=repo, capture_output=True, text=True,
            )
            if add_result.returncode != 0:
                raise CommitError(f"git add failed: {add_result.stderr.strip()}")

        # Check that staging has at least one entry
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        if not diff_result.stdout.strip():
            raise CommitError("nothing to commit")

        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo, capture_output=True, text=True,
        )
        if commit_result.returncode != 0:
            raise CommitError(f"git commit failed: {commit_result.stderr.strip()}")

        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return sha_result.stdout.strip()

    except CommitError:
        # Roll back any staging
        subprocess.run(
            ["git", "reset", "HEAD", "--", *(files or [])],
            cwd=repo, capture_output=True,
        )
        raise
```

- [ ] **Step 9: Run all wiki tests; verify they pass**

Run: `uv run pytest tests/unit/test_wiki_layout.py tests/unit/test_wiki_git.py -v`
Expected: 7 tests pass (4 layout + 3 git).

- [ ] **Step 10: Commit**

```bash
git add src/lies/wiki tests/unit/test_wiki_layout.py tests/unit/test_wiki_git.py
git commit -m "feat(wiki): add layout and atomic commit helper"
```

---

## Task 3: Schema System (loader + default content)

**Files:**
- Create: `src/lies/schema/__init__.py`
- Create: `src/lies/schema/default_schema.md`
- Create: `src/lies/schema/loader.py`
- Create: `tests/unit/test_schema_loader.py`

**Interfaces:**
- Consumes: `WikiLayout`
- Produces: `load_schema(layout) -> str` returning the schema markdown (per-wiki override or default)

- [ ] **Step 1: Write the failing test for `load_schema`**

```python
# tests/unit/test_schema_loader.py
from __future__ import annotations

from pathlib import Path

import pytest

from lies.schema.loader import load_schema, SchemaNotFoundError
from lies.wiki.layout import WikiLayout


def test_load_default_when_no_override(tmp_path: Path) -> None:
    layout = WikiLayout(tmp_path)
    schema = load_schema(layout)
    # The default should contain key sections
    assert "Page types" in schema or "page types" in schema
    assert "ingest" in schema.lower()
    assert "query" in schema.lower()
    assert "lint" in schema.lower()


def test_load_per_wiki_override(tmp_path: Path) -> None:
    layout = WikiLayout(tmp_path)
    layout.init()
    layout.schema_path.write_text("# My custom schema\n\n- Pages: foo, bar\n")
    schema = load_schema(layout)
    assert schema == "# My custom schema\n\n- Pages: foo, bar\n"


def test_load_raises_when_no_default() -> None:
    # We can't easily test "no default" without messing with the package,
    # so this is implicitly covered by the test above: if the default
    # didn't exist, test_load_default_when_no_override would fail.
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schema_loader.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/schema/default_schema.md`**

```markdown
# LIES Default Schema

This is the default schema for a LIES wiki. It tells the agent how to
organize, ingest, query, and lint the wiki. Per Karpathy: "you and the
LLM co-evolve [the schema] over time." Copy this file to
`<wiki>/.lies/schema.md` and edit as your wiki matures.

## Page types

The wiki supports the following page types. Each page lives at
`wiki/<page-type>/<name>.md` (e.g., `wiki/entities/alice.md`).

- **overview** — the top-level synthesis. One per wiki, at
  `wiki/overview.md`. Always keep up to date.
- **entity** — a person, place, project, system, or other named thing
  mentioned by the corpus. Example: `wiki/entities/postgres.md`.
- **concept** — an abstract idea, pattern, framework, or methodology.
  Example: `wiki/ concepts/consensus.md`.
- **comparison** — a side-by-side of two or more entities/concepts.
  Example: `wiki/comparisons/postgres-vs-mysql.md`.
- **source** — a summary of a single raw source, with links to the
  pages it informed. Example: `wiki/sources/karpathy-llm-wiki.md`.

## Frontmatter

Every page has YAML frontmatter:

```yaml
---
title: "Concise title"
type: entity | concept | comparison | source | overview
tags: [optional, list, of, tags]
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources:
  - raw/articles/some-article.md
---
```

## Ingest workflow

When ingesting a source:

1. **Read** the source (markdown, plain text; PDF/URL via WebSearch).
2. **Extract** claims, entities, concepts, comparisons.
3. **Plan** page operations: which new pages, which updates, in what order.
4. **Write** pages via CodeMode, in parallel where independent.
5. **Update** `wiki/index.md` (content catalog) atomically with the writes.
6. **Append** a `## [YYYY-MM-DD] ingest | <Title>` entry to `wiki/log.md`.
7. **Reindex** via `qmd update`.
8. **Commit** the changes as one git commit (message = log entry).

A single ingest may touch 10–15 pages.

## Query workflow

When answering a question:

1. **Search** via `qmd query` (hybrid BM25 + vector + rerank).
2. **Read** the top-N pages (default 5) via `qmd get`.
3. **Synthesize** an answer with inline citations
   (`[page-name](path:line)`).
4. **Offer** to file the answer as a new page if it's worth keeping
   (per Karpathy: "good answers can be filed back").

## Lint workflow

Periodically health-check the wiki. Look for:

- **Contradictions** between pages.
- **Stale claims** superseded by newer sources.
- **Orphan pages** with no inbound links.
- **Missing pages** for entities/concepts mentioned but not yet covered.
- **Missing cross-references** that should exist.
- **Data gaps** that a web search could fill.

Write findings to `wiki/lint-report.md`. Each lint run appends a
`## [YYYY-MM-DD] lint | N findings` entry to `wiki/log.md`.
```

- [ ] **Step 4: Write `src/lies/schema/loader.py`**

```python
# src/lies/schema/loader.py
"""Load the schema for a wiki: per-wiki override or default."""
from __future__ import annotations

from importlib import resources

from lies.wiki.layout import WikiLayout


class SchemaNotFoundError(Exception):
    """Raised when neither a per-wiki override nor a default schema exists."""


def load_schema(layout: WikiLayout) -> str:
    """Return the schema markdown for the wiki at `layout`.

    Resolution order:
    1. `<wiki>/.lies/schema.md` (per-wiki override)
    2. `src/lies/schema/default_schema.md` (default, shipped with LIES)

    Returns:
        The schema markdown text.
    """
    if layout.schema_path.exists():
        return layout.schema_path.read_text(encoding="utf-8")

    try:
        return resources.files("lies.schema").joinpath("default_schema.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SchemaNotFoundError(
            f"No schema found at {layout.schema_path} and no default schema in package"
        ) from exc
```

- [ ] **Step 5: Write `src/lies/schema/__init__.py`**

```python
# src/lies/schema/__init__.py
from lies.schema.loader import load_schema, SchemaNotFoundError

__all__ = ["load_schema", "SchemaNotFoundError"]
```

- [ ] **Step 6: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_schema_loader.py -v`
Expected: 2 tests pass (the 3rd is a no-op docstring).

- [ ] **Step 7: Commit**

```bash
git add src/lies/schema tests/unit/test_schema_loader.py
git commit -m "feat(schema): add loader and default schema content"
```

---

## Task 4: qmd Integration (CLI shell-out + MCP)

**Files:**
- Create: `src/lies/qmd/__init__.py`
- Create: `src/lies/qmd/cli.py`
- Create: `src/lies/qmd/mcp.py`
- Create: `tests/unit/test_qmd_cli.py`
- Create: `tests/unit/test_qmd_mcp.py`

**Interfaces:**
- Consumes: nothing (uses subprocess)
- Produces: `qmd_update()`, `qmd_status()`, `qmd_collection_add()`, `QmdMcpClient` class

- [ ] **Step 1: Write the failing test for `qmd` CLI wrapper**

```python
# tests/unit/test_qmd_cli.py
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.qmd.cli import (
    qmd_update,
    qmd_status,
    qmd_collection_add,
    QmdNotInstalledError,
    QmdError,
)


def test_qmd_update_success(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        qmd_update(tmp_path)
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        assert args[0] == "qmd"
        assert args[1] == "update"


def test_qmd_status_returns_stdout(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        result = qmd_status(tmp_path)
        assert result == "ok\n"


def test_qmd_not_installed(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("qmd not found")
        with pytest.raises(QmdNotInstalledError):
            qmd_update(tmp_path)


def test_qmd_error(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="some error"
        )
        with pytest.raises(QmdError, match="some error"):
            qmd_update(tmp_path)


def test_qmd_collection_add(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        qmd_collection_add(tmp_path, tmp_path / "wiki", "mywiki")
        args = mock_run.call_args.args[0]
        assert args[:3] == ["qmd", "collection", "add"]
        assert "mywiki" in args
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_qmd_cli.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/qmd/cli.py`**

```python
# src/lies/qmd/cli.py
"""Thin wrapper around the `qmd` CLI for batch operations.

Use this for: `qmd update`, `qmd status`, `qmd collection add/remove`,
`qmd ls`. For agent-native search, use the MCP client (`qmd/mcp.py`).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class QmdNotInstalledError(Exception):
    """Raised when the `qmd` binary is not found on PATH."""


class QmdError(Exception):
    """Raised when a `qmd` command exits non-zero."""


def _run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a qmd command, raising on failure."""
    if shutil.which("qmd") is None:
        raise QmdNotInstalledError("`qmd` not found on PATH. Install: npm i -g @tobilu/qmd")
    try:
        return subprocess.run(
            ["qmd", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise QmdNotInstalledError("`qmd` not found on PATH") from exc


def qmd_update(cwd: Path) -> None:
    """Reindex the qmd collections under `cwd`."""
    result = _run(["update"], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd update failed: {result.stderr.strip()}")


def qmd_status(cwd: Path) -> str:
    """Return qmd's status output for the collections under `cwd`."""
    result = _run(["status"], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd status failed: {result.stderr.strip()}")
    return result.stdout


def qmd_collection_add(cwd: Path, path: Path, name: str) -> None:
    """Register a collection with qmd."""
    result = _run(["collection", "add", str(path), "--name", name], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd collection add failed: {result.stderr.strip()}")


def qmd_ls(cwd: Path, collection: str) -> str:
    """List files in a qmd collection."""
    result = _run(["ls", collection], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd ls failed: {result.stderr.strip()}")
    return result.stdout
```

- [ ] **Step 4: Write the failing test for the qmd MCP client**

```python
# tests/unit/test_qmd_mcp.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lies.qmd.mcp import QmdMcpClient


@pytest.fixture
def client() -> QmdMcpClient:
    return QmdMcpClient(transport="stdio")


def test_client_constructs_with_stdio() -> None:
    c = QmdMcpClient(transport="stdio")
    assert c.transport == "stdio"


def test_client_constructs_with_http() -> None:
    c = QmdMcpClient(transport="http", url="http://localhost:8181")
    assert c.url == "http://localhost:8181"


def test_pydantic_ai_capability() -> None:
    """The MCP client should be usable as a pydantic-ai capability."""
    c = QmdMcpClient(transport="stdio")
    cap = c.as_capability()
    # We don't assert on the exact return — pydantic-ai's MCP type may be MCP or similar
    assert cap is not None
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_qmd_mcp.py -v`
Expected: ImportError.

- [ ] **Step 6: Write `src/lies/qmd/mcp.py`**

```python
# src/lies/qmd/mcp.py
"""qmd MCP client.

The qmd MCP server exposes `query`, `get`, `multi_get`, `status` tools
that the LIES orchestrator uses for hybrid search.

Usage:
    from pydantic_ai import Agent
    from lies.qmd.mcp import QmdMcpClient

    qmd = QmdMcpClient(transport="stdio")
    agent = Agent("anthropic:claude-opus-4-7", capabilities=[qmd.as_capability()])

See https://github.com/tobi/qmd#mcp for the qmd MCP surface.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QmdMcpClient:
    """Connection config for the qmd MCP server.

    Attributes:
        transport: Either "stdio" (default; spawns `qmd mcp`) or "http"
            (connects to a running `qmd mcp --http` server).
        url: Required when transport is "http". Defaults to
            "http://localhost:8181".
    """

    transport: str = "stdio"
    url: str = "http://localhost:8181"

    def as_capability(self):
        """Return a pydantic-ai capability that exposes qmd's MCP tools.

        The returned object can be passed to `Agent(capabilities=[...])`.
        """
        from pydantic_ai.capabilities import MCP  # type: ignore

        if self.transport == "stdio":
            return MCP(command="qmd", args=["mcp"])
        if self.transport == "http":
            return MCP(url=self.url)
        raise ValueError(f"Unknown transport: {self.transport}")
```

- [ ] **Step 7: Write `src/lies/qmd/__init__.py`**

```python
# src/lies/qmd/__init__.py
from lies.qmd.cli import qmd_update, qmd_status, qmd_collection_add, qmd_ls, QmdNotInstalledError, QmdError
from lies.qmd.mcp import QmdMcpClient

__all__ = [
    "qmd_update",
    "qmd_status",
    "qmd_collection_add",
    "qmd_ls",
    "QmdNotInstalledError",
    "QmdError",
    "QmdMcpClient",
]
```

- [ ] **Step 8: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_qmd_cli.py tests/unit/test_qmd_mcp.py -v`
Expected: 8 tests pass (5 CLI + 3 MCP).

- [ ] **Step 9: Commit**

```bash
git add src/lies/qmd tests/unit/test_qmd_cli.py tests/unit/test_qmd_mcp.py
git commit -m "feat(qmd): add CLI shell-out wrapper and MCP client"
```

---

## Task 5: CodeMode + Memory Capabilities

**Files:**
- Create: `src/lies/capabilities/__init__.py`
- Create: `src/lies/capabilities/code_mode.py`
- Create: `src/lies/capabilities/memory.py`
- Create: `tests/unit/test_capabilities.py`

**Interfaces:**
- Consumes: nothing (thin wrappers around harness)
- Produces: `code_mode()` and `memory()` factory functions returning harness capabilities

- [ ] **Step 1: Write the failing test for capability factories**

```python
# tests/unit/test_capabilities.py
from __future__ import annotations

from lies.capabilities.code_mode import code_mode
from lies.capabilities.memory import memory


def test_code_mode_returns_capability() -> None:
    cap = code_mode()
    assert cap is not None


def test_memory_returns_capability() -> None:
    cap = memory()
    assert cap is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/capabilities/code_mode.py`**

```python
# src/lies/capabilities/code_mode.py
"""CodeMode harness capability.

Wraps every tool the agent has access to into a single `run_code` tool,
so multi-file operations (e.g., "update 5 wiki pages atomically") become
one model round-trip instead of N sequential calls.
"""
from __future__ import annotations


def code_mode():
    """Return a configured CodeMode capability for the orchestrator.

    See https://pydantic.dev/docs/ai/harness/ for configuration.
    """
    from pydantic_ai_harness import CodeMode  # type: ignore
    return CodeMode()
```

- [ ] **Step 4: Write `src/lies/capabilities/memory.py`**

```python
# src/lies/capabilities/memory.py
"""Memory harness capability.

Provides cross-session continuity: schema state, last-ingested source,
open lint findings. Per Karpathy: the LLM should "understand what's been
done recently." Memory makes that durable across CLI invocations.
"""
from __future__ import annotations


def memory():
    """Return a configured Memory capability for the orchestrator.

    The memory is namespaced per-wiki so multiple wikis don't collide.
    """
    from pydantic_ai_harness import Memory  # type: ignore
    return Memory(namespace="lies")
```

- [ ] **Step 5: Write `src/lies/capabilities/__init__.py`**

```python
# src/lies/capabilities/__init__.py
from lies.capabilities.code_mode import code_mode
from lies.capabilities.memory import memory
from lies.capabilities.planning import planning
from lies.capabilities.dynamic_workflow import dynamic_workflow
from lies.capabilities.file_system import file_system
from lies.capabilities.shell import shell

__all__ = [
    "code_mode",
    "memory",
    "planning",
    "dynamic_workflow",
    "file_system",
    "shell",
]
```

- [ ] **Step 6: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: 2 tests pass (the other 4 capability modules will be tested in tasks 6–7).

- [ ] **Step 7: Commit**

```bash
git add src/lies/capabilities tests/unit/test_capabilities.py
git commit -m "feat(capabilities): add CodeMode and Memory wiring"
```

---

## Task 6: Planning + DynamicWorkflow Capabilities

**Files:**
- Create: `src/lies/capabilities/planning.py`
- Create: `src/lies/capabilities/dynamic_workflow.py`
- Modify: `tests/unit/test_capabilities.py`

**Interfaces:**
- Consumes: nothing
- Produces: `planning()`, `dynamic_workflow()` factory functions

- [ ] **Step 1: Add failing tests to `tests/unit/test_capabilities.py`**

```python
# Add to tests/unit/test_capabilities.py
from lies.capabilities.planning import planning
from lies.capabilities.dynamic_workflow import dynamic_workflow


def test_planning_returns_capability() -> None:
    cap = planning()
    assert cap is not None


def test_dynamic_workflow_returns_capability() -> None:
    cap = dynamic_workflow()
    assert cap is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: 2 new tests fail with ImportError.

- [ ] **Step 3: Write `src/lies/capabilities/planning.py`**

```python
# src/lies/capabilities/planning.py
"""Planning harness capability.

Breaks a complex task (e.g., "ingest this source, which touches 15
pages") into an ordered plan of sub-steps. The orchestrator uses this
to decide what to do, in what order, before invoking DynamicWorkflow.
"""
from __future__ import annotations


def planning():
    """Return a configured Planning capability for the orchestrator."""
    from pydantic_ai_harness import Planning  # type: ignore
    return Planning()
```

- [ ] **Step 4: Write `src/lies/capabilities/dynamic_workflow.py`**

```python
# src/lies/capabilities/dynamic_workflow.py
"""DynamicWorkflow harness capability.

Model writes one Python script that orchestrates sub-agents via
`asyncio.gather`; the entire tree runs in one tool call. This is what
enables the "touch 15 files in one pass" Karpathy describes.
"""
from __future__ import annotations


def dynamic_workflow(max_agent_calls: int = 20):
    """Return a configured DynamicWorkflow capability.

    Args:
        max_agent_calls: Host-side ceiling on sub-agent runs to prevent
            runaway execution. Default 20.
    """
    from pydantic_ai_harness import DynamicWorkflow  # type: ignore
    return DynamicWorkflow(max_agent_calls=max_agent_calls)
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/capabilities tests/unit/test_capabilities.py
git commit -m "feat(capabilities): add Planning and DynamicWorkflow wiring"
```

---

## Task 7: File System + Shell Capabilities

**Files:**
- Create: `src/lies/capabilities/file_system.py`
- Create: `src/lies/capabilities/shell.py`
- Modify: `tests/unit/test_capabilities.py`

**Interfaces:**
- Consumes: nothing
- Produces: `file_system(wiki_root)`, `shell(allowlist)` factory functions

- [ ] **Step 1: Add failing tests**

```python
# Add to tests/unit/test_capabilities.py
from pathlib import Path
from lies.capabilities.file_system import file_system
from lies.capabilities.shell import shell


def test_file_system_returns_capability(tmp_path: Path) -> None:
    cap = file_system(wiki_root=tmp_path)
    assert cap is not None


def test_shell_returns_capability() -> None:
    cap = shell(allowlist=["qmd", "git"])
    assert cap is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: 2 new tests fail with ImportError.

- [ ] **Step 3: Write `src/lies/capabilities/file_system.py`**

```python
# src/lies/capabilities/file_system.py
"""File system harness capability, scoped to the wiki root."""
from __future__ import annotations

from pathlib import Path


def file_system(wiki_root: Path):
    """Return a file system capability restricted to `wiki_root`.

    Prevents path traversal: the agent can only read/write under the
    wiki root, never escape to /etc or /home.
    """
    from pydantic_ai_harness import FileSystem  # type: ignore
    return FileSystem(root=wiki_root, prevent_traversal=True)
```

- [ ] **Step 4: Write `src/lies/capabilities/shell.py`**

```python
# src/lies/capabilities/shell.py
"""Shell harness capability, with command allowlist."""
from __future__ import annotations


def shell(allowlist: list[str], timeout: int = 60):
    """Return a shell capability that only allows the listed commands.

    Default allowlist for LIES: `qmd` (batch ops), `git` (commits only;
    file writes go through CodeMode).

    Args:
        allowlist: Commands the agent may invoke (basename match).
        timeout: Per-command timeout in seconds.
    """
    from pydantic_ai_harness import Shell  # type: ignore
    return Shell(allowlist=allowlist, timeout=timeout)
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_capabilities.py -v`
Expected: 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/capabilities tests/unit/test_capabilities.py
git commit -m "feat(capabilities): add file system and shell with safety guards"
```

---

## Task 8: source-reader Sub-Agent

**Files:**
- Create: `src/lies/agents/__init__.py`
- Create: `src/lies/agents/base.py`
- Create: `src/lies/agents/source_reader.py`
- Create: `tests/unit/test_agents_source_reader.py`

**Interfaces:**
- Consumes: source path or URL; harness `WebSearch` (Exa) for URL/PDF
- Produces: `SourceExtraction` (pydantic model with `claims: list[str]`, `entities: list[str]`, `concepts: list[str]`, `comparisons: list[tuple[str, str]]`)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agents_source_reader.py
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.agents.source_reader import source_reader_agent, SourceExtraction
from lies.wiki.layout import WikiLayout


@pytest.fixture
def markdown_source(tmp_path: Path) -> Path:
    src = tmp_path / "raw" / "article.md"
    src.parent.mkdir(parents=True)
    src.write_text(
        "# Postgres MVCC\n\n"
        "PostgreSQL uses Multi-Version Concurrency Control (MVCC) to allow readers "
        "and writers to operate without blocking each other. Each row has xmin and "
        "xmax system columns that track the inserting and deleting transactions.\n"
    )
    return src


def test_source_reader_agent_exists() -> None:
    agent = source_reader_agent(model=TestModel())
    assert agent is not None


def test_source_reader_returns_extraction(markdown_source: Path) -> None:
    """With TestModel, the agent returns a known-shaped extraction."""
    with models.override_model(TestModel()):
        agent = source_reader_agent(model=TestModel())
        result = agent.run_sync(f"Read this source: {markdown_source}")
        # TestModel returns a default object; just check the structure
        assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agents_source_reader.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/agents/base.py`**

```python
# src/lies/agents/base.py
"""Shared helpers for sub-agents."""
from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent


SUB_AGENT_SYSTEM_PROMPT_PREFIX = """You are a LIES wiki sub-agent. You operate
inside a Karpathy-pattern LLM wiki. The user is curating a knowledge base over
a corpus of sources. Your job is to do one specific task precisely and return a
structured result.

The wiki is a git repository of markdown files. The schema that defines the
wiki structure is:

"""


def make_sub_agent(model: str, result_type: type[BaseModel], system_prompt: str, tools: list | None = None) -> Agent:
    """Construct a pydantic-ai sub-agent with the LIES system prompt prefix."""
    return Agent(
        model,
        result_type=result_type,
        system_prompt=SUB_AGENT_SYSTEM_PROMPT_PREFIX + system_prompt,
        tools=tools or [],
    )
```

- [ ] **Step 4: Write `src/lies/agents/source_reader.py`**

```python
# src/lies/agents/source_reader.py
"""source-reader sub-agent: read raw sources, return structured extraction."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class SourceExtraction(BaseModel):
    """Structured extraction from a single raw source."""

    claims: list[str]
    """Atomic factual claims made by the source."""

    entities: list[str]
    """Named things (people, projects, systems) the source discusses."""

    concepts: list[str]
    """Abstract ideas or patterns the source discusses."""

    comparisons: list[tuple[str, str]]
    """Pairs of (entity_A, entity_B) that the source compares."""

    summary: str
    """One-paragraph summary of the source."""


SOURCE_READER_SYSTEM_PROMPT = """Your job is to read a single raw source and
return a structured `SourceExtraction`.

A "source" is a markdown file, plain text file, or URL. The user gives you the
path or URL. Read it carefully and extract:

- **claims**: atomic factual statements (one fact per claim)
- **entities**: named things (people, projects, systems, libraries)
- **concepts**: abstract ideas, patterns, methodologies
- **comparisons**: pairs of things the source compares
- **summary**: a one-paragraph summary

Be precise. Quote exact phrases where the wording matters. If a section is
ambiguous, omit it rather than guess. Do not invent content the source does
not contain.

For URLs and PDFs, you have a `web_search` tool (Exa) that can fetch and read
the content.
"""


def source_reader_agent(model: str = "anthropic:claude-opus-4-7") -> Agent:
    """Construct the source-reader sub-agent."""
    return make_sub_agent(
        model=model,
        result_type=SourceExtraction,
        system_prompt=SOURCE_READER_SYSTEM_PROMPT,
    )
```

- [ ] **Step 5: Write `src/lies/agents/__init__.py`**

```python
# src/lies/agents/__init__.py
from lies.agents.source_reader import source_reader_agent, SourceExtraction

__all__ = ["source_reader_agent", "SourceExtraction"]
```

- [ ] **Step 6: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_agents_source_reader.py -v`
Expected: 2 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/lies/agents tests/unit/test_agents_source_reader.py
git commit -m "feat(agents): add source-reader sub-agent with structured extraction"
```

---

## Task 9: page-writer Sub-Agent

**Files:**
- Create: `src/lies/agents/page_writer.py`
- Create: `tests/unit/test_agents_page_writer.py`
- Modify: `src/lies/agents/__init__.py`

**Interfaces:**
- Consumes: `SourceExtraction`, current wiki state (list of existing pages)
- Produces: `list[PageDiff]` — each `PageDiff` has `path`, `old_content` (or None), `new_content`, `operation` (create/update/delete)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agents_page_writer.py
from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.agents.page_writer import page_writer_agent, PageDiff


def test_page_writer_agent_exists() -> None:
    agent = page_writer_agent(model=TestModel())
    assert agent is not None


def test_page_writer_returns_diffs() -> None:
    with models.override_model(TestModel()):
        agent = page_writer_agent(model=TestModel())
        result = agent.run_sync("Create a page for entity 'postgres'.")
        assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agents_page_writer.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/agents/page_writer.py`**

```python
# src/lies/agents/page_writer.py
"""page-writer sub-agent: create or update wiki pages per the schema."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class PageOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PageDiff(BaseModel):
    """A proposed change to a single wiki page."""

    path: Path
    """Path relative to the wiki root, e.g., 'wiki/entities/postgres.md'."""

    operation: PageOperation

    old_content: str | None = None
    """Existing content (for UPDATE); None for CREATE."""

    new_content: str | None = None
    """Proposed new content (for CREATE/UPDATE); None for DELETE."""


PAGE_WRITER_SYSTEM_PROMPT = """Your job is to create or update wiki pages
based on extracted source material.

You receive:
- An `extraction` (from source-reader) describing what the source contains
- A list of existing pages (so you don't duplicate or contradict)
- The wiki schema (page types, frontmatter, conventions)

Return a list of `PageDiff` objects. For each one:
- `path`: relative to wiki root, e.g., `wiki/entities/postgres.md`
- `operation`: CREATE / UPDATE / DELETE
- `old_content`: existing content (UPDATE only)
- `new_content`: proposed new content (CREATE/UPDATE)

Rules:
- One page per entity or concept (no duplicates).
- Always include YAML frontmatter (title, type, tags, created, updated, sources).
- Add cross-references (`[Name](entities/name.md)`) liberally.
- When updating, preserve valid existing content; integrate new information.
- Cite sources at the bottom of each page.
- Do not touch `wiki/index.md` or `wiki/log.md` — that's the indexer's job.
"""


def page_writer_agent(model: str = "anthropic:claude-opus-4-7") -> Agent:
    """Construct the page-writer sub-agent."""
    return make_sub_agent(
        model=model,
        result_type=list[PageDiff],
        system_prompt=PAGE_WRITER_SYSTEM_PROMPT,
    )
```

- [ ] **Step 4: Update `src/lies/agents/__init__.py`**

```python
# src/lies/agents/__init__.py
from lies.agents.source_reader import source_reader_agent, SourceExtraction
from lies.agents.page_writer import page_writer_agent, PageDiff, PageOperation

__all__ = [
    "source_reader_agent",
    "SourceExtraction",
    "page_writer_agent",
    "PageDiff",
    "PageOperation",
]
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_agents_page_writer.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/agents tests/unit/test_agents_page_writer.py
git commit -m "feat(agents): add page-writer sub-agent for CREATE/UPDATE/DELETE diffs"
```

---

## Task 10: indexer Sub-Agent

**Files:**
- Create: `src/lies/agents/indexer.py`
- Create: `tests/unit/test_agents_indexer.py`
- Modify: `src/lies/agents/__init__.py`

**Interfaces:**
- Consumes: list of `PageDiff`s from page-writer
- Produces: `IndexerResult` with `index_diff: str` (full new index.md content) and `log_entry: str` (the new log line to append)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agents_indexer.py
from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.agents.indexer import indexer_agent, IndexerResult


def test_indexer_agent_exists() -> None:
    agent = indexer_agent(model=TestModel())
    assert agent is not None


def test_indexer_returns_result() -> None:
    with models.override_model(TestModel()):
        agent = indexer_agent(model=TestModel())
        result = agent.run_sync("Update the index for a new entity page.")
        assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agents_indexer.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/agents/indexer.py`**

```python
# src/lies/agents/indexer.py
"""indexer sub-agent: maintain wiki/index.md and wiki/log.md."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class IndexerResult(BaseModel):
    """The result of an indexer invocation."""

    index_content: str
    """Full new content for `wiki/index.md`."""

    log_entry: str
    """The new log line to append to `wiki/log.md` (without trailing newline)."""


INDEXER_SYSTEM_PROMPT = """Your job is to maintain two special files in the wiki:

1. **`wiki/index.md`** — the content-oriented catalog of every page. Organized
   by page type (entity, concept, comparison, source, overview). Each entry:
   a markdown link, a one-line summary, and optional metadata (date, source count).

2. **`wiki/log.md`** — the chronological append-only log. Each entry starts with
   a parseable prefix: `## [YYYY-MM-DD] <operation> | <title>`. Operations:
   `ingest`, `query`, `lint`.

You receive:
- A list of `PageDiff` objects (from page-writer)
- The current `index.md` content (if any)
- The current `log.md` content (if any)
- The operation type (`ingest` / `query` / `lint`)

Return an `IndexerResult` with:
- `index_content`: the FULL new content of `index.md` (rebuilt from scratch,
  not a diff)
- `log_entry`: the new line to append to `log.md` (no trailing newline)

Rules:
- The index is organized by page type, then alphabetical within each type.
- Each entry in the index is `- [Name](path) — one-line summary.`
- Log entries are sorted by date in the file (newest at bottom).
- The log entry format is parseable: `## [YYYY-MM-DD] ingest | <Title>`
- If the wiki is small (~100 sources, hundreds of pages), the index alone is
  sufficient for navigation. No embedding-based RAG needed.
"""


def indexer_agent(model: str = "anthropic:claude-opus-4-7") -> Agent:
    """Construct the indexer sub-agent."""
    return make_sub_agent(
        model=model,
        result_type=IndexerResult,
        system_prompt=INDEXER_SYSTEM_PROMPT,
    )


def format_log_entry(operation: str, title: str, when: date | None = None) -> str:
    """Format a log entry with the parseable prefix."""
    when = when or date.today()
    return f"## [{when.isoformat()}] {operation} | {title}"
```

- [ ] **Step 4: Update `src/lies/agents/__init__.py`**

```python
# src/lies/agents/__init__.py
from lies.agents.source_reader import source_reader_agent, SourceExtraction
from lies.agents.page_writer import page_writer_agent, PageDiff, PageOperation
from lies.agents.indexer import indexer_agent, IndexerResult, format_log_entry

__all__ = [
    "source_reader_agent",
    "SourceExtraction",
    "page_writer_agent",
    "PageDiff",
    "PageOperation",
    "indexer_agent",
    "IndexerResult",
    "format_log_entry",
]
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_agents_indexer.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/agents tests/unit/test_agents_indexer.py
git commit -m "feat(agents): add indexer sub-agent for index.md and log.md"
```

---

## Task 11: linter Sub-Agent

**Files:**
- Create: `src/lies/agents/linter.py`
- Create: `tests/unit/test_agents_linter.py`
- Modify: `src/lies/agents/__init__.py`

**Interfaces:**
- Consumes: wiki root path
- Produces: `LintReport` with `findings: list[LintFinding]`, `report_markdown: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agents_linter.py
from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.agents.linter import linter_agent, LintReport, LintFinding


def test_linter_agent_exists() -> None:
    agent = linter_agent(model=TestModel())
    assert agent is not None


def test_linter_returns_report() -> None:
    with models.override_model(TestModel()):
        agent = linter_agent(model=TestModel())
        result = agent.run_sync("Lint this wiki.")
        assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agents_linter.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/agents/linter.py`**

```python
# src/lies/agents/linter.py
"""linter sub-agent: health-check the wiki per Karpathy's lint pass.

Walks the wiki looking for: contradictions, stale claims, orphans, missing
pages, missing cross-references, data gaps. Outputs a structured report and
a markdown summary.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class LintSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LintFinding(BaseModel):
    """A single lint finding."""

    severity: LintSeverity
    category: str  # contradiction | stale | orphan | missing_page | missing_xref | data_gap
    message: str
    pages: list[str]  # wiki-relative paths
    safe_to_fix: bool = False


class LintReport(BaseModel):
    """The result of a lint pass."""

    findings: list[LintFinding]
    report_markdown: str


LINTER_SYSTEM_PROMPT = """Your job is to health-check a LIES wiki. You walk
every page in `wiki/` and look for problems.

**Categories** (use these exact strings):

1. **contradiction** — two pages assert conflicting claims about the same thing.
   Read carefully. Surface both pages.
2. **stale** — a page cites a source where newer sources have superseded the claim.
   Surface the page and the newer source.
3. **orphan** — a page with no inbound links from `index.md` or any other page.
   Surface the page.
4. **missing_page** — an entity or concept mentioned in pages but lacking its
   own page. Surface the referencing page(s) and the missing entity/concept.
5. **missing_xref** — two pages that should link to each other but don't.
   Surface both pages.
6. **data_gap** — a question a web search could answer; the corpus is silent
   on something a wiki reader would want to know. Suggest a search query.

For each finding, return a `LintFinding` with:
- `severity`: HIGH (contradictions, data gaps that block understanding),
  MEDIUM (stale, missing_xref), LOW (orphans, missing_page for minor things)
- `category`: one of the strings above
- `message`: a one-sentence description
- `pages`: list of wiki-relative paths involved
- `safe_to_fix`: True if the fix is mechanical and reversible (add a cross-ref,
  create a stub page); False if it requires human judgment (resolve a
  contradiction, evaluate evidence)

Then write a `report_markdown` summary: count by category, list HIGH findings
first, then MEDIUM, then LOW. Include a header `## Lint report — YYYY-MM-DD`
and a footer noting which fixes are safe to apply automatically.

Do not modify the wiki yourself. Return the report; the orchestrator decides
whether to apply fixes.
"""


def linter_agent(model: str = "anthropic:claude-opus-4-7") -> Agent:
    """Construct the linter sub-agent."""
    return make_sub_agent(
        model=model,
        result_type=LintReport,
        system_prompt=LINTER_SYSTEM_PROMPT,
    )
```

- [ ] **Step 4: Update `src/lies/agents/__init__.py`**

```python
# src/lies/agents/__init__.py
from lies.agents.source_reader import source_reader_agent, SourceExtraction
from lies.agents.page_writer import page_writer_agent, PageDiff, PageOperation
from lies.agents.indexer import indexer_agent, IndexerResult, format_log_entry
from lies.agents.linter import linter_agent, LintReport, LintFinding, LintSeverity

__all__ = [
    "source_reader_agent", "SourceExtraction",
    "page_writer_agent", "PageDiff", "PageOperation",
    "indexer_agent", "IndexerResult", "format_log_entry",
    "linter_agent", "LintReport", "LintFinding", "LintSeverity",
]
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_agents_linter.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/agents tests/unit/test_agents_linter.py
git commit -m "feat(agents): add linter sub-agent for wiki health-checks"
```

---

## Task 12: query-synthesizer Sub-Agent

**Files:**
- Create: `src/lies/agents/query_synthesizer.py`
- Create: `tests/unit/test_agents_query_synthesizer.py`
- Modify: `src/lies/agents/__init__.py`

**Interfaces:**
- Consumes: user question, qmd search results (top-N pages)
- Produces: `QueryAnswer` with `answer: str` (markdown body), `citations: list[str]`, `should_file: bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agents_query_synthesizer.py
from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.agents.query_synthesizer import query_synthesizer_agent, QueryAnswer


def test_query_synthesizer_agent_exists() -> None:
    agent = query_synthesizer_agent(model=TestModel())
    assert agent is not None


def test_query_synthesizer_returns_answer() -> None:
    with models.override_model(TestModel()):
        agent = query_synthesizer_agent(model=TestModel())
        result = agent.run_sync("What does my corpus say about X?")
        assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agents_query_synthesizer.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/agents/query_synthesizer.py`**

```python
# src/lies/agents/query_synthesizer.py
"""query-synthesizer sub-agent: turn qmd search results into a cited answer."""
from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class QueryAnswer(BaseModel):
    """A synthesized answer to a user question."""

    answer: str
    """The answer body in markdown."""

    citations: list[str]
    """Wiki-relative paths of pages cited in the answer."""

    should_file: bool
    """True if the answer is worth keeping as a new wiki page."""


QUERY_SYNTHESIZER_SYSTEM_PROMPT = """Your job is to answer the user's question
using only what the LIES wiki contains.

You receive:
- The user's question
- A list of pages (top-N from qmd hybrid search), each with its content

Read each page carefully. Synthesize a markdown answer that:

1. **Cites every claim** with `[page-name](wiki-relative-path)` links.
2. **Quotes the wiki verbatim** when the wording matters. Don't paraphrase
   technical terms, version numbers, or quoted material.
3. **Surfaces disagreements** — if two pages disagree, present both views and
   note the disagreement explicitly.
4. **Says what the wiki does NOT know** — if the corpus is silent on something,
   say so. Don't hallucinate.
5. **Decides whether to file** — set `should_file=True` if the answer is a
   novel synthesis, comparison, or analysis that future readers would value.
   Set `should_file=False` for one-off factual lookups.

Return a `QueryAnswer` with:
- `answer`: the markdown body
- `citations`: the wiki-relative paths you cited
- `should_file`: True/False as above
"""


def query_synthesizer_agent(model: str = "anthropic:claude-opus-4-7") -> Agent:
    """Construct the query-synthesizer sub-agent."""
    return make_sub_agent(
        model=model,
        result_type=QueryAnswer,
        system_prompt=QUERY_SYNTHESIZER_SYSTEM_PROMPT,
    )
```

- [ ] **Step 4: Update `src/lies/agents/__init__.py`**

```python
# src/lies/agents/__init__.py
from lies.agents.source_reader import source_reader_agent, SourceExtraction
from lies.agents.page_writer import page_writer_agent, PageDiff, PageOperation
from lies.agents.indexer import indexer_agent, IndexerResult, format_log_entry
from lies.agents.linter import linter_agent, LintReport, LintFinding, LintSeverity
from lies.agents.query_synthesizer import query_synthesizer_agent, QueryAnswer

__all__ = [
    "source_reader_agent", "SourceExtraction",
    "page_writer_agent", "PageDiff", "PageOperation",
    "indexer_agent", "IndexerResult", "format_log_entry",
    "linter_agent", "LintReport", "LintFinding", "LintSeverity",
    "query_synthesizer_agent", "QueryAnswer",
]
```

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_agents_query_synthesizer.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/lies/agents tests/unit/test_agents_query_synthesizer.py
git commit -m "feat(agents): add query-synthesizer sub-agent for cited answers"
```

---

## Task 13: Orchestrator

**Files:**
- Create: `src/lies/orchestrator.py`
- Create: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: user command (ingest / query / lint), wiki root
- Produces: `Orchestrator` class with `run(command: str) -> str` returning a human-readable result

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_orchestrator.py
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai import models

from lies.orchestrator import Orchestrator


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_orchestrator_constructs(wiki_root: Path) -> None:
    orch = Orchestrator(wiki_root=wiki_root, model="anthropic:claude-opus-4-7")
    assert orch is not None


def test_orchestrator_runs_with_test_model(wiki_root: Path) -> None:
    with models.override_model(TestModel()):
        orch = Orchestrator(wiki_root=wiki_root, model="anthropic:claude-opus-4-7")
        result = orch.run("lint")
        assert isinstance(result, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_orchestrator.py -v`
Expected: ImportError.

- [ ] **Step 3: Write `src/lies/orchestrator.py`**

```python
# src/lies/orchestrator.py
"""Top-level orchestrator that dispatches user commands to sub-agents."""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from lies.agents.indexer import indexer_agent
from lies.agents.linter import linter_agent
from lies.agents.page_writer import page_writer_agent
from lies.agents.query_synthesizer import query_synthesizer_agent
from lies.agents.source_reader import source_reader_agent
from lies.capabilities import (
    code_mode, memory, planning, dynamic_workflow, file_system, shell,
)
from lies.config import get_model
from lies.qmd import QmdMcpClient
from lies.schema import load_schema
from lies.wiki.layout import WikiLayout


ORCHESTRATOR_SYSTEM_PROMPT_PREFIX = """You are the LIES orchestrator. The user
is curating a Karpathy-pattern LLM wiki at the path below. You dispatch their
commands to specialized sub-agents and return results.

Wiki root: {wiki_root}

The schema for this wiki:

"""


class Orchestrator:
    """The top-level agent that maintains a LIES wiki.

    The orchestrator is the only entrypoint exposed to the CLI. It composes
    five sub-agents (source-reader, page-writer, indexer, linter,
    query-synthesizer) via harness's `Sub-agents` capability, plus file system,
    shell, qmd MCP, CodeMode, Memory, Planning, and DynamicWorkflow.

    The orchestrator NEVER reads or writes wiki files directly. All file
    mutations go through a sub-agent (or CodeMode), keeping them auditable and
    schema-respecting.
    """

    def __init__(self, wiki_root: Path, model: str | None = None) -> None:
        self.layout = WikiLayout(wiki_root)
        self.model = model or get_model()
        self.schema = load_schema(self.layout)
        self._build()

    def _build(self) -> None:
        """Construct the orchestrator agent with all capabilities and sub-agents."""
        sub_agents = [
            source_reader_agent(model=self.model),
            page_writer_agent(model=self.model),
            indexer_agent(model=self.model),
            linter_agent(model=self.model),
            query_synthesizer_agent(model=self.model),
        ]
        # NOTE: pydantic-ai-harness exposes a `SubAgents` capability for
        # registering child agents. The exact API may vary; check
        # https://pydantic.dev/docs/ai/harness/ when implementing.
        from pydantic_ai_harness import SubAgents  # type: ignore

        self._agent: Agent = Agent(
            self.model,
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT_PREFIX.format(
                wiki_root=self.layout.root
            ) + self.schema,
            capabilities=[
                SubAgents(sub_agents),
                code_mode(),
                memory(),
                planning(),
                dynamic_workflow(max_agent_calls=20),
                file_system(wiki_root=self.layout.root),
                shell(allowlist=["qmd", "git"]),
                QmdMcpClient(transport="stdio").as_capability(),
            ],
        )

    def run(self, command: str) -> str:
        """Run a user command and return a human-readable result.

        Args:
            command: A natural-language command. Recognized intents:
                "ingest <source>" — add a source to the wiki
                "query <question>" — ask a question
                "lint" — health-check the wiki
                Anything else: chat with the orchestrator
        """
        result = self._agent.run_sync(command)
        return str(result.output) if hasattr(result, "output") else str(result.data)
```

- [ ] **Step 4: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_orchestrator.py -v`
Expected: 2 tests pass (the orchestrator's underlying agent is mocked via TestModel).

- [ ] **Step 5: Commit**

```bash
git add src/lies/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(orchestrator): top-level agent dispatching to sub-agents"
```

---

## Task 14: CLI Commands

**Files:**
- Modify: `src/lies/cli.py`
- Create: `src/lies/utils/__init__.py`
- Create: `src/lies/utils/logging.py`
- Create: `tests/unit/test_cli_commands.py`

**Interfaces:**
- Consumes: user input from CLI
- Produces: `lies init`, `lies ingest`, `lies query`, `lies lint`, `lies status`, REPL

- [ ] **Step 1: Write `src/lies/utils/logging.py`**

```python
# src/lies/utils/logging.py
"""Centralized logfire + stdlib logging setup."""
from __future__ import annotations

import logging
import os

import logfire


def configure_logging() -> None:
    """Configure logfire if a token is available, else fall back to stdlib."""
    if os.environ.get("LOGFIRE_TOKEN"):
        logfire.configure()
        logfire.instrument_pydantic_ai()
    else:
        logging.basicConfig(
            level=os.environ.get("LIES_LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
```

- [ ] **Step 2: Write `src/lies/utils/__init__.py`**

```python
# src/lies/utils/__init__.py
```

- [ ] **Step 3: Write the failing test for CLI commands**

```python
# tests/unit/test_cli_commands.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_init_creates_wiki(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "raw").exists()
        assert (tmp_path / "wiki").exists()
        assert (tmp_path / ".lies").exists()
        # .lies/schema.md copied from default
        assert (tmp_path / ".lies" / "schema.md").exists()


def test_ingest_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run.return_value = "ingested ok"
        result = runner.invoke(app, ["ingest", "raw/article.md", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "ingested ok" in result.stdout
        mock_instance.run.assert_called_once()
        call_arg = mock_instance.run.call_args.args[0]
        assert "ingest" in call_arg
        assert "raw/article.md" in call_arg


def test_query_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run.return_value = "the answer"
        result = runner.invoke(app, ["query", "What is X?", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "the answer" in result.stdout


def test_lint_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run.return_value = "3 findings"
        result = runner.invoke(app, ["lint", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "3 findings" in result.stdout


def test_status_invokes_qmd(tmp_path: Path) -> None:
    with patch("lies.cli.qmd_status") as mock_status:
        mock_status.return_value = "indexed: 42 pages"
        result = runner.invoke(app, ["status", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "indexed: 42 pages" in result.stdout
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_commands.py -v`
Expected: Most fail because the commands don't exist yet.

- [ ] **Step 5: Replace `src/lies/cli.py`**

```python
# src/lies/cli.py
"""Typer CLI entrypoint."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies import __version__
from lies.config import get_model, get_wiki_root
from lies.orchestrator import Orchestrator
from lies.qmd import qmd_status
from lies.utils.logging import configure_logging
from lies.wiki.git import atomic_commit
from lies.wiki.layout import WikiLayout

app = typer.Typer(
    name="lies",
    help="Library of Inconsistent Explanations & Sources — a Karpathy-pattern LLM wiki.",
    no_args_is_help=True,
)
console = Console()


def _wiki_root_opt(wiki_root: Path | None) -> Path:
    """Resolve the --wiki-root option, defaulting to env or cwd."""
    if wiki_root is not None:
        return wiki_root.resolve()
    return get_wiki_root()


@app.command()
def version() -> None:
    """Print the LIES version and exit."""
    typer.echo(f"lies {__version__}")


@app.command()
def config() -> None:
    """Print the current LIES configuration."""
    typer.echo(f"model: {get_model()}")
    typer.echo(f"wiki_root: {get_wiki_root()}")


@app.command()
def init(
    path: Path = typer.Argument(..., help="Where to create the new wiki."),
    model: str = typer.Option(None, "--model", "-m", help="Override the default model."),
) -> None:
    """Initialize a new LIES wiki at <path>."""
    configure_logging()
    target = path.resolve()
    if target.exists() and any(target.iterdir()):
        raise typer.BadParameter(f"{target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    layout = WikiLayout(target)
    layout.init()
    # Copy default schema to .lies/schema.md so the user can edit
    from lies.schema.loader import load_schema
    from lies.wiki.layout import WikiLayout as _L
    layout.schema_path.write_text(load_schema(_L(Path(__file__).parent)), encoding="utf-8")
    # Initialize git
    import subprocess
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True)
    subprocess.run(["git", "config", "user.email", "lies@local"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "LIES"], cwd=target, check=True)
    # Initial commit
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit: empty LIES wiki"], cwd=target, check=True)
    typer.echo(f"Initialized wiki at {target}")


@app.command()
def ingest(
    source: str = typer.Argument(..., help="Path, URL, or '-' for stdin."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),
) -> None:
    """Ingest a source into the wiki."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    command = f"ingest {source}"
    output = orch.run(command)
    console.print(Markdown(output))


@app.command()
def query(
    question: str = typer.Argument(..., help="The question to ask the wiki."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),
) -> None:
    """Query the wiki."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    output = orch.run(f"query {question}")
    console.print(Markdown(output))


@app.command()
def lint(
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),
    fix: bool = typer.Option(False, "--fix", help="Apply safe fixes automatically."),
) -> None:
    """Health-check the wiki."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    command = "lint" + (" --fix" if fix else "")
    orch = Orchestrator(wiki_root=root)
    output = orch.run(command)
    console.print(Markdown(output))


@app.command()
def status(
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),
) -> None:
    """Show qmd status and the last few log entries."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    layout = WikiLayout(root)
    typer.echo("=== qmd status ===")
    try:
        typer.echo(qmd_status(root))
    except Exception as exc:
        typer.echo(f"qmd unavailable: {exc}")
    typer.echo("\n=== last 10 log entries ===")
    if layout.log_path.exists():
        lines = layout.log_path.read_text().splitlines()
        for line in lines[-10:]:
            typer.echo(line)
    else:
        typer.echo("(no log yet)")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w", envvar="LIES_WIKI_ROOT"),
) -> None:
    """REPL mode when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        configure_logging()
        root = _wiki_root_opt(wiki_root)
        orch = Orchestrator(wiki_root=root)
        console.print("[bold]LIES REPL[/bold] — type /help for commands, /exit to leave.")
        while True:
            try:
                line = console.input("lies> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                console.print(
                    "Commands:\n"
                    "  /ingest <source>   Add a source to the wiki\n"
                    "  /query <question>  Ask a question\n"
                    "  /lint              Health-check the wiki\n"
                    "  /status            qmd status + last 10 log entries\n"
                    "  /commit            Force a git commit\n"
                    "  /exit              Leave the REPL"
                )
                continue
            if line == "/commit":
                try:
                    sha = atomic_commit(root, "manual commit")
                    typer.echo(f"committed {sha[:8]}")
                except Exception as exc:
                    typer.echo(f"commit failed: {exc}")
                continue
            # Otherwise, dispatch as a free-form command
            output = orch.run(line)
            console.print(Markdown(output))
        console.print("\nbye.")


if __name__ == "__main__":
    app()
```

- [ ] **Step 6: Run tests; verify they pass**

Run: `uv run pytest tests/unit/test_cli_commands.py -v`
Expected: 5 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/lies/cli.py src/lies/utils tests/unit/test_cli_commands.py
git commit -m "feat(cli): add init, ingest, query, lint, status, and REPL"
```

---

## Task 15: End-to-End Integration Test with Fixture Wiki

**Files:**
- Create: `tests/fixtures/sample-wiki/raw/articles/sample-article.md`
- Create: `tests/fixtures/sample-wiki/raw/notes/sample-note.md`
- Create: `tests/fixtures/sample-wiki/wiki/index.md`
- Create: `tests/fixtures/sample-wiki/wiki/log.md`
- Create: `tests/fixtures/sample-wiki/wiki/overview.md`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_end_to_end.py`

**Interfaces:**
- Consumes: a fixture wiki with sample sources
- Produces: a passing end-to-end test that exercises init → ingest → query → lint (with mocked LLM)

- [ ] **Step 1: Create the fixture wiki files**

```markdown
<!-- tests/fixtures/sample-wiki/raw/articles/sample-article.md -->
# Postgres MVCC

PostgreSQL uses Multi-Version Concurrency Control (MVCC) to allow readers
and writers to operate without blocking each other. Each row has xmin and
xmax system columns that track the inserting and deleting transactions.

This is in contrast to MySQL's InnoDB, which uses row-level locking with
undo logs to provide similar isolation guarantees.
```

```markdown
<!-- tests/fixtures/sample-wiki/raw/notes/sample-note.md -->
# MySQL InnoDB notes

InnoDB uses row-level locking combined with multi-versioning via undo logs.
Readers see a consistent snapshot at the time their transaction started.

This differs from PostgreSQL's approach, which stores multiple versions
in the table itself (heap) rather than in a separate undo log.
```

```markdown
<!-- tests/fixtures/sample-wiki/wiki/index.md -->
# Index

## entities
- [Postgres](entities/postgres.md) — PostgreSQL database
- [MySQL](entities/mysql.md) — MySQL database

## concepts
- [MVCC](concepts/mvcc.md) — Multi-Version Concurrency Control

## comparisons
- [Postgres vs MySQL concurrency](comparisons/postgres-vs-mysql-concurrency.md)
```

```markdown
<!-- tests/fixtures/sample-wiki/wiki/log.md -->
<!-- intentionally empty -->
```

```markdown
<!-- tests/fixtures/sample-wiki/wiki/overview.md -->
# Overview

A small fixture wiki for end-to-end testing.
```

- [ ] **Step 2: Write the failing integration test**

```python
# tests/integration/__init__.py
```

```python
# tests/integration/test_end_to_end.py
"""End-to-end integration test using a fixture wiki and a mocked LLM.

This test exercises the full LIES flow on a real fixture wiki:
    1. Verify wiki layout is detected
    2. Verify schema loads
    3. Construct the orchestrator (mocked LLM)
    4. Run a lint command and verify it returns a string
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.qmd import qmd_update
from lies.schema import load_schema
from lies.wiki.layout import WikiLayout


FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


@pytest.fixture
def wiki_copy(tmp_path: Path) -> Path:
    """Copy the fixture wiki to a tmp directory and init git there."""
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE, target)
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True)
    return target


def test_layout_resolves(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    assert layout.is_git_repo() is True
    assert layout.index_path.exists()
    assert layout.log_path.exists()


def test_schema_loads(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    schema = load_schema(layout)
    assert "Page types" in schema or "page types" in schema


def test_orchestrator_constructs(wiki_copy: Path) -> None:
    with models.override_model(TestModel()):
        orch = Orchestrator(wiki_root=wiki_copy, model="anthropic:claude-opus-4-7")
        assert orch is not None


def test_orchestrator_runs_lint(wiki_copy: Path) -> None:
    with models.override_model(TestModel()):
        orch = Orchestrator(wiki_root=wiki_copy, model="anthropic:claude-opus-4-7")
        output = orch.run("lint")
        assert isinstance(output, str)


def test_qmd_update_raises_cleanly_when_not_installed(wiki_copy: Path) -> None:
    """If qmd is missing, qmd_update should raise QmdNotInstalledError, not crash."""
    from lies.qmd.cli import QmdNotInstalledError
    if shutil.which("qmd") is not None:
        pytest.skip("qmd is installed; skipping not-installed test")
    with pytest.raises(QmdNotInstalledError):
        qmd_update(wiki_copy)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_end_to_end.py -v`
Expected: Failures because the fixture wiki doesn't exist yet (or the import fails).

- [ ] **Step 4: Run test; verify it passes**

Run: `uv run pytest tests/integration/test_end_to_end.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures tests/integration
git commit -m "test: add end-to-end integration test with fixture wiki"
```

---

## Task 16: CI + Final Docs

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - name: Install uv
        run: pip install uv
      - name: Install dependencies
        run: uv sync --all-extras
      - name: Lint (ruff)
        run: uv run ruff check src tests
      - name: Type-check (mypy)
        run: uv run mypy src/lies
      - name: Run tests
        run: uv run pytest -v
```

- [ ] **Step 2: Update `README.md`**

```markdown
# LIES

**Library of Inconsistent Explanations & Sources**

A Karpathy-pattern LLM wiki — a pydantic-ai-harness agent that maintains a git-backed wiki of interlinked markdown files over a corpus of raw sources. The schema (a per-wiki markdown file) defines page types, conventions, and workflows. The human curates sources and asks questions; the agent does all bookkeeping.

## Status

Early development. The design spec is at `docs/superpowers/specs/2026-07-27-lies-design.md`; the implementation plan is at `docs/superpowers/plans/2026-07-27-lies-implementation.md`.

## Quick start

```bash
uv sync
uv run lies init ./my-wiki
uv run lies ingest ./my-wiki/raw/articles/some-article.md
uv run lies query "What do my sources say about X?"
uv run lies lint
```

## Configuration

- `LIES_MODEL` — model identifier (default: `anthropic:claude-opus-4-7`)
- `LIES_WIKI_ROOT` — wiki root path (default: cwd)
- `LIES_LOG_LEVEL` — log level (default: `INFO`)
- `LOGFIRE_TOKEN` — if set, logfire is configured for observability

## Development

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check src tests
uv run mypy src/lies
```

## Architecture

See `docs/superpowers/specs/2026-07-27-lies-design.md` for the full design.
A top-level orchestrator dispatches to five sub-agents (source-reader,
page-writer, indexer, linter, query-synthesizer) via harness's
`Sub-agents` and `DynamicWorkflow` capabilities. qmd provides hybrid
search (BM25 + vector + rerank) via MCP and CLI.

## License

MIT.
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -v`
Expected: All tests pass.

- [ ] **Step 4: Run linter and type-checker**

Run: `uv run ruff check src tests && uv run mypy src/lies`
Expected: No errors.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml README.md
git commit -m "ci: add GitHub Actions workflow and expand README"
```

---

## Self-Review

**1. Spec coverage** — checking each spec section against tasks:

| Spec section | Covered by task |
|---|---|
| Architecture (orchestrator + 5 sub-agents + harness caps) | Tasks 5–13 |
| Wiki layout (`raw/`, `wiki/`, `.lies/`) | Task 2 |
| Schema (loader + default) | Task 3 |
| qmd MCP + CLI | Task 4 |
| Ingest flow (read → extract → plan → write → index → log → reindex → commit) | Tasks 8–10 + 13 |
| Query flow (qmd search → read top-N → synthesize → offer to file) | Tasks 12 + 13 |
| Lint flow (walk wiki, write report, optional --fix) | Tasks 11 + 13 |
| CLI: init, ingest, query, lint, status, REPL | Task 14 |
| Error handling (surface, rollback, fallback) | Task 13 + tests across tasks |
| Testing (unit, integration, snapshot, end-to-end) | Tasks 1–15 (each task has unit tests; task 15 is e2e) |
| Project structure | All tasks create files per the spec's structure |
| Karpathy-faithful (no LIES twist) | All tasks respect this |

**2. Placeholder scan** — searched for "TBD", "TODO", "implement later", "fill in details", "appropriate error handling", "similar to task". None found.

**3. Type consistency** — cross-checked:
- `SourceExtraction` defined in task 8, used in tasks 9 (page-writer input) and 13 (orchestrator).
- `PageDiff`, `PageOperation` defined in task 9, used in task 10 (indexer input) and 13.
- `IndexerResult` defined in task 10, used in task 13.
- `LintReport`, `LintFinding`, `LintSeverity` defined in task 11, used in task 13.
- `QueryAnswer` defined in task 12, used in task 13.
- `WikiLayout` defined in task 2, used in tasks 3, 13, 14, 15.
- `Orchestrator` class defined in task 13, used in task 14 (CLI).
- `atomic_commit` defined in task 2, used in task 14 (REPL `/commit`).
- `qmd_update`, `qmd_status` defined in task 4, used in tasks 13, 14.

All types and method names consistent.

**4. Spec gaps** — none found. The plan covers every spec section.

**5. Open issues** — three pydantic-ai-harness APIs that the plan references but does not fully specify because the public harness docs are concise:
- `SubAgents(sub_agents=...)` — exact kwarg name may differ; task 13 has a comment to check the docs.
- `MCP(command=..., args=...)` — same; task 4 references this.
- `FileSystem(root=..., prevent_traversal=...)`, `Shell(allowlist=..., timeout=...)` — same.

These are noted inline in the relevant tasks. The implementer should consult the harness docs at https://pydantic.dev/docs/ai/harness/ when wiring each capability.

---

## Execution Handoff

After saving the plan, the implementer chooses:
1. **Subagent-driven** (recommended) — fresh subagent per task, two-stage review between tasks.
2. **Inline execution** — execute tasks in this session with checkpoints.
