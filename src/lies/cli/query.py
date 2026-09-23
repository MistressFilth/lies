"""Querying and maintenance panel: query, lint, status.

Orchestrator + qmd + rich imports stay inside command bodies so
``import lies.cli`` doesn't pay for the model stack or the rich
markdown renderer (markdown-it is ~30ms of cold-start).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Annotated

import typer

from lies.cli import app
from lies.cli._helpers import (
    WikiFlockUnrepairable,
    WikiLockBusy,
    configure_logging,
)

if TYPE_CHECKING:
    # Imported only for the type annotations on the
    # ``_build_compat_run_query_kwargs`` helper. Importing at module
    # top-level transitively pulls in ``pydantic_ai`` and ``fastmcp``,
    # which the CLI must keep off its lazy-import path (see
    # ``tests/unit/cli/test_cli_lazy_imports``).
    from lies.query.tag_expr import ResolvedTagFilter

__all__ = (
    "lint",
    "query",
    "status",
)


def _collect_available_tags(wiki) -> set[str]:  # noqa: ANN001 - Wiki import is lazy
    """Return every addressable tag in the library.

    Thin shim over :func:`lies.library.registry.library_collection_names`
    — kept so the CLI ``query`` boundary has the same helper name as
    its MCP counterpart. ``wiki`` is accepted for signature uniformity
    with the legacy per-wiki resolution but is intentionally ignored:
    collections live in the library, not in any wiki.
    """
    from lies.library.registry import library_collection_names

    return set(library_collection_names())


def _build_compat_run_query_kwargs(
    *,
    tag_filter: ResolvedTagFilter | None,
    collection: str | None,
    file_back: bool,
) -> dict[str, object]:
    """Build the F18/F19 ``Orchestrator.run_query`` kwargs from the CLI's legacy surface.

    Translates the pre-F18 / Bundle-C kwargs to the new orchestrator
    surface:

    - ``tag_filter.include`` (a ``TagExpr`` AST) → ``tag_expr`` (the
      body of a single include expression, rendered from the AST).
    - ``tag_filter.exclude`` (a single string) →
      ``exclude_tags=[<exclude>]`` (the F18 librarian's list
      contract).
    - ``collection=<name>`` → ``tag_expr="c:<name>"`` (the F15
      strict-name prefix; matches what the MCP ``query`` tool does
      for the same kwarg). When the user passed a tag filter, the
      CLI merges them by OR-ing the collection include into the
      tag_expr string (rare but allowed by the F15 grammar).
    - ``file_back`` flows straight through (the F19 setter for the
      filing-back gate).

    Returns the kwargs dict the caller unpacks into ``run_query``.

    The ``tag_filter`` annotation is a forward reference
    (``"ResolvedTagFilter | None"``) so the helper's type signature
    documents the contract without importing ``lies.query.tag_expr``
    at CLI startup — that module transitively pulls in
    ``pydantic_ai`` and ``fastmcp``, which the CLI must keep off its
    lazy-import path (see ``tests/unit/cli/test_cli_lazy_imports``).
    The annotation is consumed by static type checkers; runtime
    callers pass the real ``ResolvedTagFilter`` object.
    """
    from lies.query.tag_expr import _render_include

    include_body: str | None = None
    if tag_filter is not None and tag_filter.include is not None:
        include_body = _render_include(tag_filter.include)
    if collection is not None:
        coll_atom = f"c:{collection}"
        include_body = f"{coll_atom}|{include_body}" if include_body else coll_atom

    exclude_list: list[str] = []
    if tag_filter is not None and tag_filter.exclude is not None:
        exclude_list = [tag_filter.exclude]

    return {
        "tag_expr": include_body,
        "exclude_tags": exclude_list or None,
        "file_back": file_back,
    }


@app.command(
    short_help="Query the wiki with LLM synthesis over qmd hits (extractive fallback).",
    rich_help_panel="Querying and maintenance",
    # ``-amazon`` (the exclude atom) looks like a short option cluster to
    # click. Unknown options fall through to the positional list so the
    # tag-filter grammar owns them; known options are still parsed.
    context_settings={"ignore_unknown_options": True},
)
def query(
    tokens: list[str] = typer.Argument(
        ...,
        metavar="[+tag[&|tag]...] [-tag] QUESTION...",
        help=(
            "Optional tag filter followed by the question. A leading +tag "
            "(atoms joined by & or |, & binding tighter) restricts the search; "
            "a following -tag excludes one tag. Everything left is the question."
        ),
    ),
    collection: str | None = typer.Option(
        None,
        "--collection",
        help=(
            "Collection the synthesized page is filed under "
            "(wiki/<collection>/synthesis/<file>). Required for the file-back "
            "loop to actually write; without it, the agent's should_file "
            "verdict is recorded as a synthesis_reason note."
        ),
    ),
    no_file: bool = typer.Option(
        False,
        "--no-file",
        help="Skip the file-back loop even if the agent marks the answer should_file.",
    ),
    force_file: bool = typer.Option(
        False,
        "--force-file",
        help="Force the file-back loop even if the agent did not mark should_file.",
    ),
    name: str | None = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to query (default: $LIES_WIKI_NAME)."
    ),
    tag_expr: str | None = typer.Option(
        None,
        "--tag-expr",
        help=(
            "Explicit include expression, body only (no leading '+'), "
            "e.g. 'airflow&provider'. Mirrors the MCP tool's tag_expr."
        ),
    ),
    exclude_tag: str | None = typer.Option(
        None,
        "--exclude-tag",
        help=(
            "Explicit single tag to exclude. Accepts an optional "
            "``t:`` or ``c:`` qualifier prefix (F15). Mirrors the "
            "MCP tool's exclude_tags."
        ),
    ),
    format_: str = typer.Option(
        "auto",
        "--format",
        help=(
            "Output format: auto | md | table | marp | chart. Default "
            "'auto' uses the synthesizer's format_hint. Explicit values "
            "force re-synthesis with a constrained prompt if the auto-"
            "route differs."
        ),
    ),
) -> None:
    """Query the wiki with LLM synthesis over qmd hits, with an extractive fallback.

    The positional tokens carry an optional tag filter ahead of the
    question, e.g. `lies query +airflow&provider -amazon what connectors exist?`.
    A collection's own name is always an addressable tag.

    `--tag-expr` / `--exclude-tag` express the same filter without the
    prefix syntax; when either is given the positional tokens are the
    question verbatim. Both forms converge on one resolved filter.
    Grammar errors and unknown tags exit 2. See the design doc
    `2026-09-09-bundle-c-tag-filter-design.md` for the full grammar.

    `--format` selects the output renderer. Default 'auto' uses the
    synthesizer's ``format_hint``. Explicit values force re-synthesis
    with a constrained prompt if the auto-route differs; if the
    orchestrator's override entry point is unavailable (Task 7 not
    yet landed) the command falls back to the first call's answer and
    warns on stderr.
    """
    import sys

    from lies.cli.query_format import render_answer, validate_format_flag
    from lies.memory.models import WikiPlanInvalid
    from lies.query.tag_expr import (
        ResolvedTagFilter,
        TagExpr,
        TagExprEmpty,
        TagExprParseError,
        TagExprUnknown,
        _render_exclude_body,
        check_qualifier,
        parse,
        parse_query_argv,
        resolve,
    )

    # Resolve ``Orchestrator`` / ``resolve_wiki`` through
    # ``lies.cli.__init__`` so the project's existing
    # ``mock.patch("lies.cli.<name>")`` test discipline intercepts
    # the call without per-module indirection. The ``sys.modules``
    # lookup avoids a circular import (this module is itself imported
    # by ``lies.cli.__init__``).
    Orchestrator = sys.modules["lies.cli"].Orchestrator
    resolve_wiki = sys.modules["lies.cli"].resolve_wiki

    configure_logging()
    wiki = resolve_wiki(name)

    include_ast: TagExpr | None = None
    exclude: str | None = None
    exclude_qualifier: str | None = None
    explicit = tag_expr is not None or exclude_tag is not None
    try:
        if explicit:
            # Explicit form wins outright: the positional tokens stay the
            # question so a question that legitimately starts with '+' or
            # '-' is not re-read as a filter.
            question = " ".join(tokens)
            include_ast = parse(tag_expr) if tag_expr is not None else None
            exclude = exclude_tag
            if exclude_tag is not None:
                exclude_qualifier, exclude = check_qualifier(exclude_tag, position=0)
        else:
            # Task 2 (f15-exclude-compound): parse_query_argv now returns a
            # TagExpr | None for the exclude half (previously a flat
            # string). The rest of the CLI's translator still consumes a
            # flat string via ``ResolvedTagFilter.exclude``; render the
            # AST back to a body-only string here so the legacy
            # translator keeps working until Task 3 fully migrates to the
            # AST form. The qualifier info lives on each ``Include`` atom
            # in the tree; the legacy translator drops it (matches the F15
            # default ``None`` semantics for single-atom excludes).
            question, include_ast, exclude_ast, _ = parse_query_argv(tokens)
            exclude = _render_exclude_body(exclude_ast) if exclude_ast is not None else None
            exclude_qualifier = None
    except (TagExprParseError, TagExprEmpty) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        cli_format = validate_format_flag(format_)
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=2) from exc

    tag_filter: ResolvedTagFilter | None = None
    if include_ast is not None or exclude is not None:
        # Only touch the collections dir when a filter is actually
        # present; the back-compat path must not pay for the IO.
        resolved_include: TagExpr | None = None
        if include_ast is not None:
            try:
                resolved_include = resolve(
                    include_ast, available=_collect_available_tags(wiki)
                ).include
            except TagExprUnknown as exc:
                # Surface the shared library-first message (see
                # ``format_unknown_tag_error`` in the MCP server).
                # Strip the leading "unknown tag: 'foo'" line for the
                # CLI's two-line stderr contract; re-print it as
                # ``error: unknown tag: foo`` to keep the existing CLI
                # test regex (``error: unknown tag: <name>``) intact.
                from lies.mcp.server import format_unknown_tag_error

                full = format_unknown_tag_error(exc).splitlines()
                typer.echo(f"error: {full[0]}", err=True)
                for line in full[1:]:
                    typer.echo(f"  {line}", err=True)
                raise typer.Exit(code=2) from exc
        # The exclude is deliberately not validated here — the retriever
        # resolves it against the live collection set (spec: Error model).
        tag_filter = ResolvedTagFilter(
            include=resolved_include,
            exclude=exclude,
            exclude_qualifier=exclude_qualifier,  # type: ignore[arg-type]
        )

    # F18/F19: translate the CLI's legacy kwargs to the new
    # ``Orchestrator.run_query`` surface. ``collection`` (a single
    # collection name) maps to ``tag_expr="c:<name>"`` (the F15
    # ``c:`` strict-name prefix — matches what the MCP ``query``
    # tool does for the same kwarg). ``--force-file`` forces
    # ``file_back=True``; ``--no-file`` flips it off. ``tag_filter``
    # is split into ``tag_expr`` (include body, rendered from the
    # AST) + ``exclude_tags`` (the NOT list, at most one entry per
    # the F15 grammar). The pre-F18 ``collection=`` / ``file=`` /
    # ``force_file=`` / ``tag_filter=`` kwargs were retired in
    # Task 6.
    orch = Orchestrator(wiki)
    orch_kwargs = _build_compat_run_query_kwargs(
        tag_filter=tag_filter,
        collection=collection,
        file_back=force_file or not no_file,
    )
    try:
        answer = orch.run_query(question, **orch_kwargs)
    except WikiPlanInvalid as exc:
        # ``run_query`` raises when the agent/force file marked the answer
        # for filing but the caller did not supply ``--collection``.
        # Without the typed-error envelope a missing collection would
        # silently drop the filing intent; the spec mandates a clean
        # exit-2 + operator-actionable message instead.
        typer.echo(
            "error: --collection NAME required to file synthesis (or pass --no-file to skip)",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    answer_format = getattr(answer, "format_hint", None) or getattr(answer, "format", "md")
    # F1: handle --format override. If the operator's choice differs from
    # the synthesizer's format_hint, re-synthesize with a constrained
    # prompt. The orchestrator exposes a compat ``run_query_with_format``
    # entry point for the override; if it raises, fall back to the
    # first call's answer.
    if cli_format != "auto" and answer_format != cli_format:
        try:
            answer = orch.run_query_with_format(
                question,
                tag_expr=orch_kwargs.get("tag_expr"),
                exclude_tags=orch_kwargs.get("exclude_tags"),
                file_back=orch_kwargs["file_back"],
                format_hint=cli_format,
            )
        except Exception as exc:  # noqa: BLE001 - second call is best-effort
            import logging

            logging.getLogger(__name__).warning(
                "format override re-synthesis failed for --format=%s; "
                "using auto-route (format=%s): %s: %s",
                cli_format,
                answer_format,
                type(exc).__name__,
                exc,
            )
            typer.echo(
                f"warning: re-synthesis for --format={cli_format} failed; "
                f"using auto-route (format={answer_format}).",
                err=True,
            )

    render_format = getattr(answer, "format_hint", None) or getattr(answer, "format", "md")
    if cli_format != "auto":
        render_format = cli_format
    render_answer(render_format, answer.answer)

    # F19: ``QueryAnswer`` no longer carries the pre-F18
    # ``synthesis_reason`` / ``synthesis_used`` envelope. The F19
    # librarian+synthesizer pair always runs synthesis, so the
    # fallback note only surfaces when the orchestrator's CLI form
    # called back into the pre-F18 extractive path (which the F18
    # orchestrator does not expose). Tolerate the field's absence
    # so the new surface keeps the CLI's printing contract.
    synthesis_reason = getattr(answer, "synthesis_reason", None)
    if synthesis_reason:
        if getattr(answer, "synthesis_used", True):
            typer.echo(f"_Note: {synthesis_reason}._")
        else:
            typer.echo(
                f"_Note: LLM synthesis unavailable ({synthesis_reason}); answered extractively._"
            )
    # F3 file-back receipt. Printed only when there is something to say
    # (durable change or error); an empty receipt is silent so the no-op
    # case stays clean. The F19 ``QueryAnswer`` does not surface a
    # ``file_receipt`` (filing-back is best-effort and logs at
    # WARNING on refusal; the CLI is intentionally silent on the
    # success path). Tolerate the field's absence for the F19 surface.
    file_receipt = getattr(answer, "file_receipt", None)
    if file_receipt:
        if file_receipt.changed_pages:
            lines = ["(synthesis: durably filed"]
            for ref in file_receipt.changed_pages:
                lines.append(f"  - {ref.op.value}: {ref.path}")
            lines.append(")")
            typer.echo("\n".join(lines))
        elif file_receipt.errors:
            typer.echo(f"(synthesis: error — {file_receipt.errors[0]})")


@app.command(
    short_help="Run lint; with --fix also apply the repair plan.",
    rich_help_panel="Querying and maintenance",
)
def lint(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to lint (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    fix: Annotated[
        bool, typer.Option("--fix", help="Apply repair plan for safe_to_fix findings.")
    ] = False,
    force_repair: Annotated[
        bool,
        typer.Option(
            "--force-repair",
            help=(
                "Reap a stale memory flock and retry once before applying the "
                "repair plan. Only meaningful with --fix; surfaces "
                "WikiFlockUnrepairable (exit 1) if the retry still loses."
            ),
        ),
    ] = False,
) -> None:
    """Run lint; with --fix also apply the repair plan.

    ``--force-repair`` (with ``--fix``) escalates wiki-memory
    contention: the cross-process flock is unconditionally reaped +
    retried once before applying the repair plan. Without the flag, a
    live contender surfaces as ``WikiLockBusy`` (exit 1). If the
    force-repair retry still loses, ``WikiFlockUnrepairable`` is
    surfaced (also exit 1) with an operator-actionable pointer to
    ``lies flock <name> force-repair``.
    """
    from rich.console import Console
    from rich.markdown import Markdown

    from lies.cli import Orchestrator, WikiLinkCorpusMissing, WikiLinkResolver, resolve_wiki

    configure_logging()
    wiki = resolve_wiki(name)
    try:
        resolver = WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))
    except WikiLinkCorpusMissing:
        typer.echo(f"error: no wiki/ or raw/ directory under {wiki.data_root}", err=True)
        raise typer.Exit(code=2) from None
    orch = Orchestrator(wiki)
    # Use the host-side ``run_lint`` entry point so the lint pass writes
    # a deterministic ``wiki/lint-report.md`` and appends to ``wiki/log.md``.
    try:
        output = orch.run_lint(apply=fix, resolver=resolver, force_repair=force_repair)
    except WikiFlockUnrepairable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except WikiLockBusy as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    Console().print(Markdown(output))


@app.command(
    short_help="Show qmd status, recent invisible writes, and the last few log entries.",
    rich_help_panel="Querying and maintenance",
)
def status(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to report status for (default: $LIES_WIKI_NAME).",
    ),
    memory_limit: int = typer.Option(
        10,
        "--memory-limit",
        help="Number of recent MemoryPlan applications to show. Pass 0 to skip.",
    ),
) -> None:
    """Show qmd status, recent invisible writes, and the last few log entries."""
    from lies.cli import resolve_wiki
    from lies.memory import sidecar
    from lies.qmd import qmd_status
    from lies.wiki.layout import WikiLayout

    if memory_limit < 0:
        raise typer.BadParameter("--memory-limit must be >= 0", param_hint="--memory-limit")
    configure_logging()
    # Library section surfaces before wiki resolution so the line still
    # appears on a fresh wiki (no catalog yet → graceful indicator). The
    # library is independent of any specific wiki, so it does not depend
    # on ``resolve_wiki`` succeeding.
    try:
        from lies.library.catalog import list_pages, open_catalog
        from lies.library.errors import LibraryError
        from lies.library.paths import Library

        lib = Library.open()
        conn = open_catalog(lib)
        try:
            rows = list_pages(conn, section="library")
            quarantine = list_pages(conn, section="library-migrated")
            typer.echo(
                f"library: catalog: {len(rows)} pages in section=library, "
                f"{len(quarantine)} migrated"
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except (OSError, sqlite3.Error, LibraryError):
        # Status is observability; we still want to surface the wiki
        # section. Narrow catch so a regression that mis-spells
        # ``list_pages`` (e.g. ``NameError``) surfaces instead of being
        # silently masked as ``(no catalog yet)``.
        typer.echo("library: (no catalog yet)")
    wiki = resolve_wiki(name)
    root = wiki.data_root
    layout = WikiLayout(root)
    try:
        from lies.memory.catalog import count_pages, open_catalog

        conn = open_catalog(wiki)
        try:
            n_pages = count_pages(conn)
            ver_row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            schema_ver = ver_row[0] if ver_row else "unknown"
        finally:
            conn.close()
        typer.echo(f"catalog: {n_pages} pages, schema v{schema_ver}")
    except Exception:  # noqa: BLE001 - status is observability; never fail the command
        typer.echo("catalog: unavailable")
    typer.echo("=== qmd status ===")
    try:
        typer.echo(qmd_status(root))
    except Exception as exc:  # noqa: BLE001 - qmd failures must not crash the CLI
        typer.echo(f"qmd unavailable: {exc}")
    if memory_limit > 0:
        try:
            rows = sidecar.read_recent(wiki, limit=memory_limit)
        except OSError as exc:
            typer.echo("\n=== recent invisible writes ===")
            typer.echo(f"sidecar unavailable: {exc}")
        else:
            if rows:
                typer.echo("\n=== recent invisible writes ===")
                for rec in rows:
                    typer.echo(sidecar.format_record_block(rec))
    typer.echo("\n=== last 10 log entries ===")
    log_path = layout.wiki_dir / "log.md"
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        for line in lines[-10:]:
            typer.echo(line)
    else:
        typer.echo("(no log yet)")
