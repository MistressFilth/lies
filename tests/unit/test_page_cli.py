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


def _mock_resolve_wiki(wiki_dir: Path) -> MagicMock:
    """Return a ``MagicMock`` standing in for the resolved wiki.

    Only ``wiki_dir`` is read by the page-write command (and by
    ``build_author_plan`` via the ``exists`` closure). Other attributes
    are stubbed so attribute access never raises.
    """
    wiki = MagicMock()
    wiki.wiki_dir = wiki_dir
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
