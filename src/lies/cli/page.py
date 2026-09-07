"""``lies page`` — generic page-author CLI (F39 + F12).

Subcommands
-----------
``write``  Write one page to the wiki via ``WikiMemoryService.apply_plan``.

The CLI is intentionally a sub-app so future authoring commands (lint
of a single page, ``read``, etc.) can hang off the same ``page``
namespace. The sub-app is registered onto the root ``app`` from
:mod:`lies.cli.__init__` (like the other ``*_app`` sub-apps).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

if TYPE_CHECKING:
    # Type-checker-only import for the lazy ``__getattr__`` re-export below.
    # The runtime import lives in ``__getattr__`` so ``import lies.cli``
    # does not pull in the memory service stack (pydantic_ai, fastmcp).
    from lies.memory.models import MemoryReceipt  # noqa: TC004

__all__ = ("page_app", "write")


def __getattr__(name: str):
    """Lazy re-export of CLI command body's heavy dependencies.

    Tests that ``mock.patch("lies.cli.page.Orchestrator", ...)`` keep
    working without ``import lies.orchestrator`` running at module top.
    Routing the lookup through a module-level ``__getattr__`` also lets
    the write command reference ``Orchestrator`` and ``build_author_plan``
    as bare names (PEP 562 module attribute lookup). First access
    triggers the import and caches the symbol on the module's globals so
    subsequent reads are a normal attribute access.

    Same contract that :mod:`lies.cli.ingestion` and :mod:`lies.cli.memory`
    follow — ``import lies.cli`` does not pull pydantic_ai / fastmcp /
    anthropic into ``sys.modules``.
    """
    if name == "Orchestrator":
        from lies.orchestrator import Orchestrator as _Orchestrator

        globals()[name] = _Orchestrator
        return _Orchestrator
    if name == "MemoryReceipt":
        from lies.memory.models import MemoryReceipt as _MemoryReceipt

        globals()[name] = _MemoryReceipt
        return _MemoryReceipt
    if name == "build_author_plan":
        from lies.page import build_author_plan as _build_author_plan

        globals()[name] = _build_author_plan
        return _build_author_plan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


page_app = typer.Typer(
    name="page",
    help="Author and inspect wiki pages directly.",
    rich_help_panel="Wiki management",
    no_args_is_help=True,
)


_TYPE_PLURAL: dict[str, str] = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "source": "sources",
    "synthesis": "synthesis",
}


def _rel_path(page_type: str, collection: str, slug: str) -> str:
    """Return the wiki-relative path for one authored page.

    Mirrors the path resolution in :func:`lies.page.build_author_plan`
    so the CLI's collision pre-check lines up with what the plan
    builder would emit.
    """
    if page_type == "overview":
        return "wiki/overview.md"
    if page_type not in _TYPE_PLURAL:
        # Type validation lives inside ``build_author_plan``; the CLI
        # defers to it for the structured WikiPlanInvalid envelope.
        return ""
    return f"{collection}/{_TYPE_PLURAL[page_type]}/{slug}.md"


def _read_body(body_file: Path) -> str:
    """Read the page body from a file path; ``-`` means stdin."""
    if str(body_file) == "-":
        return sys.stdin.read()
    return body_file.read_text(encoding="utf-8")


def _print_receipt(rel_path: str, receipt: "MemoryReceipt") -> None:
    """Print a one-line ``(author: ...)`` receipt, mirroring the query CLI."""
    if receipt.errors:
        typer.echo(f"(author: error — {receipt.errors[0]})")
        return
    if receipt.changed_pages:
        op_kind = (
            "update" if any(p.op.name == "UPDATE" for p in receipt.changed_pages) else "create"
        )
        typer.echo(f"(author: durably filed\n  - {op_kind}: {rel_path}\n)")
        return
    typer.echo("(author: durably filed)")


@page_app.command(name="write")
def write(
    collection: str = typer.Option(..., "--collection", help="Target collection."),
    type: str = typer.Option(
        ...,
        "--type",
        help="Page type: overview|entity|concept|comparison|source|synthesis.",
    ),
    slug: str = typer.Option(..., "--slug", help="Page slug."),
    title: str = typer.Option(..., "--title", help="Page title."),
    body_file: Path = typer.Option(..., "--body-file", help="Path to body (- for stdin)."),
    derived_from: list[str] = typer.Option(
        [], "--derived-from", help="Provenance slugs (repeatable)."
    ),
    tag: list[str] = typer.Option([], "--tag", help="Frontmatter tags (repeatable)."),
    sources: list[str] = typer.Option([], "--sources", help="Frontmatter sources (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate + plan, do not write."),
    force: bool = typer.Option(False, "--force", help="Overwrite on collision."),
    name: Optional[str] = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to write into."
    ),
) -> None:
    """Write one page to the wiki via ``WikiMemoryService.apply_plan``."""
    # Heavy imports live inside the command body so ``import lies.cli``
    # does not pull the orchestrator + pydantic_ai stack. The
    # module-level ``__getattr__`` exposes ``Orchestrator`` and
    # ``build_author_plan`` as lazy attributes; ``globals()`` first
    # short-circuits to a test-supplied ``mock.patch("lies.cli.page.
    # Orchestrator", ...)`` when present so the patched symbol wins.
    orch_cls = globals().get("Orchestrator") or __getattr__("Orchestrator")
    plan_builder = globals().get("build_author_plan") or __getattr__("build_author_plan")
    from lies.cli import resolve_wiki

    wiki = resolve_wiki(name)
    body = _read_body(body_file)

    rel_path = _rel_path(type, collection, slug)
    if not rel_path:
        typer.echo(f"error: unknown page type {type!r}", err=True)
        raise typer.Exit(code=2)

    exists = (wiki.wiki_dir / rel_path).exists()
    if exists and not force:
        typer.echo(
            f"error: page already exists at {rel_path}; pass --force to overwrite",
            err=True,
        )
        raise typer.Exit(code=2)

    orch = orch_cls(wiki)

    try:
        plan = plan_builder(
            type=type,
            collection=collection,
            slug=slug,
            title=title,
            body=body,
            derived_from=derived_from,
            tags=tag,
            sources=sources,
            exists=lambda r: (wiki.wiki_dir / r).exists(),
            sha_lookup=lambda r: orch._memory_service.current_state(r)[0],
        )
    except Exception as exc:  # noqa: BLE001 - plan validation surfaces as exit 2
        typer.echo(f"error: plan_invalid: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if dry_run:
        typer.echo(f"(dry-run: validated — would write {rel_path})")
        raise typer.Exit(code=0)

    receipt = orch.file_back_author(plan)
    _print_receipt(rel_path, receipt)
