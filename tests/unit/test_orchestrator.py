from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.query.tag_expr import Include, ResolvedTagFilter
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def wiki_root(tmp_path: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    return make_wiki(name="orch-test", data_root=tmp_path)


def test_orchestrator_constructs(wiki_root: Path) -> None:
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert orch is not None


def test_orchestrator_runs_with_test_model(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator's underlying agent is mocked via TestModel.

    TestModel is configured with `call_tools=[]` so it returns a plain text
    response instead of trying to invoke the orchestrator's many tools
    (delegate_task, run_workflow, run_code, etc.) -- which would loop on
    invalid workflow scripts.

    The default `transport="http"` registers qmd's tools as native MCP
    tools, which TestModel rejects (``UserError: TestModel does not
    support built-in tools``). Opt out via ``LIES_QMD_TRANSPORT=stdio``
    so the capability builds a local toolset instead -- the test only
    exercises agent plumbing, not the qmd transport.
    """
    monkeypatch.setenv("LIES_QMD_TRANSPORT", "stdio")
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    with orch._agent.override(model=TestModel(call_tools=[], custom_output_text="lint ok")):
        result = orch.run("lint")
    assert isinstance(result, str)
    assert result == "lint ok"


# --- wiki dataclass propagation tests --------------------------------------
#
# The orchestrator is the single entry point for a wiki. The `Wiki`
# dataclass must be propagated consistently to:
#   1. The Orchestrator's top-level state (a first-class attribute).
#   2. The system prompt (so sub-agents know where the wiki lives).
#   3. Capabilities that are wiki-scoped (`file_system(wiki_root=...)`).
#
# These tests pin that contract.


def test_wiki_is_top_level_attribute(wiki_root: Path) -> None:
    """`orch.wiki` must be set from the constructor argument as the Wiki
    dataclass, exposing the post-XDG role-routed paths."""
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert orch.wiki is wiki_root
    assert orch.wiki.data_root == wiki_root.data_root


def test_wiki_data_root_propagates_to_system_prompt(wiki_root: Path) -> None:
    """The agent's system prompt must include the resolved wiki data root path.

    Sub-agents and tool calls rely on this for path scoping and
    path-aware reasoning.
    """
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    prompt = orch._agent._system_prompts[0]  # type: ignore[attr-defined]
    assert str(orch.wiki.data_root) in prompt
    assert "Wiki root:" in prompt


def test_wiki_data_root_propagates_to_file_system_capability(wiki_root: Path) -> None:
    """The file_system capability must be scoped to the wiki data root.

    This is the security boundary that prevents the agent from
    reading or writing outside the wiki.
    """
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))

    # pydantic-ai-harness stores the per-agent capabilities under
    # `agent.root_capability` (a CombinedCapability with a `capabilities`
    # list). Find the FileSystem among them.
    root_cap = orch._agent.root_capability  # type: ignore[attr-defined]
    caps = getattr(root_cap, "capabilities", [])
    fs_caps = [c for c in caps if getattr(c, "__class__", type(c)).__name__ == "FileSystem"]
    assert fs_caps, "expected a FileSystem capability in the orchestrator"
    # FileSystem stores the root under various names depending on the
    # harness version; check the obvious ones.
    fs = fs_caps[0]
    root = (
        getattr(fs, "root", None) or getattr(fs, "root_dir", None) or getattr(fs, "wiki_root", None)
    )
    assert root == orch.wiki.data_root, (
        f"file_system capability root ({root!r}) does not match "
        f"orchestrator wiki.data_root ({orch.wiki.data_root!r})"
    )


def test_wiki_data_root_resolution_handles_relative_paths(tmp_path: Path) -> None:
    """A relative data root must be resolved to an absolute path.

    The CLI passes `--name` through Typer and resolves the data root
    via the XDG helpers; the orchestrator must canonicalize the path
    once at construction so downstream components see a stable root.
    """
    import os

    cwd = tmp_path
    (cwd / "raw").mkdir()
    (cwd / "wiki").mkdir()
    rel = Path("subdir-of-cwd")
    (cwd / rel).mkdir()
    (cwd / rel / "raw").mkdir()
    (cwd / rel / "wiki").mkdir()

    # Run from tmp_path so the relative path resolves against it
    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        rel_wiki = make_wiki(name="relative", data_root=cwd / rel)
        orch = Orchestrator(wiki=rel_wiki, models=models_for_tests("test"))
    finally:
        os.chdir(old_cwd)

    assert orch.wiki.data_root.is_absolute()
    assert orch.wiki.data_root == (cwd / rel).resolve()


def test_orchestrator_uses_qmd_http_transport(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator registers a QmdCapability with http transport."""
    from lies.qmd import capability as qmd_capability
    from lies.qmd.capability import QmdCapability

    built: list[dict] = []
    original_init = QmdCapability.__init__

    def _recording_init(self, **kwargs):  # type: ignore[no-untyped-def]
        built.append(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(qmd_capability.QmdCapability, "__init__", _recording_init)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)
    monkeypatch.delenv("LIES_QMD_TRANSPORT", raising=False)
    monkeypatch.delenv("LIES_QMD_URL", raising=False)

    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert built, "QmdCapability was not constructed"
    assert built[0]["transport"] == "http"
    assert built[0]["url"] == "http://127.0.0.1:8181"
    assert built[0]["wiki"] is orch.wiki


# --- Task 6 / Bundle C — tag_filter plumbing ------------------------------


def test_run_query_threads_tag_filter_through_retriever(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Orchestrator.run_query(tag_filter=...)`` passes the filter down
    into :func:`retrieve_pages`. The retriever resolves it against the
    collection set; this test pins the wiring, not the resolution."""
    from lies.query.synthesizer import PageRead

    captured: dict[str, object] = {}

    def fake_retrieve_pages(*_a: object, **kw: object) -> tuple[list[PageRead], str]:
        captured["tag_filter"] = kw.get("tag_filter")
        return [], ""

    monkeypatch.setattr("lies.orchestrator.retrieve_pages", fake_retrieve_pages)

    # Skip the synthesizer agent (the empty-pages branch never calls it).
    tf = ResolvedTagFilter(include=Include("airflow"))
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    orch.run_query("what is X?", tag_filter=tf)

    assert captured["tag_filter"] == tf


def test_run_query_without_tag_filter_passes_none(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: ``run_query`` without a ``tag_filter`` keyword
    passes ``None`` into :func:`retrieve_pages`."""
    from lies.query.synthesizer import PageRead

    captured: dict[str, object] = {}

    def fake_retrieve_pages(*_a: object, **kw: object) -> tuple[list[PageRead], str]:
        captured["tag_filter"] = kw.get("tag_filter")
        return [], ""

    monkeypatch.setattr("lies.orchestrator.retrieve_pages", fake_retrieve_pages)

    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    orch.run_query("what is X?")

    assert captured["tag_filter"] is None
