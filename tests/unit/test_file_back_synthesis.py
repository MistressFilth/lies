"""Tests for Orchestrator.file_back_synthesis."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import (
    MemoryReceipt,
    PageReference,
    WikiLockBusy,
    OperationKind,
    WikiPlanInvalid,
)
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer
from tests.conftest import make_wiki, models_for_tests


def _answer() -> SynthesizedAnswer:
    return SynthesizedAnswer(
        answer="A hook intercepts events at fixed points in the agent's lifecycle.",
        pages_read=["claude-code/concepts/hooks", "claude-code/concepts/skills"],
        should_file=True,
    )


def test_file_back_synthesis_success(tmp_path: Path) -> None:
    wiki_dir = tmp_path
    wiki = MagicMock()
    wiki.wiki_dir = wiki_dir
    memory_service = MagicMock()
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(
                path="claude-code/synthesis/x.md",
                collection_id="claude-code",
                op=OperationKind.CREATE,
            )
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    memory_service.apply_plan.return_value = receipt

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    ans = _answer()
    out = orch.file_back_synthesis(ans, collection="claude-code")

    assert out is receipt
    assert memory_service.apply_plan.call_count == 1


def test_file_back_synthesis_retries_three_times_on_lock_busy(tmp_path: Path) -> None:
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    memory_service.apply_plan.side_effect = WikiLockBusy("busy")

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 3
    assert out.errors == ["file_back_failed_after_3_attempts: WikiLockBusy: busy"]
    assert out.changed_pages == []


def test_file_back_synthesis_recovers_after_one_retry(tmp_path: Path) -> None:
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(path="x.md", collection_id="claude-code", op=OperationKind.CREATE)
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    memory_service.apply_plan.side_effect = [WikiLockBusy("busy"), receipt]

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 2
    assert out is receipt


def test_file_back_synthesis_does_not_retry_on_wikiplaninvalid_from_apply_plan(
    tmp_path: Path,
) -> None:
    """``WikiPlanInvalid`` raised by ``apply_plan`` falls to the catch-all.

    ``WikiPlanInvalid`` is not in the retryable-exception tuple
    (``WikiLockBusy``/``WikiWriteConflict``/``WikiCommitFailed``), so the
    inline retry loop does not retry: the first call propagates to the
    catch-all ``except Exception`` branch which returns a
    ``MemoryReceipt`` with the error stringified. The receipt carries
    no ``changed_pages``.
    """
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    memory_service.apply_plan.side_effect = WikiPlanInvalid("pages_read empty")

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 1
    assert any("WikiPlanInvalid" in e for e in out.errors)
    assert out.changed_pages == []


def test_file_back_synthesis_does_not_retry_on_wikiplaninvalid_from_build(
    tmp_path: Path,
) -> None:
    """Build-time ``WikiPlanInvalid`` short-circuits before ``apply_plan``.

    ``build_synthesis_plan`` raises ``WikiPlanInvalid`` for plan
    validation failures (e.g. empty ``pages_read``); the orchestrator's
    surrounding ``try`` returns immediately with a
    ``MemoryReceipt(errors=["plan_invalid: ..."])`` and never invokes
    ``apply_plan``.
    """
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()

    with (
        patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None),
        patch(
            "lies.orchestrator.build_synthesis_plan",
            side_effect=WikiPlanInvalid("pages_read is empty; nothing to file"),
        ),
    ):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

        out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 0
    assert out == MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=["plan_invalid: pages_read is empty; nothing to file"],
    )


@pytest.fixture
def real_orch(tmp_path: Path) -> Orchestrator:
    """Real Orchestrator wired to a real WikiMemoryService.

    Existing ``file_back_synthesis`` tests use ``MagicMock`` for the
    service, which silently bypasses ``validate_plan``. This fixture
    stands up the full service so a regression in the validation
    envelope surfaces as a real ``WikiEvidenceMissing`` rather than a
    no-op mock call.

    ``run_query`` resolves ``qmd_search`` through **two** call sites:
    ``lies.query.synthesizer._qmd_search_default`` (set by
    ``set_qmd_search``) and ``lies.memory.retrieval.search_wiki`` which
    falls back to ``lies.qmd.cli.qmd_query`` directly. CI runners do
    not ship ``qmd`` on PATH; stubbing only the synthesizer path leaves
    the retrieval path free to raise ``QmdNotInstalledError`` and
    propagate ``fallback_reason="qmd_stale"`` into the synthesis
    receipt, breaking the no-errors assertion.

    Independently, ``WikiMemoryService.apply_plan`` calls
    ``self._qmd_update(...)`` after every commit (``_refresh_qmd``); a
    missing ``qmd`` binary turns that into ``qmd_stale: ...`` on the
    returned ``MemoryReceipt.errors``. Replace the bound
    ``_qmd_update`` with a no-op so CI runners without ``qmd`` still
    see a clean receipt. Patch both surfaces.
    """
    from unittest.mock import patch

    from lies.query.synthesizer import set_qmd_search

    root = tmp_path / "wiki"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n\nAlpha is the first letter.\n",
        encoding="utf-8",
    )
    (root / "wiki" / "index.md").write_text(
        "# Index\n\n## concepts\n\n- [Alpha](concepts/alpha.md) — `alpha`\n",
        encoding="utf-8",
    )
    wiki = make_wiki(name="fileback", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text(
        "## Page types\n- concept\n- synthesis\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    canned = [{"path": "wiki/concepts/alpha.md", "score": 0.9}]
    set_qmd_search(lambda *a, **kw: canned)
    with patch("lies.qmd.cli.qmd_query", return_value=canned):
        orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
        # Bypass the post-commit qmd refresh; CI runners don't ship qmd.
        orch._memory_service._qmd_update = lambda _path: None  # type: ignore[attr-defined]
        try:
            yield orch
        finally:
            from lies.query.synthesizer import qmd_query

            set_qmd_search(qmd_query)


def test_run_query_registers_pages_read_evidence_before_file_back(
    real_orch: Orchestrator, tmp_path: Path
) -> None:
    """Bug 3 regression: ``run_query`` must register pages_read evidence.

    Before the fix, ``run_query`` called ``file_back_synthesis`` without
    first calling ``register_evidence``; the plan's
    ``evidence=pages_read`` was rejected by ``validate_operation_evidence``
    because ``_known_evidence`` was empty. The receipt carried
    ``plan_invalid: frontmatter type missing or does not match page_type
    'synthesi'`` (Bug 1/Bug 2 compounded with Bug 3) or simply
    ``WikiEvidenceMissing``.

    With all three bugs fixed, ``run_query(file=True, force_file=True)``
    files a real synthesis page on disk; ``apply_plan`` returns a
    receipt whose ``changed_pages`` lists the new file.
    """
    from lies.agents.query_synthesizer import QueryAnswer
    from unittest import mock

    canned = QueryAnswer(
        answer="Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)",
        citations=["wiki/concepts/alpha.md"],
        should_file=True,
    )
    with mock.patch.object(
        type(real_orch._query_synthesizer_agent), "run_sync", return_value=mock.Mock(output=canned)
    ):
        result = real_orch.run_query(
            "what is alpha?",
            collection="wiki",
            file=True,
            force_file=True,
        )

    assert result.synthesis_used is True
    assert result.file_receipt is not None
    # The receipt must show a successful write, not a validation failure.
    assert result.file_receipt.errors == []
    assert result.file_receipt.changed_pages, result.file_receipt

    # And the page must actually exist on disk under the synthesis dir.
    written = [op for op in result.file_receipt.changed_pages if "/synthesis/" in op.path]
    assert written, result.file_receipt
    # ``PageReference.path`` keeps the ``wiki/`` prefix; ``_apply_operations``
    # strips it before resolving against ``wiki_dir`` for the actual write.
    rel = written[0].path.removeprefix("wiki/").removeprefix("wiki/")
    on_disk = (real_orch.wiki.wiki_dir / rel).read_text(encoding="utf-8")
    assert "type: synthesis" in on_disk
    assert "tags: [synthesis]" in on_disk


def test_run_query_file_back_rejects_unknown_evidence(
    real_orch: Orchestrator,
) -> None:
    """Validation envelope: unknown evidence must still trip apply_plan.

    Guards the contract from the other direction: if a plan references
    evidence that was never registered (whether the caller forgot or the
    agent hallucinated), ``apply_plan`` raises. Pairs with the
    regression above to confirm the fix didn't accidentally widen the
    evidence window.
    """
    from lies.memory.models import WikiEvidenceMissing
    from lies.memory.service import build_synthesis_plan

    # Build a plan whose evidence references a page the orchestrator
    # never registered. Without a prior ``register_evidence`` call,
    # ``validate_operation_evidence`` must reject it.
    plan = build_synthesis_plan(
        question="what is a hook?",
        answer="A hook intercepts events.",
        pages_read=["wiki/concepts/ghost.md"],
        collection="wiki",
    )
    with pytest.raises(WikiEvidenceMissing):
        real_orch._memory_service.apply_plan(plan)


def test_run_query_missing_collection_when_should_file_raises(
    real_orch: Orchestrator,
) -> None:
    """Bug-fix regression: missing ``collection`` + ``should_file=True`` raises.

    The orchestrator's ``run_query`` raises :class:`WikiPlanInvalid`
    when the answer is marked for filing (or ``force_file=True``) but
    the caller did not supply ``collection``. The CLI catches the
    typed error and exits 2; the MCP layer re-raises it as a
    ``ToolError``. When ``should_file=False`` and ``force_file=False``,
    missing ``collection`` is irrelevant and the call returns normally.
    """
    from lies.agents.query_synthesizer import QueryAnswer
    from unittest import mock

    canned = QueryAnswer(
        answer="Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)",
        citations=["wiki/concepts/alpha.md"],
        should_file=True,
    )
    with mock.patch.object(
        type(real_orch._query_synthesizer_agent), "run_sync", return_value=mock.Mock(output=canned)
    ):
        with pytest.raises(WikiPlanInvalid, match="collection required to file synthesis"):
            real_orch.run_query(
                "what is alpha?",
                collection=None,
                file=True,
                force_file=False,
            )

    # ``force_file=True`` without ``collection`` also raises.
    with mock.patch.object(
        type(real_orch._query_synthesizer_agent), "run_sync", return_value=mock.Mock(output=canned)
    ):
        with pytest.raises(WikiPlanInvalid, match="collection required to file synthesis"):
            real_orch.run_query(
                "what is alpha?",
                collection=None,
                file=True,
                force_file=True,
            )


def test_run_query_missing_collection_when_no_file_needed_succeeds(
    real_orch: Orchestrator,
) -> None:
    """Missing ``collection`` is a no-op when filing is not requested.

    ``should_file=False`` and ``force_file=False`` mean no filing
    happens regardless of ``collection``. The orchestrator must
    return the synthesized answer normally rather than raising.
    """
    from lies.agents.query_synthesizer import QueryAnswer
    from unittest import mock

    canned = QueryAnswer(
        answer="Alpha is the first letter.",
        citations=["wiki/concepts/alpha.md"],
        should_file=False,
    )
    with mock.patch.object(
        type(real_orch._query_synthesizer_agent), "run_sync", return_value=mock.Mock(output=canned)
    ):
        result = real_orch.run_query(
            "what is alpha?",
            collection=None,
            file=True,
            force_file=False,
        )

    assert result.synthesis_used is True
    assert result.file_receipt is None
