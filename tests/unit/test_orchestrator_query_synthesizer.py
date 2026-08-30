"""Unit tests for Orchestrator._call_query_synthesizer + run_query wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.query_synthesizer import QueryAnswer
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    root = tmp_path / "wiki"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n", encoding="utf-8"
    )
    (root / "wiki" / "index.md").write_text(
        "# Index\n\n## concepts\n\n- [Alpha](concepts/alpha.md) — `alpha`\n",
        encoding="utf-8",
    )
    wiki = make_wiki(name="qsynth", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


@pytest.fixture(autouse=True)
def _no_real_qmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never shell out to the real qmd binary from these tests."""
    monkeypatch.setattr(
        "lies.query.synthesizer.qmd_query",
        lambda *a, **kw: [{"path": "concepts/alpha.md", "score": 0.9}],
    )


def _answer(**kwargs: object) -> QueryAnswer:
    base = {
        "answer": "Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)",
        "citations": ["wiki/concepts/alpha.md"],
        "should_file": False,
    }
    base.update(kwargs)
    return QueryAnswer(**base)  # type: ignore[arg-type]


def test_orchestrator_has_query_synthesizer_agent(orch: Orchestrator) -> None:
    assert hasattr(orch, "_query_synthesizer_agent")
    assert orch._query_synthesizer_agent is not None


def test_run_query_uses_agent_answer_on_success(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=_answer(should_file=True)),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is True
    assert result.synthesis_reason == ""
    assert result.answer == "Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)"
    assert result.citations == ["wiki/concepts/alpha.md"]
    assert result.should_file is True


def test_run_query_falls_back_to_extractive_when_agent_raises(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        side_effect=RuntimeError("model exploded"),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is False
    assert result.synthesis_reason == "RuntimeError: model exploded"
    assert "Based on 1 wiki page(s)" in result.answer
    assert result.citations == ["wiki/concepts/alpha.md"]


def test_run_query_drops_citations_the_agent_never_received(orch: Orchestrator) -> None:
    hallucinated = _answer(citations=["wiki/concepts/alpha.md", "wiki/concepts/ghost.md"])
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=hallucinated),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is True
    assert result.citations == ["wiki/concepts/alpha.md"]
    assert "wiki/concepts/ghost.md" in result.synthesis_reason
    assert result.synthesis_reason.startswith("dropped 1 unretrieved citation(s)")


def test_run_query_skips_the_agent_when_no_pages_were_retrieved(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lies.orchestrator.retrieve_pages", lambda *a, **kw: ([], "qmd_no_results"))
    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync") as run_sync:
        result = orch.run_query("what is alpha?")

    assert run_sync.call_count == 0
    assert result.synthesis_used is False
    assert result.fallback_used is True


def test_run_query_passes_full_page_bodies_not_excerpts(orch: Orchestrator) -> None:
    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        orch.run_query("what is alpha?")

    deps = captured["deps"]
    assert deps.question == "what is alpha?"  # type: ignore[attr-defined]
    assert "Alpha is the first letter." in deps.page_texts["wiki/concepts/alpha.md"]  # type: ignore[attr-defined]
    assert "title: Alpha" in deps.page_texts["wiki/concepts/alpha.md"]  # type: ignore[attr-defined]


def test_run_query_skips_unreadable_pages(orch: Orchestrator) -> None:
    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    real_read = Path.read_text

    def flaky_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "alpha.md":
            raise OSError("permission denied")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        mock.patch.object(Path, "read_text", flaky_read),
        mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture),
    ):
        orch.run_query("what is alpha?")

    assert "wiki/concepts/alpha.md" not in captured["deps"].page_texts  # type: ignore[attr-defined]
