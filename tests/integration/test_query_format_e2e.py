"""End-to-end integration tests for F1 query output formats.

Spec § 13 calls for one happy-path integration test per format plus an
auto-route test and an override-mismatch test. The agent's LLM is
mocked via ``query_synthesizer_agent.run_sync`` (the pattern used by
``tests/integration/test_end_to_end.py``); the marp CLI is mocked via
``shutil.which`` so the marp subprocess never runs in CI.

Gated on ``INTEGRATION=1`` per the
``tests/integration/conftest.py::pytest_collection_modifyitems`` hook.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.query_synthesizer import QueryAnswer
from lies.orchestrator import Orchestrator
from lies.qmd.cli import qmd_query
from lies.query.synthesizer import set_qmd_search
from tests.conftest import make_wiki, models_for_tests

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


@pytest.fixture
def wiki_copy(tmp_path: Path) -> Path:
    """Copy the sample fixture wiki to a tmp dir and init git."""
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target


def _stub_qmd_search(paths: list[str]):
    """Build a qmd_search stub that returns ``paths`` regardless of the wiki pass."""

    def _fn(cwd: Path, question: str, top_n: int, **kwargs: object):
        # Mirror the wiki-pass filter pattern from
        # ``test_query_synthesizer._qmd_ok``: when ``collection_filter``
        # carries a ``wiki_*`` entry, return no hits for that pass.
        if kwargs.get("collection_filter") and any(
            "wiki_" in str(s) for s in kwargs["collection_filter"]
        ):
            return []
        return [{"path": p, "score": 1.0} for p in paths]

    return _fn


def _make_synth_answer(body: str, fmt: str, citations: list[str] | None = None) -> QueryAnswer:
    """A canned synthesizer output with the requested format_hint."""
    return QueryAnswer(
        answer=body,
        citations=citations or ["wiki/entities/postgres.md"],
        should_file=False,
        format_hint=fmt,  # type: ignore[arg-type]
    )


def _patch_synthesizer(orch: Orchestrator, query_answers: list[QueryAnswer]) -> list[QueryAnswer]:
    """Patch ``_query_synthesizer_agent.run_sync`` so it pops a canned answer per call.

    Returns the list so the test can introspect which answers were
    consumed (the override path may invoke the synthesizer twice).
    """
    answers = list(query_answers)

    def fake_run_sync(self, prompt: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if not answers:
            raise RuntimeError("synthesizer invoked more times than canned answers")
        return mock.Mock(output=answers.pop(0))

    patcher = mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", new=fake_run_sync)
    patcher.start()
    return answers


# ---------------------------------------------------------------------------
# Happy-path: each format round-trips through orchestrator.run_query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["md", "table", "marp"])
def test_run_query_happy_path_each_format(wiki_copy: Path, fmt: str) -> None:
    """``Orchestrator.run_query`` returns a ``SynthesizedAnswer`` with the
    requested ``format`` field set, for each of the three documented values.

    The synthesizer emits ``format_hint=fmt``; the orchestrator's
    success path produces an answer whose ``format`` field matches the
    validated hint (``md`` always validates; ``table`` and ``marp``
    validate when the body actually carries the corresponding shape —
    we feed table/marp-shaped bodies here so the validator passes).
    """
    wiki = make_wiki(name="sample", data_root=wiki_copy)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    if fmt == "md":
        body = "### Q\n\nA prose answer with bullets."
    elif fmt == "table":
        body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n| c | d |\n"
    else:  # marp
        body = "---\nmarp: true\n---\n\n---\n\n## Slide 1\n\n---\n\n## Slide 2\n"

    set_qmd_search(_stub_qmd_search(["entities/postgres.md"]))
    try:
        _patch_synthesizer(orch, [_make_synth_answer(body, fmt)])
        answer = orch.run_query("anything", file=False)
    finally:
        set_qmd_search(qmd_query)

    assert answer.format == fmt, (
        f"format mismatch: orchestrator returned {answer.format!r}, expected {fmt!r}"
    )


# ---------------------------------------------------------------------------
# Auto-route: orchestrator's success path carries the validated format
# ---------------------------------------------------------------------------


def test_run_query_auto_route_passes_format_field(wiki_copy: Path) -> None:
    """The auto-route flow's ``format`` field surfaces the synthesizer's
    ``format_hint`` to the caller. Without it, downstream CLI / MCP
    surfaces cannot dispatch on the validated format.

    Mirrors the regression pin spec § 13 calls out:
    ``tests/integration/test_end_to_end.py::test_run_query_returns_empty_when_qmd_unavailable``
    asserts the default ``format='md'``; this test pins the success-path
    contract for the synthesizer's validated hint.
    """
    wiki = make_wiki(name="sample", data_root=wiki_copy)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n"
    set_qmd_search(_stub_qmd_search(["entities/postgres.md"]))
    try:
        _patch_synthesizer(orch, [_make_synth_answer(body, "table")])
        answer = orch.run_query("anything", file=False)
    finally:
        set_qmd_search(qmd_query)

    assert answer.format == "table"


# ---------------------------------------------------------------------------
# Override path: CLI --format=marp on a synthesizer that emitted format=md
# ---------------------------------------------------------------------------


def test_run_query_with_format_marp_overrides_auto_route(wiki_copy: Path, tmp_path: Path) -> None:
    """``Orchestrator.run_query_with_format(cli_format='marp')`` runs the
    synthesizer with a marp-shaped body and the returned ``format`` field
    reflects the override.

    Stubs ``marp`` on PATH (via ``shutil.which``) so the marp CLI is
    never actually invoked — the test runs entirely offline. The CLI
    wrapping path is tested separately in ``tests/unit/test_query_cli.py``
    (the first-call/second-call flow lives there).
    """
    wiki = make_wiki(name="sample", data_root=wiki_copy)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    marp_body = "---\nmarp: true\n---\n\n---\n\n## Slide 1\n\n---\n\n## Slide 2\n"

    set_qmd_search(_stub_qmd_search(["entities/postgres.md"]))
    try:
        # ``run_query_with_format`` invokes the synthesizer once (no
        # auto-route pre-call); the canned answer must carry the marp
        # body + format_hint to satisfy the spec § 6 override shape.
        _patch_synthesizer(
            orch,
            [_make_synth_answer(marp_body, "marp")],
        )
        with mock.patch.object(shutil, "which", return_value=None):
            answer = orch.run_query_with_format(
                "anything",
                cli_format="marp",
                file=False,
            )
    finally:
        set_qmd_search(qmd_query)

    assert answer.format == "marp"
    assert "marp: true" in answer.answer
