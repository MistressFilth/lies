"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastmcp.prompts import Message  # used in every impl fn's return type

if TYPE_CHECKING:
    from fastmcp import FastMCP


# A filter token is ``+`` / ``-`` followed by a letter-led tag atom.
# Requiring a letter as the first post-sigil character keeps ordinary
# question text out of the filter path: ``-1`` (a negative number),
# ``--`` (an em-dash artifact or a bare flag) and ``-`` never match,
# while ``+c:opencode``, ``-t:draft`` and the OR-joined
# ``+c:claude_code|c:opencode`` do.
_FILTER_TOKEN_RE = re.compile(r"^[+-][A-Za-z][A-Za-z0-9_.:,|&+-]*$")


def _parse_question_filters(
    question: str,
) -> tuple[str, str | None, list[str] | None]:
    """Strip ``+tag`` / ``-tag`` filter tokens out of question text.

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

    Splitting is on whitespace alone. Question text is prose, not
    shell, so an apostrophe (``what are Claude Code's differences?``)
    or an unbalanced quote is ordinary text and passes through
    untouched -- shell-quoting semantics would raise on exactly the
    English a user is most likely to type.

    Returns ``(query_text, tag_expr, exclude_tags)``.
    """
    plus_atoms: list[str] = []
    minus_tags: list[str] = []
    text_tokens: list[str] = []
    for token in question.split():
        if not _FILTER_TOKEN_RE.match(token):
            text_tokens.append(token)
        elif token[0] == "+":
            plus_atoms.append(token[1:])
        else:
            minus_tags.append(token[1:])

    tag_expr = "|".join(plus_atoms) if plus_atoms else None
    exclude_tags = minus_tags if minus_tags else None
    query_text = " ".join(text_tokens)
    return query_text, tag_expr, exclude_tags


@dataclass(frozen=True)
class TailParse:
    """The structured result of parsing one prompt tail.

    Splitting the flags out into three explicit collections
    (``values`` / ``booleans`` / ``missing_values``) is what keeps a
    bare ``--fix`` from being confused with ``--fix=<something>``, and
    what lets a consumer report a value flag that never got a value
    rather than silently dropping it.
    """

    positionals: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    booleans: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing_values: tuple[str, ...] = ()

    def note(self) -> str:
        """Render the parse problems as a sentence for the body, or ``""``."""
        parts: list[str] = []
        if self.missing_values:
            names = ", ".join(f"--{k}" for k in self.missing_values)
            verb = "needs" if len(self.missing_values) == 1 else "need"
            parts.append(f"{names} {verb} a value; none was supplied.")
        if self.unknown:
            names = ", ".join(f"--{k}" for k in sorted(self.unknown))
            parts.append(f"Unrecognized flag(s) ignored: {names}.")
        return " ".join(parts)


def _split_tail(
    tail: str,
    *,
    value_flags: frozenset[str] = frozenset(),
    multi_word_flags: frozenset[str] = frozenset(),
    known_flags: frozenset[str] | None = None,
) -> TailParse:
    """Split a prompt tail into positionals, flag values, and bare flags.

    Every prompt takes a single ``str`` that consumes the whole slash
    tail, because hosts that bind slashes to MCP prompts pre-tokenize
    on whitespace and bind tokens positionally to declared parameters
    -- a typed ``bool`` / ``int`` / ``list[str]`` in position two or
    later receives a bare word and fails JSON decode. One ``str`` slot
    has no such problem.

    ``value_flags`` names the flags that consume the following token.
    Flags outside that set are booleans. A value may also be attached
    with ``--flag=value`` regardless, which keeps
    ``--scraper-timeout=600`` unambiguous next to a bare ``--force``.

    ``multi_word_flags`` names the subset whose value is free text
    (``--title "Pydantic basics"``): those consume every token up to the
    next ``--flag`` and join them with single spaces. The scalar value
    flags stay single-token on purpose -- ``--jobs 8 pydantic`` means
    ``jobs=8`` and a positional ``pydantic``, and a rule that swallowed
    until the next flag would eat the collection name.

    A value flag whose value is missing -- because it ended the tail,
    or because the next token is itself a flag -- is recorded in
    ``missing_values`` rather than swallowing the neighbouring flag as
    its value. That distinction is the whole reason
    ``--jobs --force`` reports a problem instead of quietly setting
    ``--jobs=--force``.

    ``known_flags`` is the prompt's full flag vocabulary; anything
    outside it lands in ``unknown`` so the body can name the typo.
    """
    tokens = tail.split()
    positionals: list[str] = []
    values: dict[str, str] = {}
    booleans: set[str] = set()
    unknown: set[str] = set()
    missing_values: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, sep, val = tok[2:].partition("=")
            if sep:
                values[key] = val
            elif key in multi_word_flags:
                taken: list[str] = []
                while i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    taken.append(tokens[i + 1])
                    i += 1
                if taken:
                    values[key] = " ".join(taken)
                else:
                    missing_values.append(key)
                    booleans.add(key)
            elif key in value_flags:
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                if nxt is not None and not nxt.startswith("--"):
                    values[key] = nxt
                    i += 1
                else:
                    missing_values.append(key)
                    booleans.add(key)
            else:
                booleans.add(key)
            if known_flags is not None and key not in known_flags:
                unknown.add(key)
        else:
            positionals.append(tok)
        i += 1
    return TailParse(
        positionals=tuple(positionals),
        values=values,
        booleans=frozenset(booleans),
        unknown=frozenset(unknown),
        missing_values=tuple(missing_values),
    )


def ask_prompt(question: str) -> list[Message]:
    """Synthesized cited answer to a question.

    The tail is consumed as a single positional string; ``+tag`` /
    ``-exclude`` filter markers are parsed out of the question text
    inside the body. The routed ``mcp__lies__search`` /
    ``mcp__lies__lib_ask`` calls carry the extracted filters.
    """
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    if not query_text:
        return [
            Message(
                "No question given — only filter tokens arrived. Ask the "
                "user what they want answered, then re-dispatch "
                'get_prompt(name="ask", arguments={"question": '
                '"<question>"}).'
            )
        ]
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

    Same filter-parsing contract as ``ask_prompt``. ``--top_k`` is
    parsed out of the tail before the filter pass and clamped to
    [1, 10]; both ``--top_k=5`` and ``--top_k 5`` bind, so the value
    never leaks into the query text.
    """
    parsed = _split_tail(
        tail,
        value_flags=frozenset({"top_k"}),
        known_flags=frozenset({"top_k"}),
    )
    if not parsed.positionals and not parsed.values:
        return [
            Message(
                "No question given. Ask the user what to ground, then "
                're-dispatch get_prompt(name="ground", '
                'arguments={"tail": "<question>"}). '
                f"{parsed.note()}".strip()
            )
        ]
    top_k = 3
    raw_top_k = parsed.values.get("top_k")
    if raw_top_k is not None:
        try:
            top_k = max(1, min(10, int(raw_top_k)))
        except ValueError:
            top_k = 3
    question = " ".join(parsed.positionals)
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    note = parsed.note()
    body = (
        f"Call mcp__lies__search({query_text!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "\u2264200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask \u2014 ground is digest-only. " + note
    )
    return [Message(body)]


def collections_prompt(tail: str) -> list[Message]:
    """Library collection registry CRUD.

    The tail is whitespace-separated, so ``<args…>`` binds one token per
    argument. An argument that needs to contain a space cannot be
    expressed through this prompt; the body says so and points at a
    direct ``Bash`` call, where the agent controls its own quoting.
    """
    parsed = _split_tail(tail)
    sub = parsed.positionals[0].lower() if parsed.positionals else ""
    args = parsed.positionals[1:]
    valid = "list, add, remove, modify, info, tag, register-shipped, where"
    # shlex.join quotes every argument that needs it, so a token
    # containing shell metacharacters survives the Bash() interpolation
    # as one argument instead of being re-split by the shell.
    joined = shlex.join(args)
    unquoted = ""
    if any("'" in a or '"' in a for a in args):
        unquoted = (
            f" Args are whitespace-separated and quotes are literal here, so "
            f"{args!r} is {len(args)} argument(s) with the quote characters "
            f"kept verbatim. Re-issue without quotes, or run the Bash command "
            f"directly with your own quoting."
        )

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
        body = (
            "Register a new collection: run "
            "Bash(lies library new <name> <path> [--tags <t1,t2>] "
            "[--scope <keywords>] [--synonyms <file>] [--scraper <cmd>]) "
            f"with args {joined!r}. Then run qmd embed so vec/hyde queries "
            f"find the new collection.{unquoted}"
        )
    elif sub in ("remove", "modify"):
        body = (
            f"Run Bash(lies library {sub} {joined}) and report the tool's "
            f"outcome to the user verbatim.{unquoted}"
        )
    elif sub == "tag":
        body = (
            f"Run Bash(lies library enrich-tags {joined}) and report which "
            f"collections now carry the tag.{unquoted}"
        )
    elif sub in ("register-shipped", "where"):
        body = (
            f"Run Bash(lies library {sub} {joined}) and surface the stdout "
            f"stream to the user.{unquoted}"
        )
    elif not sub:
        body = f"No subcommand given. Valid subcommands: {valid}. Ask the user which to invoke."
    else:
        body = (
            f"Unknown subcommand {sub!r}. Valid subcommands: {valid}. Ask the user which to invoke."
        )
    return [Message(body)]


def ingest_prompt(tail: str) -> list[Message]:
    """Route a source into the library."""
    value_flags = frozenset({"type", "slug", "title", "batch", "slug-prefix", "delete"})
    parsed = _split_tail(
        tail,
        value_flags=value_flags,
        # --title and --slug-prefix carry free text; everything else is a
        # scalar (a type name, a slug, a path) and takes one token.
        multi_word_flags=frozenset({"title", "slug-prefix"}),
        known_flags=value_flags | {"dry-run"},
    )
    note = parsed.note()
    if parsed.missing_values:
        return [
            Message(
                f"Cannot run ingest: {note} Ask the user for the "
                "missing value and re-dispatch. No command was run."
            )
        ]
    dry = " --dry-run" if "dry-run" in parsed.booleans else ""
    delete_slug = parsed.values.get("delete")
    batch_dir = parsed.values.get("batch")
    source = parsed.positionals[0] if parsed.positionals else "<source>"

    if delete_slug:
        cmd = f'lies ingest --data-dir "$LIES_DATA"{dry} --delete {shlex.quote(delete_slug)}'
        body = (
            f"Run Bash({cmd!r}) and surface stdout/stderr. The CLI "
            "removes the page file, the catalog row, appends a "
            f"delete entry to the log, and triggers qmd update. {note}"
        )
    elif batch_dir:
        prefix = parsed.values.get("slug-prefix") or "<derive-from-user>"
        body = (
            f"Run Bash(lies ingest --batch {shlex.quote(batch_dir)}{dry} "
            f'--data-dir "$LIES_DATA" --slug-prefix {prefix} '
            f"--force) \u2014 slug-prefix is required. Surface stdout/stderr. {note}"
        )
    else:
        page_type = parsed.values.get("type") or "<entity|concept|synthesis|...>"
        extra = ""
        if parsed.values.get("slug"):
            extra += f" --slug {shlex.quote(parsed.values['slug'])}"
        if parsed.values.get("title"):
            extra += f" --title {shlex.quote(parsed.values['title'])}"
        body = (
            f'Run Bash(lies ingest --data-dir "$LIES_DATA"{dry} '
            f"--source {shlex.quote(source)} --type {page_type}{extra}) \u2014 supervised mode "
            f"requires --type. Surface stdout/stderr. {note}"
        )
    return [Message(body)]


def lint_prompt(tail: str) -> list[Message]:
    """Health-check the corpus."""
    parsed = _split_tail(
        tail,
        value_flags=frozenset({"check"}),
        known_flags=frozenset({"check", "fix"}),
    )
    note = parsed.note()
    if parsed.missing_values:
        return [
            Message(
                f"Cannot run lint: {note} Ask the user which check to "
                "run and re-dispatch. No lint ran."
            )
        ]
    check = parsed.values.get("check")
    fix = "fix" in parsed.booleans
    body = (
        f"Call mcp__lies__lint(name=None, check={check!r}, "
        f"fix={fix!r}) and surface the returned report. "
        + (
            "When fix=True, narrate any repair outcomes the tool "
            "applied and re-run lint to confirm clean state. "
            if fix
            else ""
        )
        + note
    )
    return [Message(body)]


def reindex_prompt(tail: str) -> list[Message]:
    """Rebuild the search index."""
    flags_known = frozenset({"reconcile", "embed", "force", "cleanup", "all", "name"})
    parsed = _split_tail(
        tail,
        value_flags=frozenset({"name"}),
        multi_word_flags=frozenset({"name"}),
        known_flags=flags_known,
    )
    reconcile = "reconcile" in parsed.booleans
    embed = "embed" in parsed.booleans
    force = "force" in parsed.booleans
    cleanup = "cleanup" in parsed.booleans
    # ``all`` is the destructive full-reindex marker. It arrives as
    # ``--all`` from a flag-style tail and as a bare ``all`` or ``all_``
    # from the pre-single-tail positional convention this prompt used
    # to declare; all three mean the same thing, and treating the
    # positional as a no-op would silently downgrade a destructive
    # rebuild the user asked for.
    all_ = "all" in parsed.booleans or any(
        p.rstrip("_").lower() == "all" for p in parsed.positionals
    )
    name = parsed.values.get("name")
    if parsed.missing_values:
        return [
            Message(
                f"Cannot run reindex: {parsed.note()} Ask the user for the "
                "missing value and re-dispatch. No reindex ran."
            )
        ]
    destructive = cleanup or all_
    body = (
        f"Call mcp__lies__reindex(reconcile={reconcile}, embed={embed}, "
        f"force={force}, cleanup={cleanup}, all_={all_}, "
        f"name={name!r}) and surface the returned ReindexResult envelope. "
        + (
            "cleanup/all_ are destructive \u2014 wait for the host's "
            "elicit-confirmation step before re-dispatching. "
            if destructive
            else ""
        )
        + parsed.note()
    )
    return [Message(body)]


def sync_prompt(tail: str) -> list[Message]:
    """Pull + ingest remote sources."""
    value_flags = frozenset({"jobs", "scraper-timeout"})
    parsed = _split_tail(
        tail,
        value_flags=value_flags,
        known_flags=value_flags | {"no-ingest", "force", "dry-run"},
    )
    note = parsed.note()
    if parsed.missing_values:
        return [
            Message(
                f"Cannot run sync: {note} Ask the user for the missing "
                "value and re-dispatch. No sync ran."
            )
        ]
    # ``all`` is a no-op marker meaning "every collection with a
    # scraper", which is also the CLI's behaviour with no --only.
    names = [p for p in parsed.positionals if p.lower() != "all"]
    out: list[str] = []
    if names:
        out.append(f"--only {shlex.join(names)}")
    for flag, label in (
        ("no-ingest", "--no-ingest"),
        ("force", "--force"),
        ("dry-run", "--dry-run"),
    ):
        if flag in parsed.booleans:
            out.append(label)
    jobs = parsed.values.get("jobs")
    if jobs is not None and jobs != "4":
        out.append(f"--jobs {shlex.quote(jobs)}")
    timeout = parsed.values.get("scraper-timeout")
    if timeout is not None and timeout != "300":
        out.append(f"--scraper-timeout {shlex.quote(timeout)}")

    flag_str = (" " + " ".join(out)) if out else ""
    body = (
        f'Run Bash(lies sync --data-dir "$LIES_DATA"{flag_str}) and '
        "surface each collection's scrape/ingest status. "
        "Phase 3 (qmd reconcile + update + embed + cleanup) runs "
        f"automatically unless --dry-run or --no-ingest is set. {note}"
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
