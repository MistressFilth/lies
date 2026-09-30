"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.prompts import Message  # used in every impl fn's return type

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _parse_question_filters(
    question: str,
) -> tuple[str, str | None, list[str] | None]:
    """Strip ``+tag`` / ``-exclude`` filter tokens out of a slash tail.

    Claude Code's slash-command parser splits the slash tail on
    whitespace into positional tokens and binds each to the
    prompt's typed parameters in **declared** order. Multi-word
    question text cannot bind to a single ``str`` parameter; the
    text gets shredded across typed fields and FastMCP rejects
    the non-string value with a JSON-parse error.

    The slash-UX convention (mirroring the ask plugin) is to
    parse the existing ``+tag`` and ``-tag`` filter markers out
    of the question string itself. Tokens starting with ``+`` are
    include atoms (OR-joined into ``tag_expr``); tokens starting
    with ``-`` are collected as ``exclude_tags``; everything else
    is the actual question text.

    Quoted question text is out of scope for v0.41.
    """
    plus_atoms: list[str] = []
    minus_tags: list[str] = []
    text_tokens: list[str] = []
    for token in question.split():
        if token.startswith("+") and len(token) > 1:
            plus_atoms.append(token[1:])
        elif token.startswith("-") and len(token) > 1:
            minus_tags.append(token[1:])
        else:
            text_tokens.append(token)

    tag_expr = "|".join(plus_atoms) if plus_atoms else None
    exclude_tags = minus_tags if minus_tags else None
    query_text = " ".join(text_tokens)
    return query_text, tag_expr, exclude_tags


def register_all(mcp: FastMCP) -> None:
    """Wire each impl function as ``@mcp.prompt`` on ``mcp``."""

    @mcp.prompt(
        name="ask",
        description=(
            "Synthesize a cited answer from the LIES library. "
            "+tag / -tag filter tokens in question scope the search."
        ),
    )
    def _ask_prompt(question: str) -> list[Message]:
        return ask_prompt(question)

    @mcp.prompt(
        name="ground",
        description=(
            "Cite-snippet digest from the LIES library. "
            "+tag / -tag filter tokens in question scope the search. "
            "Returns verbatim snippets, no synthesis."
        ),
    )
    def _ground_prompt(question: str, top_k: int = 3) -> list[Message]:
        return ground_prompt(question, top_k)

    @mcp.prompt(
        name="collections",
        description=(
            "Library collection registry CRUD "
            "(list, add, remove, modify, info, tag, register-shipped, where)."
        ),
    )
    def _collections_prompt(
        subcommand: str,
        args: list[str] | None = None,
    ) -> list[Message]:
        return collections_prompt(subcommand, args)

    @mcp.prompt(
        name="ingest",
        description=(
            "Bring a source into the library (single source, --batch directory, or --delete slug)."
        ),
    )
    def _ingest_prompt(
        source: str,
        delete_slug: str | None = None,
        batch_dir: str | None = None,
        dry_run: bool = False,
    ) -> list[Message]:
        return ingest_prompt(source, delete_slug, batch_dir, dry_run)

    @mcp.prompt(
        name="lint",
        description=(
            "Health-check the LIES library "
            "(optionally scoped to one check, optionally with a repair pass)."
        ),
    )
    def _lint_prompt(
        check: str | None = None,
        fix: bool = False,
    ) -> list[Message]:
        return lint_prompt(check, fix)

    @mcp.prompt(
        name="reindex",
        description=("Rebuild the LIES search index (reconcile, embed, force, cleanup, all)."),
    )
    def _reindex_prompt(
        reconcile: bool = False,
        embed: bool = False,
        force: bool = False,
        cleanup: bool = False,
        all_: bool = False,
    ) -> list[Message]:
        return reindex_prompt(reconcile, embed, force, cleanup, all_)

    @mcp.prompt(
        name="sync",
        description=(
            "Pull + ingest remote sources, then reindex (scoped to named collections or all)."
        ),
    )
    def _sync_prompt(
        collections: list[str] | None = None,
        no_ingest: bool = False,
        force: bool = False,
        dry_run: bool = False,
        jobs: int = 4,
        scraper_timeout: int = 300,
    ) -> list[Message]:
        return sync_prompt(collections, no_ingest, force, dry_run, jobs, scraper_timeout)


# Concrete impl functions added by tasks 3-9 below.


def ask_prompt(question: str) -> list[Message]:
    """Synthesized cited answer to a question.

    The slash tail is consumed as a single positional string;
    ``+tag`` / ``-exclude`` filter markers are parsed out of the
    question text inside the body. The routed
    ``mcp__lies__search`` / ``mcp__lies__lib_ask`` calls carry the
    extracted filters, not the raw question.
    """
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    body = (
        f"Call mcp__lies__search({query_text!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Then call mcp__lies__lib_ask({query_text!r}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r}) for "
        f"a synthesized cited answer. "
        f'Cite each claim as [[collection/slug]]: "verbatim quote from the cited span".'
    )
    return [Message(body)]


def ground_prompt(question: str, top_k: int = 3) -> list[Message]:
    """Cite-snippet digest (no synthesis).

    Same filter-parsing contract as ``ask_prompt``: ``+tag`` /
    ``-exclude`` markers are stripped out of the question text
    and routed into ``tag_expr`` / ``exclude_tags``.
    """
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    body = (
        f"Call mcp__lies__search({query_text!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "≤200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask — ground is digest-only."
    )
    return [Message(body)]


def collections_prompt(
    subcommand: str,
    args: list[str] | None = None,
) -> list[Message]:
    """Library collection registry CRUD."""
    args = list(args or [])
    sub = subcommand.strip().lower()

    if sub == "list":
        body = (
            'Call mcp__lies__collections_read(subcommand="list") and '
            "render each entry as one markdown bullet (name, tags, "
            "source, page_count, updated_at)."
        )
    elif sub == "info":
        name = args[0] if args else "<name>"
        body = (
            f'Call mcp__lies__collections_read(subcommand="info", '
            f"name={name!r}) and render the returned metadata envelope."
        )
    elif sub == "add":
        joined = " ".join(args)
        body = (
            f"Register a new collection: run "
            f"Bash(lies library new <name> <path> [--tags <t1,t2>] "
            f"[--scope <keywords>] [--synonyms <file>] [--scraper <cmd>]) "
            f"with args {joined!r}. Then run qmd embed so vec/hyde queries "
            f"find the new collection."
        )
    elif sub in ("remove", "modify"):
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library {sub} {joined}) and report the tool's "
            f"outcome to the user verbatim."
        )
    elif sub == "tag":
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library enrich-tags {joined}) and report which "
            f"collections now carry the tag."
        )
    elif sub in ("register-shipped", "where"):
        joined = " ".join(args)
        body = f"Run Bash(lies library {sub} {joined}) and surface the stdout stream to the user."
    else:
        body = (
            f"Unknown subcommand {sub!r}. Valid subcommands: "
            f"list, add, remove, modify, info, tag, register-shipped, where. "
            f"Ask the user which to invoke."
        )
    return [Message(body)]


def ingest_prompt(
    source: str,
    delete_slug: str | None = None,
    batch_dir: str | None = None,
    dry_run: bool = False,
) -> list[Message]:
    """Route a source into the library."""
    dry = " --dry-run" if dry_run else ""
    if delete_slug:
        cmd = f'lies ingest --data-dir "$LIES_DATA"{dry} --delete {delete_slug}'
        body = (
            f"Run Bash({cmd!r}) and surface stdout/stderr. The CLI "
            f"removes the page file, the catalog row, appends a "
            f"delete entry to the log, and triggers qmd update."
        )
    elif batch_dir:
        body = (
            f"Run Bash(lies ingest --batch {batch_dir!r}{dry} "
            f'--data-dir "$LIES_DATA" --slug-prefix <derive-from-user> '
            f"--force) — slug-prefix is required. Surface stdout/stderr."
        )
    else:
        body = (
            f'Run Bash(lies ingest --data-dir "$LIES_DATA"{dry} '
            f"--source {source!r} --type <entity|concept|synthesis|...> "
            f'[--slug <slug>] [--title "<title>"]) — supervised mode '
            f"requires --type. Surface stdout/stderr."
        )
    return [Message(body)]


def lint_prompt(
    check: str | None = None,
    fix: bool = False,
) -> list[Message]:
    """Health-check the corpus."""
    body = (
        f"Call mcp__lies__lint(name=None, check={check!r}, "
        f"fix={fix!r}) and surface the returned report. "
        + (
            "When fix=True, narrate any repair outcomes the tool "
            "applied and re-run lint to confirm clean state."
            if fix
            else ""
        )
    )
    return [Message(body)]


def reindex_prompt(
    reconcile: bool = False,
    embed: bool = False,
    force: bool = False,
    cleanup: bool = False,
    all_: bool = False,
) -> list[Message]:
    """Rebuild the search index."""
    destructive = cleanup or all_
    body = (
        f"Call mcp__lies__reindex(reconcile={reconcile}, embed={embed}, "
        f"force={force}, cleanup={cleanup}, all_={all_}, "
        f"name=None) and surface the returned ReindexResult envelope. "
        + (
            "cleanup/all_ are destructive — wait for the host's "
            "elicit-confirmation step before re-dispatching."
            if destructive
            else ""
        )
    )
    return [Message(body)]


def sync_prompt(
    collections: list[str] | None = None,
    no_ingest: bool = False,
    force: bool = False,
    dry_run: bool = False,
    jobs: int = 4,
    scraper_timeout: int = 300,
) -> list[Message]:
    """Pull + ingest remote sources."""
    names = list(collections or [])
    flags = []
    if names:
        flags.append(f"--only {' '.join(names)}")
    if no_ingest:
        flags.append("--no-ingest")
    if force:
        flags.append("--force")
    if dry_run:
        flags.append("--dry-run")
    if jobs != 4:
        flags.append(f"--jobs {jobs}")
    if scraper_timeout != 300:
        flags.append(f"--scraper-timeout {scraper_timeout}")

    flag_str = (" " + " ".join(flags)) if flags else ""
    body = (
        f'Run Bash(lies sync --data-dir "$LIES_DATA"{flag_str}) and '
        f"surface each collection's scrape/ingest status. "
        f"Phase 3 (qmd reconcile + update + embed + cleanup) runs "
        f"automatically unless --dry-run or --no-ingest is set."
    )
    return [Message(body)]
