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


def _is_tag_atom(token: str) -> bool:
    """Whether a ``+``/``-`` token carries a plausible tag, not prose.

    The sigil-letter rule in ``_FILTER_TOKEN_RE`` is necessary and not
    sufficient. This library indexes command-line tooling, so a
    question about it is full of single-letter option references
    (``the -e flag of grep``); a bare letter is far more likely to be
    an option name than a tag. A real tag either carries a qualifier
    (``c:opencode``, ``t:draft``) or spells out at least two
    characters, which no single-letter option reference does.
    """
    atom = token[1:]
    return ":" in atom or len(atom) >= 2


def _parse_question_filters(
    question: str,
) -> tuple[str, str | None, list[str] | None]:
    """Strip leading ``+tag`` / ``-tag`` filter tokens out of question text.

    The slash-UX convention (mirroring the ask plugin and the spec's
    ``"[+tag-expr] [-tag ...] <question>"`` argument hint) is a
    *leading* filter run followed by the question. Two guards keep
    ordinary English out of the filter path:

    1. **Position.** A token is a filter only until the first word of
       the question. ``the -e flag of grep`` keeps every token; the
       filter scan stopped at ``the``.
    2. **Shape.** ``_is_tag_atom`` rejects single-letter atoms and
       anything without the sigil-letter shape, so ``-1``, ``--``,
       and ``-v`` stay text.

    Without (1) a question *about* option flags gets its own words
    deleted and re-injected as search filters, with no note.

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
    past_filters = False
    for token in question.split():
        if past_filters or not _FILTER_TOKEN_RE.match(token) or not _is_tag_atom(token):
            past_filters = True
            text_tokens.append(token)
        elif token[0] == "+":
            plus_atoms.append(token[1:])
        else:
            minus_tags.append(token[1:])

    tag_expr = "|".join(plus_atoms) if plus_atoms else None
    exclude_tags = minus_tags if minus_tags else None
    query_text = " ".join(text_tokens)
    return query_text, tag_expr, exclude_tags


def _verbatim(label: str, value: str) -> str:
    """Render ``label`` as a fenced block for the calling agent to pass through.

    The tail's author is the same principal as the agent reading the
    body, so this is an un-escaping aid rather than a trust boundary.
    It still removes a real class of defect: a question containing a
    quote or a newline rendered through ``repr()`` arrives at the tool
    call carrying ``\\"`` and ``\\n`` the agent has to know to strip,
    and a tail containing a closing bracket can end the rendered call
    and append instructions of its own. A fenced block with an
    explicit instruction is unambiguous in both cases.
    """
    return f"{label} (pass this string verbatim, do not re-quote):\n```\n{value}\n```"


@dataclass(frozen=True)
class TailParse:
    """The structured result of parsing one prompt tail.

    Splitting the flags out into explicit collections (``values`` /
    ``booleans`` / ``missing_values`` / ``ignored_values``) is what
    keeps a bare ``--fix`` from being confused with
    ``--fix=<something>``, and what lets a consumer report a value flag
    that never got a value rather than silently dropping it.

    ``ignored_values`` holds ``(flag, discarded)`` pairs: a boolean
    flag written ``--all=true``. The flag is *on* -- the user asked
    for it -- and the value means nothing, so it is reported rather
    than dropped without word. Routing the ``=`` form on a boolean
    into ``values`` instead (the pre-0.42.1 behavior) meant no prompt
    body, all of which read ``booleans``, ever saw the flag: the
    silent loss of a flag the user typed.
    """

    positionals: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    booleans: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing_values: tuple[str, ...] = ()
    ignored_values: tuple[tuple[str, str], ...] = ()

    def flag_on(self, name: str) -> bool:
        """Whether ``--name`` was present in any form the parser accepts.

        The one place a body asks "was this switch set?". Reading
        ``booleans`` directly is correct only for a flag declared as a
        boolean; this helper also answers for a value flag that got a
        value, so a body never silently reads the wrong collection.
        """
        return name in self.booleans or name in self.values

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
        if self.ignored_values:
            names = ", ".join(f"--{k}={v!r}" for k, v in self.ignored_values)
            parts.append(f"{names} takes no value; treated as set. Drop the '=' suffix.")
        return " ".join(parts)


def _split_tail(
    tail: str,
    *,
    value_flags: frozenset[str] = frozenset(),
    multi_word_flags: frozenset[str] = frozenset(),
    known_flags: frozenset[str] | None = None,
) -> TailParse:
    """Split a prompt tail into positionals, flag values, and bare flags.

    Every prompt takes a single ``str``, because hosts that bind
    slashes to MCP prompts pre-tokenize the tail on whitespace and
    bind tokens positionally to declared parameters -- a typed
    ``bool`` / ``int`` / ``list[str]`` in position two or later
    receives a bare word and fails JSON decode. One ``str`` slot has
    no such problem.

    That makes the slash path *safe*, not *complete*: the host binds
    exactly one token and drops the rest, so a multi-token tail
    arrives whole only through ``get_prompt``, whose arguments are not
    pre-tokenized. This grammar is the ``get_prompt`` grammar; the
    slash form exercises its single-token subset.

    ``value_flags`` names the flags that consume the following token.
    Flags outside that set are booleans. A value may also be attached
    with ``--flag=value`` on a *value* flag, which keeps
    ``--scraper-timeout=600`` unambiguous next to a bare ``--force``.
    The same ``=`` form on a *boolean* sets the flag and records the
    discarded value in ``ignored_values`` -- it does not smuggle the
    value into ``values`` where no body would read it.

    ``multi_word_flags`` names the subset whose value is free text
    (``--title Pydantic basics``): those consume every token up to the
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
    ignored_values: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, sep, val = tok[2:].partition("=")
            takes_value = key in value_flags or key in multi_word_flags
            if sep:
                if takes_value:
                    values[key] = val
                else:
                    booleans.add(key)
                    ignored_values.append((key, val))
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
        ignored_values=tuple(ignored_values),
    )


def ask_prompt(question: str) -> list[Message]:
    """Synthesized cited answer to a question.

    The tail arrives as a single string; ``+tag`` / ``-tag`` filter
    markers are parsed out of its leading run inside the body. The
    routed ``mcp__lies__search`` / ``mcp__lies__lib_ask`` calls carry
    the extracted filters.
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
    query_block = _verbatim("question", query_text)
    body = (
        f"Call mcp__lies__search with {query_block}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r} to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Then call mcp__lies__lib_ask with the same {query_block}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r} for "
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

    The emptiness check runs *after* the filter pass, not before it.
    A filter token is itself a positional, so a guard on
    ``positionals`` let ``+c:opencode`` -- and a bare ``--top_k 5`` --
    through and rendered ``search('')``.
    """
    parsed = _split_tail(
        tail,
        value_flags=frozenset({"top_k"}),
        known_flags=frozenset({"top_k"}),
    )
    top_k = 3
    raw_top_k = parsed.values.get("top_k")
    if raw_top_k is not None:
        try:
            top_k = max(1, min(10, int(raw_top_k)))
        except ValueError:
            top_k = 3
    question = " ".join(parsed.positionals)
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    if not query_text:
        return [
            Message(
                "No question given. Ask the user what to ground, then "
                're-dispatch get_prompt(name="ground", '
                'arguments={"tail": "<question>"}). '
                f"{parsed.note()}".strip()
            )
        ]
    note = parsed.note()
    query_block = _verbatim("question", query_text)
    body = (
        f"Call mcp__lies__search with {query_block}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r} to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "\u2264200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask \u2014 ground is digest-only. " + note
    )
    return [Message(body)]


# Subcommand aliases the prompt accepts, mapped to the verb
# ``lies library`` actually implements. The CLI ships list / show /
# where / new / modify / delete / enrich-tags / bootstrap-all; these
# spellings are the ones users reach for.
_LIBRARY_SUB_ALIASES = {
    "add": "new",
    "remove": "delete",
    "info": "show",
    "register-shipped": "bootstrap-all",
}
_LIBRARY_SUBS = (
    "list, show (info), new (add), modify, delete (remove), "
    "where, enrich-tags, tag, bootstrap-all (register-shipped)"
)
# The flag vocabulary of the verbs above, straight from
# ``src/lies/library/collections_cli.py``. Declaring it here is what
# lets ``--tags`` and friends reach the command line instead of
# falling through as unquoted positionals.
_LIBRARY_VALUE_FLAGS = frozenset({"source", "prompt", "tag", "untag", "set", "from-file", "json"})
# ``--set`` and ``--from-file`` take free text; the rest are scalars.
_LIBRARY_MULTI_WORD_FLAGS = frozenset({"set", "from-file"})


def collections_prompt(tail: str) -> list[Message]:
    """Library collection registry CRUD.

    The tail is whitespace-separated, so ``<args…>`` binds one token per
    argument. An argument that needs to contain a space cannot be
    expressed through this prompt; the body says so and points at a
    direct ``Bash`` call, where the agent controls its own quoting.

    A positional the verb has no slot for is reported, never appended
    to the rendered command. The alternative -- splicing surplus
    positionals into the command line -- is how a flag the parser did
    not recognise (``--tags foo,bar``) turned into a bare argument the
    user never asked to pass, on a verb that then ran with it.
    """
    parsed = _split_tail(
        tail,
        value_flags=_LIBRARY_VALUE_FLAGS,
        multi_word_flags=_LIBRARY_MULTI_WORD_FLAGS,
        known_flags=_LIBRARY_VALUE_FLAGS,
    )
    note = parsed.note()
    raw_sub = parsed.positionals[0].lower() if parsed.positionals else ""
    args = parsed.positionals[1:]
    sub = _LIBRARY_SUB_ALIASES.get(raw_sub, raw_sub)

    def leftover(used: int) -> str:
        """Name the positionals the verb had no slot for."""
        extra = args[used:]
        if not extra:
            return ""
        return (
            f" Not consumed by {raw_sub!r}: {shlex.join(extra)} "
            f"({len(extra)} positional(s) the verb does not take). "
            f"Ask the user what to do with them instead of passing them along."
        )

    unquoted = ""
    if any("'" in a or '"' in a for a in args):
        unquoted = (
            f" Args are whitespace-separated and quotes are literal here, so "
            f"{args!r} is {len(args)} argument(s) with the quote characters "
            f"kept verbatim. Re-issue without quotes, or run the Bash command "
            f"directly with your own quoting."
        )

    def flags(include: tuple[str, ...]) -> str:
        out = []
        for key in include:
            if parsed.values.get(key) is not None:
                out.append(f"--{key} {shlex.quote(parsed.values[key])}")
            elif key in parsed.booleans:
                out.append(f"--{key}")
        return (" " + " ".join(out)) if out else ""

    if sub == "list":
        body = (
            'Call mcp__lies__collections_read(subcommand="list") and '
            "render each entry as one markdown bullet (name, tags, "
            "source, page_count, updated_at)."
        )
        if parsed.flag_on("json"):
            body = (
                "Run Bash(lies library list --json) and render the parsed "
                "array as one markdown bullet per record. " + note
            )
    elif sub == "show":
        name = args[0] if args else "<name>"
        body = (
            f'Call mcp__lies__collections_read(subcommand="info", '
            f"name={shlex.quote(name)}) and render the returned metadata "
            f"envelope." + leftover(1) + note
        )
    elif sub == "new":
        slug = args[0] if args else "<slug>"
        # A second positional is the source path the pre-flag form
        # carried; the flag form overrides it.
        source_flag = flags(("source", "prompt"))
        if not source_flag and len(args) > 1:
            source_flag = f" --source {shlex.quote(args[1])}"
        body = (
            f"Register a new collection: run "
            f"Bash(lies library new {shlex.quote(slug)}{source_flag}). "
            f"Then run qmd embed so vec/hyde queries find the new "
            f"collection." + leftover(2) + unquoted + note
        )
    elif sub == "modify":
        slug = args[0] if args else "<slug>"
        body = (
            f"Run Bash(lies library modify {shlex.quote(slug)}"
            f"{flags(('tag', 'untag', 'set', 'from-file'))}) and report "
            f"the tool's outcome to the user verbatim." + leftover(1) + unquoted + note
        )
    elif sub == "tag":
        # ``tag <slug> <t1> <t2>`` is the shape users reach for;
        # ``lies library`` spells it ``modify --tag``.
        slug = args[0] if args else "<slug>"
        tags = args[1:]
        rendered = "".join(f" --tag {shlex.quote(t)}" for t in tags)
        body = (
            f"Run Bash(lies library modify {shlex.quote(slug)}"
            f"{flags(('tag', 'untag', 'set', 'from-file'))}{rendered}) "
            f"and report which tags the collection now carries."
            + leftover(1 + len(tags))
            + unquoted
            + note
        )
    elif sub == "delete":
        slug = args[0] if args else "<slug>"
        body = (
            f"Run Bash(lies library delete {shlex.quote(slug)}) and report "
            f"the tool's outcome to the user verbatim." + leftover(1) + unquoted + note
        )
    elif sub == "enrich-tags":
        body = (
            "Run Bash(lies library enrich-tags) and surface the printed "
            "hints verbatim." + leftover(0) + note
        )
    elif sub == "bootstrap-all":
        body = (
            "Run Bash(lies library bootstrap-all) and surface the stdout "
            "stream to the user." + leftover(0) + note
        )
    elif sub == "where":
        slug = args[0] if args else "<slug>"
        body = (
            f"Run Bash(lies library where {shlex.quote(slug)}) and surface "
            f"the stdout stream to the user." + leftover(1) + unquoted + note
        )
    elif not sub:
        body = f"No subcommand given. Valid subcommands: {_LIBRARY_SUBS}. Ask the user which to invoke."
    else:
        body = (
            f"Unknown subcommand {raw_sub!r}. Valid subcommands: "
            f"{_LIBRARY_SUBS}. Ask the user which to invoke."
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
    dry = " --dry-run" if parsed.flag_on("dry-run") else ""
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
        prefix = shlex.quote(parsed.values.get("slug-prefix") or "<derive-from-user>")
        body = (
            f"Run Bash(lies ingest --batch {shlex.quote(batch_dir)}{dry} "
            f'--data-dir "$LIES_DATA" --slug-prefix {prefix} '
            f"--force) \u2014 slug-prefix is required. Surface stdout/stderr. {note}"
        )
    else:
        page_type = shlex.quote(parsed.values.get("type") or "<entity|concept|synthesis|...>")
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
    fix = parsed.flag_on("fix")
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
        # ``name`` is a wiki name -- a single token. Listing it as
        # multi-word made ``--name pydantic all`` bind
        # ``name='pydantic all'``, swallowing the destructive marker
        # into the name and dropping the confirmation the user needed.
        known_flags=flags_known,
    )
    reconcile = parsed.flag_on("reconcile")
    embed = parsed.flag_on("embed")
    force = parsed.flag_on("force")
    cleanup = parsed.flag_on("cleanup")
    # ``all`` is the destructive full-reindex marker. It arrives as
    # ``--all`` from a flag-style tail and as a bare ``all`` or ``all_``
    # from the pre-single-tail positional convention this prompt used
    # to declare; all three mean the same thing, and treating the
    # positional as a no-op would silently downgrade a destructive
    # rebuild the user asked for.
    all_ = parsed.flag_on("all") or any(p.rstrip("_").lower() == "all" for p in parsed.positionals)
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
    value_flags = frozenset({"jobs", "scraper-timeout", "only"})
    parsed = _split_tail(
        tail,
        value_flags=value_flags,
        # ``--only`` takes a comma list and is the flag spelling of the
        # bare collection names. Without it in the vocabulary, a typed
        # ``--only pydantic`` was reported as an unrecognized flag the
        # body had just rendered into its own command.
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
    only = parsed.values.get("only")
    if only:
        names.extend(n for n in only.split(",") if n)
    out: list[str] = []
    if names:
        out.append(f"--only {shlex.join(names)}")
    for flag, label in (
        ("no-ingest", "--no-ingest"),
        ("force", "--force"),
        ("dry-run", "--dry-run"),
    ):
        if parsed.flag_on(flag):
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

    Every prompt takes exactly one ``str``. Hosts that bind slashes to
    MCP prompts pre-tokenize the tail on whitespace and bind tokens
    positionally to declared parameters, so a typed parameter in
    position two or later receives a bare word and fails JSON decode.
    One string slot has no such problem, and the flags are parsed
    inside each body.

    One string slot makes the slash path *safe*, not *complete*: the
    host binds exactly one token and drops the rest. A multi-token
    tail reaches a prompt whole only through ``get_prompt``, whose
    arguments are not pre-tokenized -- that is the path
    ``instructions.md`` routes user questions down, and the reason
    ``_split_tail`` carries a grammar the slash form never exercises
    in full.
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
