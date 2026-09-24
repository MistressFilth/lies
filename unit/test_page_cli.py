"""CLI tests for ``lies page write`` (F39 + F12).

Mirrors ``tests/unit/test_query_cli.py`` patterns: uses Typer's
CliRunner, mocks ``Orchestrator`` and ``resolve_wiki`` so no on-disk
wiki or real memory service is touched.

Coverage:

- success path prints ``(author: durably filed - create: <path>)``.
- collision without ``--force`` exits 2; the receipt branch is skipped.
- ``--force`` overwrites an existing page (one ``file_back_author`` call).
- ``--dry-run`` validates the plan but never calls ``file_back_author``.
- ``--collection`` missing exits 2 (typer enforces required options).
- F17 (Task 5): ``_SectionRefusal`` from ``build_author_plan`` exits 2
  with the missing-heading message on stderr; ``file_back_author`` is
  never invoked.
- F17 (Task 5): ``wiki.section_contract`` is threaded into the
  ``build_author_plan`` call (production callers see enforcement;
  the default ``None`` is a no-op per Task 3).

The ``lies.cli.page`` module imports ``Orchestrator`` lazily via a
module-level ``__getattr__``; tests mock the symbol at the
``lies.cli.page`` alias — the seam the lazy-import contract keeps open.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.cli.page import page_app
from lies.memory.models import MemoryReceipt, OperationKind, PageReference


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _register_app() -> None:
    """Register ``page_app`` onto the root ``app`` for this test run.

    Task 8 deliberately does NOT register ``page_app`` in
    ``src/lies/cli/__init__.py`` (Task 9 owns that boundary). This
    helper registers it locally for the test process; ``app.add_typer``
    is idempotent when the same ``name`` is added twice, so repeated
    invocations within a session stay safe.
    """
    app.add_typer(page_app, name="page", rich_help_panel="Wiki management")


@pytest.fixture
def mock_orchestrator() -> MagicMock:
    """Patch ``Orchestrator`` at the ``lies.cli.page`` alias.

    The page-write command imports ``Orchestrator`` lazily via
    :func:`__getattr__`; patching the resolved symbol in the page
    module's namespace is the seam the lazy-import contract keeps open.

    The default receipt carries a single ``CREATE`` page reference so
    tests can assert on the ``create:`` branch of the receipt renderer.
    """
    sentinel = MemoryReceipt(
        changed_pages=[
            PageReference(
                path="claude-code/concepts/hooks.md",
                collection_id="claude-code",
                op=OperationKind.CREATE,
            )
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    with patch("lies.cli.page.Orchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.file_back_author = MagicMock(return_value=sentinel)
        # ``current_state`` is the sha lookup ``build_author_plan`` calls
        # on collision; tests that force overwrite need it to return a
        # real-looking sha so ``PageUpdate.expected_sha256`` validates.
        instance._memory_service.current_state = MagicMock(return_value=("a" * 64, "existing"))
        yield instance


def _mock_resolve_wiki(wiki_dir: Path, section_contract=None) -> MagicMock:
    """Return a ``MagicMock`` standing in for the resolved wiki.

    Only ``wiki_dir`` is read by the page-write command (and by
    ``build_author_plan`` via the ``exists`` closure). The F17 refusal
    tests pass ``section_contract`` explicitly so the contract check
    actually fires (a MagicMock's ``__iter__`` yields nothing — Task 3
    + 4 reports both note the brittleness). Other attributes are
    stubbed so attribute access never raises.
    """
    wiki = MagicMock()
    wiki.wiki_dir = wiki_dir
    if section_contract is not None:
        wiki.section_contract = section_contract
    return wiki


def test_write_success_prints_create_receipt(
    runner: CliRunner, mock_orchestrator: MagicMock, tmp_path: Path
) -> None:
    """Body from stdin (--body-file -) is read and the receipt is printed."""
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "claude-code" / "concepts").mkdir(parents=True)

    _register_app()
    with patch("lies.cli.resolve_wiki", return_value=_mock_resolve_wiki(wiki_dir)):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "claude-code",
                "--type",
                "concept",
                "--slug",
                "hooks",
                "--title",
                "Hooks",
                "--body-file",
                "-",
            ],
            input="body content\n",
        )

    assert result.exit_code == 0, result.stdout
    assert "durably filed" in result.stdout
    assert "create:" in result.stdout
    assert "claude-code/concepts/hooks.md" in result.stdout
    assert mock_orchestrator.file_back_author.call_count == 1


def test_write_collision_no_force_exits_2(runner: CliRunner, tmp_path: Path) -> None:
    """Collision without --force exits 2; no apply_plan call is made."""
    body_file = tmp_path / "body.md"
    body_file.write_text("body")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    collision_path = wiki_dir / "claude-code" / "concepts" / "hooks.md"
    collision_path.parent.mkdir(parents=True)
    collision_path.write_text("existing")

    _register_app()
    with patch("lies.cli.resolve_wiki", return_value=_mock_resolve_wiki(wiki_dir)):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "claude-code",
                "--type",
                "concept",
                "--slug",
                "hooks",
                "--title",
                "Hooks",
                "--body-file",
                str(body_file),
            ],
        )

    assert result.exit_code == 2
    # ``err=True`` writes to stderr; ``result.output`` joins both streams.
    assert "page already exists" in result.output


def test_write_force_overwrites(
    runner: CliRunner,
    mock_orchestrator: MagicMock,
    tmp_path: Path,
) -> None:
    """--force bypasses the collision check; one apply_plan call is made."""
    body_file = tmp_path / "body.md"
    body_file.write_text("body")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    collision_path = wiki_dir / "claude-code" / "concepts" / "hooks.md"
    collision_path.parent.mkdir(parents=True)
    collision_path.write_text("existing")

    _register_app()
    with patch("lies.cli.resolve_wiki", return_value=_mock_resolve_wiki(wiki_dir)):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "claude-code",
                "--type",
                "concept",
                "--slug",
                "hooks",
                "--title",
                "Hooks",
                "--body-file",
                str(body_file),
                "--force",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert mock_orchestrator.file_back_author.call_count == 1


def test_write_dry_run_skips_apply(
    runner: CliRunner,
    mock_orchestrator: MagicMock,
    tmp_path: Path,
) -> None:
    """--dry-run validates the plan but never calls file_back_author."""
    body_file = tmp_path / "body.md"
    body_file.write_text("body")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    _register_app()
    with patch("lies.cli.resolve_wiki", return_value=_mock_resolve_wiki(wiki_dir)):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "c",
                "--type",
                "concept",
                "--slug",
                "s",
                "--title",
                "T",
                "--body-file",
                str(body_file),
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert mock_orchestrator.file_back_author.call_count == 0
    assert "dry-run" in result.stdout.lower() or "validated" in result.stdout.lower()


def test_write_missing_collection_exits_2(runner: CliRunner, tmp_path: Path) -> None:
    """Missing --collection surfaces a typer usage error (exit 2)."""
    body_file = tmp_path / "body.md"
    body_file.write_text("body")
    _register_app()
    result = runner.invoke(
        app,
        [
            "page",
            "write",
            "--type",
            "concept",
            "--slug",
            "s",
            "--title",
            "T",
            "--body-file",
            str(body_file),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# F17 Task 5 — refusal surface for ``lies page write``
# ---------------------------------------------------------------------------


def test_write_refuses_missing_sections(
    runner: CliRunner, mock_orchestrator: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F17 Task 5: ``_SectionRefusal`` from ``build_author_plan`` exits 2.

    Mirrors ``test_file_knowledge_refuses_missing_sections`` (Task 4).
    When the wiki's section contract requires headings the body
    omits, ``build_author_plan`` returns a ``_SectionRefusal``. The
    CLI must short-circuit with exit 2 and the missing-heading
    message preserved on stderr — *before* touching
    ``Orchestrator.file_back_author``. Without this the refusal would
    flow through the orchestrator's defensive seam and produce a
    misleading success-shaped receipt (page_path set, op="create").
    """
    from lies.page.author import _SectionRefusal

    refusal = _SectionRefusal(
        error=("missing required section(s) for synthesis: ## Evidence, ## Open Questions"),
        page_type="synthesis",
        slug="what-is-x",
        title="What is X",
    )
    monkeypatch.setattr("lies.cli.page.build_author_plan", lambda **_: refusal)

    body_file = tmp_path / "body.md"
    body_file.write_text("## Thesis\n\nx\n")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    _register_app()
    with patch("lies.cli.resolve_wiki", return_value=_mock_resolve_wiki(wiki_dir)):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "default",
                "--type",
                "synthesis",
                "--slug",
                "what-is-x",
                "--title",
                "What is X",
                "--body-file",
                str(body_file),
            ],
        )

    # Refusal: exit 2 with the missing-heading message on stderr.
    assert result.exit_code == 2, result.output
    joined = result.output
    assert "## Evidence" in joined
    assert "## Open Questions" in joined
    assert "synthesis" in joined
    # ``file_back_author`` must NOT have been invoked — the CLI
    # short-circuits before the orchestrator, matching the MCP
    # behaviour from Task 4.
    assert mock_orchestrator.file_back_author.call_count == 0


def test_write_threads_section_contract_to_plan_builder(
    runner: CliRunner, mock_orchestrator: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F17 Task 5: ``wiki.section_contract`` must reach ``build_author_plan``.

    The wiki's resolved contract (Task 2) is the per-wiki source of
    truth for required headings. The CLI must thread it into
    ``build_author_plan`` so production wikis see enforcement; the
    default-``None`` path (Task 3) is a no-op. We pin the contract
    on identity (``is``) because ``SectionContract`` is a frozen
    Pydantic model with structural equality.
    """
    from lies.memory.models import (
        EvidenceAppend,
        MemoryPlan,
        PageCreate,
        PageDelete,
        PageUpdate,
    )
    from lies.page.author import _SectionRefusal
    from lies.schema.sections import SectionContract

    contract = SectionContract(
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )
    captured: dict[str, object] = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        # Return a plan that satisfies everything so the call proceeds.
        op: PageCreate | PageUpdate | EvidenceAppend | PageDelete = PageCreate(
            path="default/synthesis/what-is-x.md",
            content="stub",
            evidence=["default/what-is-x"],
            tag="synthesis",
        )
        return MemoryPlan(operations=[op], rationale="stub", evidence=["default/what-is-x"])

    monkeypatch.setattr("lies.cli.page.build_author_plan", _capture)

    body_file = tmp_path / "body.md"
    body_file.write_text("## Thesis\n\nx\n\n## Evidence\n\n[[e]]\n\n## Open Questions\n\nx\n")
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    _register_app()
    with patch(
        "lies.cli.resolve_wiki",
        return_value=_mock_resolve_wiki(wiki_dir, section_contract=contract),
    ):
        result = runner.invoke(
            app,
            [
                "page",
                "write",
                "--collection",
                "default",
                "--type",
                "synthesis",
                "--slug",
                "what-is-x",
                "--title",
                "What is X",
                "--body-file",
                str(body_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["section_contract"] is contract
    # Sanity: defensive type-pinning so a future refactor doesn't
    # silently downgrade to ``None``.
    assert not isinstance(captured["section_contract"], _SectionRefusal)
