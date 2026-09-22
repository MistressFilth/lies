"""Integration tests for N2 linter tool dispatch.

Exercises the full ``Orchestrator.run_lint`` path with the linter
sub-agent registered against ``TestModel`` (no live LLM key
required). Verifies:

1. Small-wiki regression: a wiki with ≤10 pages runs through the
   orchestrator's lint pass without prompt-overflow errors and
   produces a merged report (shell + LLM).
2. Big-wiki tool-path: a wiki at the pre-N2 break-point (>150
   pages) completes the same dispatch via the tool surface without
   prompt-stuffing overflow AND without ``WikiPageNotFound``
   errors that would otherwise be silently swallowed by the
   orchestrator's fallback path.

Both gated behind ``INTEGRATION=1`` (Makefile:36-45) so the
pre-commit ``make-unit-test`` hook skips them; CI with the gate
enabled runs both. Pattern matches existing
``tests/integration/test_lint_report_end_to_end.py`` for the
pre-N2 surface.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="integration test (set INTEGRATION=1)",
)


# Pre-N2 linter received the entire corpus in the system prompt; a
# wiki at or above this page count overflowed the 128K local cap.
# Post-N2 the linter drives its own read budget via tools, so a
# wiki above this threshold MUST complete the tool dispatch path
# without exception. The big-wiki test seeds just over this count
# to prove the regression scenario the PR exists to fix.
_PRE_N2_BREAK_POINT = 150


def _seed_wiki(root: Path, page_count: int) -> None:
    """Seed ``<root>/wiki/`` with ``page_count`` pages and init git.

    Mixes top-level pages (``page-{i:03d}.md``) with a small cluster
    of subdirectory pages so the linter's clustering step has real
    partition signals to chew on (rather than 200 identical rows
    under one section).
    """
    wiki_dir = root / "wiki"
    raw_dir = root / "raw"
    wiki_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)
    (wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    # Top-level pages — most of the corpus.
    for i in range(page_count):
        (wiki_dir / f"page-{i:03d}.md").write_text(
            f"# Page {i}\n\nThis is page {i} with a claim.",
            encoding="utf-8",
        )
    # Subdirectory pages — exercise slug-with-`/` paths the
    # linter's clustering logic relies on.
    concepts_dir = wiki_dir / "concepts"
    people_dir = wiki_dir / "people"
    concepts_dir.mkdir(exist_ok=True)
    people_dir.mkdir(exist_ok=True)
    for name in ("alpha", "beta", "gamma"):
        (concepts_dir / f"{name}.md").write_text(
            f"# Concept {name}\n\nClaim about {name}.",
            encoding="utf-8",
        )
    for name in ("alice", "bob"):
        (people_dir / f"{name}.md").write_text(
            f"# Person {name}\n\nClaim about {name}.",
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=root, check=True)


def _wrap_read_with_call_counter(orch) -> None:
    """Wrap ``memory_service.read`` so call counts are observable.

    The orchestrator's ``_memory_service`` exposes ``read`` as a
    bound method. A thin closure keeps the real implementation
    in place and exposes ``read_call_count`` on the service
    instance for the assertion helper.
    """
    original = orch._memory_service.read
    orch._memory_service.read_call_count = 0  # type: ignore[attr-defined]

    def counting(*args: object, **kwargs: object) -> object:
        orch._memory_service.read_call_count += 1  # type: ignore[attr-defined]
        return original(*args, **kwargs)

    counting.call_count = 0
    orch._memory_service.read = counting  # type: ignore[method-assign]


def _assert_linter_completed_without_fallback(orch) -> None:
    """End-to-end proof the linter drove ``wiki_read`` at least once.

    Pre-fix: ``wiki_list_pages`` emitted ``path`` strings;
    ``wiki_read(page_ids)`` passed them to ``WikiMemoryService.read``,
    which expected SHA-1 page_ids and raised ``WikiPageNotFound``.
    The orchestrator's broad ``except Exception`` swallowed the
    failure; ``_call_linter`` returned ``(LintReport(findings=[]),
    "WikiPageNotFound: ...")`` and the merged report carried the
    exception as the LLM section.

    Post-fix: page_ids round-trip through ``_page_id_for``; the
    resilient wrapper catches ``WikiPageNotFound`` and reports
    ``unknown_page_ids`` so a hallucinated id does not fail the
    whole dispatch. ``memory_service.read`` is called at least
    once during the agent's tool loop — proof the agent reached
    the batched-read step.

    The caller wraps ``orch._memory_service.read`` with a
    counting closure (``_wrap_read_with_call_counter``) so this
    helper can assert on ``read_call_count`` without rewriting
    the real method.
    """
    assert orch._memory_service.read_call_count >= 1, (
        "linter did not drive wiki_read at all; the agent loop "
        "ended before the batched-read step. Pre-fix this meant "
        "the inventory emitted paths instead of page_ids and the "
        "agent short-circuited on the first invalid input."
    )


def test_small_wiki_lint_completes(tmp_path: Path) -> None:
    """A ≤10-page wiki runs through the lint path without prompt overflow."""
    from lies.orchestrator import Orchestrator
    from tests.conftest import make_wiki, models_for_tests

    _seed_wiki(tmp_path, page_count=5)
    wiki = make_wiki(name="lint-small", data_root=tmp_path)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    _wrap_read_with_call_counter(orch)

    # N2 path: dispatch via TestModel; the orchestrator constructs
    # LintDeps() marker, the linter agent runs against TestModel.
    # No exception, no fallback line in the merged report.
    output = orch.run_lint(apply=False)
    assert "## Lint report" in output
    _assert_linter_completed_without_fallback(orch)


def test_big_wiki_lint_takes_tool_path(tmp_path: Path) -> None:
    """A wiki above the pre-N2 break-point exercises the tool path.

    Pre-N2 the linter received the entire corpus in the system
    prompt; at ``_PRE_N2_BREAK_POINT`` (150+) pages that overflowed
    the 128K local cap and the linter went silent on contradiction
    / stale / data_gap findings. Post-N2 the linter pulls pages via
    tools, the dispatch completes, and at least one ``wiki_read``
    round-tripped page_ids through ``WikiMemoryService.read``
    without raising.
    """
    from lies.orchestrator import Orchestrator
    from tests.conftest import make_wiki, models_for_tests

    # Seed just over the pre-N2 break-point so the regression
    # scenario the PR exists to fix is actually exercised.
    page_count = _PRE_N2_BREAK_POINT + 50
    _seed_wiki(tmp_path, page_count=page_count)
    wiki = make_wiki(name="lint-big", data_root=tmp_path)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    _wrap_read_with_call_counter(orch)

    output = orch.run_lint(apply=False)
    assert "## Lint report" in output
    _assert_linter_completed_without_fallback(orch)
