"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

import shlex
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


def _split_tail(
    tail: str,
    *,
    value_flags: frozenset[str] = frozenset(),
) -> tuple[list[str], dict[str, str | None]]:
    """Split a slash tail into positional tokens and ``--flag`` values.

    Every prompt takes a single ``str`` that consumes the whole slash
    tail, because hosts that bind slashes to MCP prompts pre-tokenize
    on whitespace and bind tokens positionally to declared parameters
    -- a typed ``bool`` / ``int`` / ``list[str]`` in position two or
    later receives a bare word and fails JSON decode. One ``str`` slot
    has no such problem.

    ``value_flags`` names the flags that consume the following token.
    Flags outside that set are booleans and map to ``None``. A value
    may also be attached with ``--flag=value`` regardless, which keeps
    ``--scraper-timeout=600`` unambiguous next to a bare ``--force``.

    Returns ``(positionals, flags)``.
    """
    tokens = shlex.split(tail) if tail.strip() else []
    positionals: list[str] = []
    flags: dict[str, str | None] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, sep, val = tok[2:].partition("=")
            if sep:
                flags[key] = val
            elif key in value_flags and i + 1 < len(tokens):
                flags[key] = tokens[i + 1]
                i += 1
            else:
                flags[key] = None
        else:
            positionals.append(tok)
        i += 1
    return positionals, flags


def ask_prompt(question: str) -> list[Message]:
    """Synthesized cited answer to a question.

    The tail is consumed as a single positional string; ``+tag`` /
    ``-exclude`` filter markers are parsed out of the question text
    inside the body. The routed ``mcp__lies__search`` /
    ``mcp__lies__lib_ask`` calls carry the extracted filters.
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


def ground_prompt(tail: str) -> list[Message]:
    """Cite-snippet digest (no synthesis).

    Same filter-parsing contract as ``ask_prompt``. ``--top_k=N`` is
    parsed out of the tail before the filter pass and clamped to
    [1, 10].
    """
    positionals, flags = _split_tail(tail)
    top_k = 3
    raw_top_k = flags.get("top_k")
    if raw_top_k is not None:
        try:
            top_k = max(1, min(10, int(raw_top_k)))
        except ValueError:
            top_k = 3
    question = " ".join(positionals)
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    body = (
        f"Call mcp__lies__search({query_text!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "\u2264200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask \u2014 ground is digest-only."
    )
    return [Message(body)]


def collections_prompt(tail: str) -> list[Message]:
    """Library collection registry CRUD."""
    positionals, _flags = _split_tail(tail)
    sub = positionals[0].lower() if positionals else ""
    args = positionals[1:]
    valid = "list, add, remove, modify, info, tag, register-shipped, where"

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
            "Register a new collection: run "
            "Bash(lies library new <name> <path> [--tags <t1,t2>] "
            "[--scope <keywords>] [--synonyms <file>] [--scraper <cmd>]) "
            f"with args {joined!r}. Then run qmd embed so vec/hyde queries "
            "find the new collection."
        )
    elif sub in ("remove", "modify"):
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library {sub} {joined}) and report the tool's "
            "outcome to the user verbatim."
        )
    elif sub == "tag":
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library enrich-tags {joined}) and report which "
            "collections now carry the tag."
        )
    elif sub in ("register-shipped", "where"):
        joined = " ".join(args)
        body = f"Run Bash(lies library {sub} {joined}) and surface the stdout stream to the user."
    elif not sub:
        body = f"No subcommand given. Valid subcommands: {valid}. Ask the user which to invoke."
    else:
        body = (
            f"Unknown subcommand {sub!r}. Valid subcommands: {valid}. Ask the user which to invoke."
        )
    return [Message(body)]


def ingest_prompt(tail: str) -> list[Message]:
    """Route a source into the library."""
    positionals, flags = _split_tail(
        tail,
        value_flags=frozenset({"type", "slug", "title", "batch", "slug-prefix", "delete"}),
    )
    dry = " --dry-run" if "dry-run" in flags else ""
    delete_slug = flags.get("delete")
    batch_dir = flags.get("batch")
    source = positionals[0] if positionals else "<source>"

    if delete_slug:
        cmd = f'lies ingest --data-dir "$LIES_DATA"{dry} --delete {delete_slug}'
        body = (
            f"Run Bash({cmd!r}) and surface stdout/stderr. The CLI "
            "removes the page file, the catalog row, appends a "
            "delete entry to the log, and triggers qmd update."
        )
    elif batch_dir:
        prefix = flags.get("slug-prefix") or "<derive-from-user>"
        body = (
            f"Run Bash(lies ingest --batch {batch_dir!r}{dry} "
            f'--data-dir "$LIES_DATA" --slug-prefix {prefix} '
            "--force) \u2014 slug-prefix is required. Surface stdout/stderr."
        )
    else:
        page_type = flags.get("type") or "<entity|concept|synthesis|...>"
        extra = ""
        if flags.get("slug"):
            extra += f" --slug {flags['slug']}"
        if flags.get("title"):
            extra += f' --title "{flags["title"]}"'
        body = (
            f'Run Bash(lies ingest --data-dir "$LIES_DATA"{dry} '
            f"--source {source!r} --type {page_type}{extra}) \u2014 supervised mode "
            "requires --type. Surface stdout/stderr."
        )
    return [Message(body)]


def lint_prompt(tail: str) -> list[Message]:
    """Health-check the corpus."""
    _positionals, flags = _split_tail(tail, value_flags=frozenset({"check"}))
    check = flags.get("check")
    fix = "fix" in flags
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


def reindex_prompt(tail: str) -> list[Message]:
    """Rebuild the search index."""
    _positionals, flags = _split_tail(tail)
    reconcile = "reconcile" in flags
    embed = "embed" in flags
    force = "force" in flags
    cleanup = "cleanup" in flags
    all_ = "all" in flags
    destructive = cleanup or all_
    body = (
        f"Call mcp__lies__reindex(reconcile={reconcile}, embed={embed}, "
        f"force={force}, cleanup={cleanup}, all_={all_}, "
        "name=None) and surface the returned ReindexResult envelope. "
        + (
            "cleanup/all_ are destructive \u2014 wait for the host's "
            "elicit-confirmation step before re-dispatching."
            if destructive
            else ""
        )
    )
    return [Message(body)]


def sync_prompt(tail: str) -> list[Message]:
    """Pull + ingest remote sources."""
    positionals, flags = _split_tail(tail, value_flags=frozenset({"jobs", "scraper-timeout"}))
    # ``all`` is a no-op marker meaning "every collection with a
    # scraper", which is also the CLI's behaviour with no --only.
    names = [p for p in positionals if p.lower() != "all"]
    out: list[str] = []
    if names:
        out.append(f"--only {' '.join(names)}")
    for flag, label in (
        ("no-ingest", "--no-ingest"),
        ("force", "--force"),
        ("dry-run", "--dry-run"),
    ):
        if flag in flags:
            out.append(label)
    jobs = flags.get("jobs")
    if jobs is not None and jobs != "4":
        out.append(f"--jobs {jobs}")
    timeout = flags.get("scraper-timeout")
    if timeout is not None and timeout != "300":
        out.append(f"--scraper-timeout {timeout}")

    flag_str = (" " + " ".join(out)) if out else ""
    body = (
        f'Run Bash(lies sync --data-dir "$LIES_DATA"{flag_str}) and '
        "surface each collection's scrape/ingest status. "
        "Phase 3 (qmd reconcile + update + embed + cleanup) runs "
        "automatically unless --dry-run or --no-ingest is set."
    )
    return [Message(body)]


def register_all(mcp: FastMCP) -> None:
    """Wire each impl function as ``@mcp.prompt`` on ``mcp``.

    Every prompt takes exactly one ``str`` that consumes the whole
    slash tail. Hosts that bind slashes to MCP prompts pre-tokenize the
    tail on whitespace and bind tokens positionally to declared
    parameters, so a typed parameter in position two or later receives
    a bare word and fails JSON decode. One string slot has no such
    problem, and the flags are parsed inside each body.
    """

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
    def _ground_prompt(tail: str) -> list[Message]:
        return ground_prompt(tail)

    @mcp.prompt(
        name="collections",
        description=(
            "Library collection registry CRUD "
            "(list, add, remove, modify, info, tag, register-shipped, where)."
        ),
    )
    def _collections_prompt(tail: str) -> list[Message]:
        return collections_prompt(tail)

    @mcp.prompt(
        name="ingest",
        description=(
            "Bring a source into the library (single source, --batch directory, or --delete slug)."
        ),
    )
    def _ingest_prompt(tail: str) -> list[Message]:
        return ingest_prompt(tail)

    @mcp.prompt(
        name="lint",
        description=(
            "Health-check the LIES library "
            "(optionally scoped to one check, optionally with a repair pass)."
        ),
    )
    def _lint_prompt(tail: str) -> list[Message]:
        return lint_prompt(tail)

    @mcp.prompt(
        name="reindex",
        description=("Rebuild the LIES search index (reconcile, embed, force, cleanup, all)."),
    )
    def _reindex_prompt(tail: str) -> list[Message]:
        return reindex_prompt(tail)

    @mcp.prompt(
        name="sync",
        description=(
            "Pull + ingest remote sources, then reindex (scoped to named collections or all)."
        ),
    )
    def _sync_prompt(tail: str) -> list[Message]:
        return sync_prompt(tail)
