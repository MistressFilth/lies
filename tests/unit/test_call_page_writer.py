"""Unit tests for ``Orchestrator._call_page_writer`` and ``Orchestrator._call_source_reader``.

Both wrappers wrap a sub-agent call (via ``page_writer_agent`` /
``source_reader_agent``) and quarantine the source on any ``Exception``
raised from the agent. The tests monkeypatch the module-level factory so
the agent returned by ``page_writer_agent(model=...)`` /
``source_reader_agent(model=...)`` is a small fake that records nothing
but yields a fixed output (or raises on demand). This keeps the unit
test fast and isolated from the real LLM call that the F2 ingest flow
will eventually exercise end-to-end in Task 10.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.agents.page_writer import PageDiff, PageOperation
from lies.agents.source_reader import SourceExtraction
from lies.memory.models import IngestQuarantined
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


class _FakeResult:
    """Mimics pydantic-ai's ``AgentRunResult`` enough for ``.output``."""

    def __init__(self, output: object) -> None:
        self.output = output


class _FakeAgent:
    """Drop-in agent that returns ``output`` or raises ``raise_on_run``.

    ``output`` is whatever the wrapper is expected to pass through (a
    ``PageDiff`` list for the page writer, a ``SourceExtraction`` for
    the source reader). ``raise_on_run`` simulates the agent layer
    blowing up; the wrapper is supposed to quarantine the source and
    re-raise as :class:`IngestQuarantined`.
    """

    def __init__(
        self,
        output: object | None = None,
        raise_on_run: BaseException | None = None,
    ) -> None:
        self.output = output
        self.raise_on_run = raise_on_run

    def run_sync(self, *args: object, **kwargs: object) -> _FakeResult:
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return _FakeResult(self.output)


def _make_wiki(tmp_path: Path):
    """A wiki rooted at ``tmp_path`` with ``wiki/`` and ``raw/`` prepared.

    Mirrors :func:`tests.conftest.make_wiki`'s contract but uses a
    deterministic name so the poison root (``state/lies/<name>/poison``)
    stays isolated per test.
    """
    data_root = tmp_path
    (data_root / "wiki").mkdir(parents=True, exist_ok=True)
    (data_root / "raw").mkdir(parents=True, exist_ok=True)
    return make_wiki(name="call-page-writer", data_root=data_root)


def _fake_extraction() -> SourceExtraction:
    """A deterministic :class:`SourceExtraction` for the success-path tests."""
    return SourceExtraction(
        claims=["Postgres uses MVCC"],
        entities=["Postgres"],
        concepts=["MVCC"],
        comparisons=[],
        summary="Postgres uses MVCC for concurrency.",
    )


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    """An :class:`Orchestrator` over a tmp wiki with ``test`` models.

    ``models_for_tests("test")`` avoids falling back to the real
    ``anthropic:claude-opus-4-7`` default; the test model string is
    pydantic-ai's well-known sentinel and never actually invoked because
    every test monkeypatches the agent factory.
    """
    return Orchestrator(wiki=_make_wiki(tmp_path), models=models_for_tests("test"))


# --- _call_page_writer -----------------------------------------------------


def test_call_page_writer_returns_page_diffs(
    monkeypatch: pytest.MonkeyPatch, orch: Orchestrator
) -> None:
    """Happy path: the page-writer agent's ``PageDiff`` list is returned.

    The wrapper is a pass-through on the success path; quarantine and
    ``IngestQuarantined`` are only invoked when the agent raises.
    """
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.CREATE,
                    path=Path("wiki/foo/concepts/alpha.md"),
                    new_content="# Alpha\n",
                )
            ]
        ),
    )
    diffs = orch._call_page_writer(
        extraction=_fake_extraction(),
        existing_pages=[],
        schema_text="",
    )
    assert isinstance(diffs, list)
    assert diffs[0].operation == PageOperation.CREATE


def test_call_page_writer_quarantines_on_agent_failure(
    monkeypatch: pytest.MonkeyPatch, orch: Orchestrator
) -> None:
    """Agent raises -> ``quarantine`` copies the file + sidecar, raise
    :class:`IngestQuarantined`.

    The wrapper receives ``source_relpath`` as ``raw/<collection>/<file>``
    but ``quarantine`` wants the path relative to ``raw/<collection>/``
    (i.e., just the basename); the wrapper strips the prefix before
    delegating. The poison sidecar must exist after the call.
    """
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(raise_on_run=ValueError("rate limit")),
    )
    # Pre-stage the source file under raw/foo/ so quarantine can copy it.
    src = orch.wiki.data_root / "raw" / "foo" / "incoming.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Source\n", encoding="utf-8")
    with pytest.raises(IngestQuarantined):
        orch._call_page_writer(
            extraction=_fake_extraction(),
            existing_pages=[],
            schema_text="",
            source_relpath="raw/foo/incoming.md",
            collection="foo",
        )
    poison = orch.wiki.poison_root / "foo" / "incoming.md"
    assert poison.exists()
    assert poison.with_suffix(poison.suffix + ".reason").exists()


# --- _call_source_reader ---------------------------------------------------


def test_call_source_reader_returns_extraction(
    monkeypatch: pytest.MonkeyPatch, orch: Orchestrator
) -> None:
    """Happy path: the source-reader agent's ``SourceExtraction`` is returned."""
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    raw_path = orch.wiki.data_root / "raw" / "foo" / "incoming.md"
    extraction = orch._call_source_reader(raw_path=raw_path)
    assert isinstance(extraction, SourceExtraction)
    assert "Postgres uses MVCC" in extraction.claims


def test_call_source_reader_quarantines_on_agent_failure(
    monkeypatch: pytest.MonkeyPatch, orch: Orchestrator
) -> None:
    """Agent raises -> ``quarantine`` + :class:`IngestQuarantined`."""
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(raise_on_run=ValueError("rate limit")),
    )
    # Pre-stage the source file under raw/foo/ so quarantine can copy it.
    src = orch.wiki.data_root / "raw" / "foo" / "incoming.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Source\n", encoding="utf-8")
    with pytest.raises(IngestQuarantined):
        orch._call_source_reader(
            raw_path=src,
            collection="foo",
            source_relpath="raw/foo/incoming.md",
        )
    poison = orch.wiki.poison_root / "foo" / "incoming.md"
    assert poison.exists()
    assert poison.with_suffix(poison.suffix + ".reason").exists()
