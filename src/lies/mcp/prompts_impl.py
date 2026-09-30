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
    into ``values`` instead (the pre-0.42.0 behavior) meant no prompt
    body, all of which read ``booleans``, ever saw the flag: the
    silent loss of a flag the user typed.

    ``repeats`` holds *every* occurrence of a value flag, in order.
    ``values`` keeps the last one, which is the right answer for a
    scalar and a silent loss for a repeatable one -- ``modify --tag a
    --tag b`` is a real shape (``--tag`` is declared "Tag to add
    (repeatable)" by ``lies library modify``), and a dict of last
    wins dropped the first tag with no word. Bodies that render a
    repeatable flag read ``repeats_of``; bodies that render a scalar
    read ``values``.
    """

    positionals: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    repeats: dict[str, tuple[str, ...]] = field(default_factory=dict)
    booleans: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing_values: tuple[str, ...] = ()
    ignored_values: tuple[tuple[str, str], ...] = ()

    def repeats_of(self, name: str) -> tuple[str, ...]:
        """Every value ``name`` was given, in order (empty if never)."""
        return self.repeats.get(name, ())

    def flag_on(self, name: str) -> bool:
        """Whether ``--name`` was present in any form the parser accepts.

        The one place a body asks "was this switch set?". Reading
        ``booleans`` directly is correct only for a flag declared as a
        boolean; this helper also answers for a value flag that got a
        value, so a body never silently reads the wrong collection.
        """
        return name in self.booleans or name in self.values

    def note(self) -> str:
        """Render the parse problems as a sentence for the body.

        Returns ``""`` or a leading-space-prefixed sentence, so a body
        that appends it to a rendered command cannot weld the last word
        of that command to the first word of the note.
        """
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
        return f" {' '.join(parts)}" if parts else ""


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
    Every occurrence of a value flag is recorded in ``repeats``;
    ``values`` holds the last, which is right for a scalar and a
    silent loss for a repeatable one.
    """
    tokens = tail.split()
    positionals: list[str] = []
    values: dict[str, str] = {}
    repeats: dict[str, list[str]] = {}
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
                    repeats.setdefault(key, []).append(val)
                else:
                    booleans.add(key)
                    ignored_values.append((key, val))
            elif key in multi_word_flags:
                taken: list[str] = []
                while i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    taken.append(tokens[i + 1])
                    i += 1
                if taken:
                    joined = " ".join(taken)
                    values[key] = joined
                    repeats.setdefault(key, []).append(joined)
                else:
                    missing_values.append(key)
                    booleans.add(key)
            elif key in value_flags:
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                if nxt is not None and not nxt.startswith("--"):
                    values[key] = nxt
                    repeats.setdefault(key, []).append(nxt)
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
        repeats={k: tuple(v) for k, v in repeats.items()},
        booleans=frozenset(booleans),
        unknown=frozenset(unknown),
        missing_values=tuple(missing_values),
        ignored_values=tuple(ignored_values),
    )


def _split_leading_flags(
    tail: str,
    *,
    value_flags: frozenset[str],
    known_flags: frozenset[str],
) -> tuple[TailParse, str]:
    """Consume only the *leading* run of ``--flags``; the rest is question text.

    A tail that is mostly a question must not have its words parsed as
    flags. This library indexes command-line tooling, so "what is the
    ``--only`` flag" is an ordinary question and its ``--only`` is
    ordinary text; running the full ``_split_tail`` grammar over the
    whole string deleted the words and searched for the remainder
    (``"what is the flag"``). Two guards close that:

    1. **Leading run only.** Scanning stops at the first token that is
       not a flag, so a flag-shaped word inside a sentence is never
       reached.
    2. **``--`` terminator.** Everything after a bare ``--`` is text,
       so a user whose question *starts* with a flag can say so.

    Returns ``(parse_of_the_leading_flags, question_text)``.
    """
    tokens = tail.split()
    head: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if not (tok.startswith("--") and len(tok) > 2):
            break
        head.append(tok)
        i += 1
        key, sep, _ = tok[2:].partition("=")
        if not sep and key in value_flags and i < len(tokens) and not tokens[i].startswith("--"):
            head.append(tokens[i])
            i += 1
    parsed = _split_tail(" ".join(head), value_flags=value_flags, known_flags=known_flags)
    return parsed, " ".join(tokens[i:])


def _leftover_note(raw_args: tuple[str, ...], used: int, verb: str) -> str:
    """Name the positionals ``verb`` had no slot for.

    Splicing surplus positionals into a command line is how a flag the
    parser did not recognise turned into a bare argument the user never
    asked to pass, on a verb that then ran with it. Reporting is the
    alternative: the agent asks, instead of running something nobody
    typed.
    """
    extra = raw_args[used:]
    if not extra:
        return ""
    return (
        f" Not consumed by {verb!r}: {shlex.join(extra)} "
        f"({len(extra)} positional(s) the verb does not take). "
        f"Ask the user what to do with them instead of passing them along."
    )


def _render_flags(parsed: TailParse, include: tuple[str, ...]) -> str:
    """Render every occurrence of each requested flag, in ``include`` order.

    A value flag is read through ``repeats_of``, so ``modify --tag a
    --tag b`` renders both — a repeatable Typer option means one
    occurrence per value. A boolean renders bare, once, from
    ``booleans``. A key with both a value and a bare occurrence (a
    value flag that ran out of input) renders its value; the missing
    value is named in :meth:`TailParse.note` rather than papered over
    with a second spelling.
    """
    out: list[str] = []
    for key in include:
        values = parsed.repeats_of(key)
        if values:
            out.extend(f"--{key} {shlex.quote(v)}" for v in values)
        elif key in parsed.booleans:
            out.append(f"--{key}")
    return (" " + " ".join(out)) if out else ""


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

    Same filter-parsing contract as ``ask_prompt``, and the same
    reason it is parsed differently: the tail is a *question*, and a
    question about command-line tooling is full of option flags. Only
    the leading run of ``--flags`` is parsed
    (:func:`_split_leading_flags`), so ``what is the --only flag``
    grounds the words ``--only`` and ``flag`` rather than deleting
    them. ``--top_k`` is clamped to [1, 10]; both ``--top_k=5`` and
    ``--top_k 5`` bind, so the value never leaks into the query text.

    The emptiness check runs *after* the filter pass, not before it.
    A filter token is itself a positional, so a guard on
    ``positionals`` let ``+c:opencode`` -- and a bare ``--top_k 5`` --
    through and rendered ``search('')``.
    """
    parsed, question = _split_leading_flags(
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
    # A flag-shaped word inside the question is text, and it stays
    # there. Say so, because the other reading -- "the prompt ignored
    # my --top_k" -- is the one a user forms when the words they typed
    # come back inside a search string.
    if any(tok.startswith("--") for tok in question.split()):
        note += (
            " Flags are read from the leading run only, so a --flag after"
            " the first word stays part of the question; put --top_k first,"
            " or open the tail with -- to say the flags stop there."
        )
    query_block = _verbatim("question", query_text)
    body = (
        f"Call mcp__lies__search with {query_block}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r} to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "\u2264200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask \u2014 ground is digest-only." + note
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
# Per-verb flag vocabulary, transcribed from the Typer signatures in
# ``src/lies/library/collections_cli.py``: (value flags, boolean
# flags). Per-verb rather than one union set, because the two errors
# are symmetric and both are silent. Too narrow and a real flag
# (``new --tag cli``) is reported as a typo with its value falling
# into a positional; too wide and a flag the verb does not have is
# parsed as a boolean, reported as "on", and never rendered. Matching
# the signature exactly is the only setting where neither happens --
# ``tests/unit/mcp/test_rendered_commands_are_runnable.py`` checks
# every rendered command against the live Typer app, so the table
# cannot drift again without a test going red.
_LIBRARY_VERB_FLAGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "list": (frozenset(), frozenset({"json"})),
    "show": (frozenset(), frozenset()),
    "where": (frozenset(), frozenset()),
    "new": (frozenset({"source", "prompt", "tag"}), frozenset()),
    "modify": (frozenset({"set", "tag", "untag", "from-file"}), frozenset()),
    "tag": (frozenset({"set", "tag", "untag", "from-file"}), frozenset()),
    "delete": (frozenset(), frozenset({"force"})),
    "enrich-tags": (frozenset(), frozenset()),
    "bootstrap-all": (frozenset(), frozenset({"json"})),
}
_LIBRARY_ANY_VALUE = frozenset().union(*(v for v, _ in _LIBRARY_VERB_FLAGS.values()))
_LIBRARY_ANY_BOOL = frozenset().union(*(b for _, b in _LIBRARY_VERB_FLAGS.values()))
_LIBRARY_ANY_KNOWN = _LIBRARY_ANY_VALUE | _LIBRARY_ANY_BOOL
# ``--force`` on ``delete`` skips an interactive confirmation, so a
# body that renders it has to say what it is skipping.
_LIBRARY_FORCE_WARNING = " --force skips the delete confirmation; say so before running it."


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

    Parsed twice: once against the union of every verb's flags to find
    the subcommand, then again against that verb's own flags to render
    it. The second parse is authoritative -- its ``unknown`` set is
    what names a flag the verb has no slot for.
    """
    first = _split_tail(
        tail,
        value_flags=_LIBRARY_ANY_VALUE,
        known_flags=_LIBRARY_ANY_KNOWN,
    )
    raw_sub = first.positionals[0].lower() if first.positionals else ""
    sub = _LIBRARY_SUB_ALIASES.get(raw_sub, raw_sub)
    if not sub:
        return [
            Message(
                f"No subcommand given. Valid subcommands: {_LIBRARY_SUBS}. "
                "Ask the user which to invoke."
            )
        ]
    verb_value, verb_bool = _LIBRARY_VERB_FLAGS.get(sub, (frozenset(), frozenset()))
    parsed = _split_tail(
        tail,
        value_flags=verb_value,
        known_flags=verb_value | verb_bool,
    )
    note = parsed.note()
    args = parsed.positionals[1:]

    unquoted = ""
    if any("'" in a or '"' in a for a in args):
        unquoted = (
            f" Args are whitespace-separated and quotes are literal here, so "
            f"{args!r} is {len(args)} argument(s) with the quote characters "
            f"kept verbatim. Re-issue without quotes, or run the Bash command "
            f"directly with your own quoting."
        )

    if sub == "list":
        if parsed.flag_on("json"):
            body = (
                "Run Bash(lies library list --json) and render the parsed "
                "array as one markdown bullet per record. " + note
            )
        else:
            body = (
                'Call mcp__lies__collections_read(subcommand="list") and '
                "render each entry as one markdown bullet (name, tags, "
                "source, page_count, updated_at). " + note
            )
    elif sub == "show":
        name = args[0] if args else "<name>"
        body = (
            f'Call mcp__lies__collections_read(subcommand="info", '
            f"name={shlex.quote(name)}) and render the returned metadata "
            f"envelope." + _leftover_note(args, 1, raw_sub) + unquoted + note
        )
    elif sub == "new":
        slug = args[0] if args else "<slug>"
        # A second positional is the source path the pre-flag form
        # carried; the flag form overrides it.
        source_flag = _render_flags(parsed, ("source", "prompt"))
        if not source_flag and len(args) > 1:
            source_flag = f" --source {shlex.quote(args[1])}"
        body = (
            f"Register a new collection: run "
            f"Bash(lies library new {shlex.quote(slug)}"
            f"{source_flag}{_render_flags(parsed, ('tag',))}). "
            f"Then run qmd embed so vec/hyde queries find the new "
            f"collection."
            + _leftover_note(args, 2 if source_flag else 1, raw_sub)
            + unquoted
            + note
        )
    elif sub == "modify":
        slug = args[0] if args else "<slug>"
        body = (
            f"Run Bash(lies library modify {shlex.quote(slug)}"
            f"{_render_flags(parsed, ('tag', 'untag', 'set', 'from-file'))}) "
            f"and report the tool's outcome to the user verbatim."
            + _leftover_note(args, 1, raw_sub)
            + unquoted
            + note
        )
    elif sub == "tag":
        # ``tag <slug> <t1> <t2>`` is the shape users reach for;
        # ``lies library`` spells it ``modify --tag``.
        slug = args[0] if args else "<slug>"
        tags = args[1:]
        rendered = "".join(f" --tag {shlex.quote(t)}" for t in tags)
        body = (
            f"Run Bash(lies library modify {shlex.quote(slug)}"
            f"{rendered}{_render_flags(parsed, ('untag', 'set', 'from-file'))}) "
            f"and report which tags the collection now carries."
            + _leftover_note(args, 1 + len(tags), raw_sub)
            + unquoted
            + note
        )
    elif sub == "delete":
        slug = args[0] if args else "<slug>"
        force = " --force" if parsed.flag_on("force") else ""
        body = (
            f"Run Bash(lies library delete {shlex.quote(slug)}{force}) and "
            f"report the tool's outcome to the user verbatim."
            + (_LIBRARY_FORCE_WARNING if force else "")
            + _leftover_note(args, 1, raw_sub)
            + unquoted
            + note
        )
    elif sub == "enrich-tags":
        body = (
            "Run Bash(lies library enrich-tags) and surface the printed "
            "hints verbatim." + _leftover_note(args, 0, raw_sub) + note
        )
    elif sub == "bootstrap-all":
        json_flag = " --json" if parsed.flag_on("json") else ""
        body = (
            f"Run Bash(lies library bootstrap-all{json_flag}) and surface the "
            f"stdout stream to the user." + _leftover_note(args, 0, raw_sub) + note
        )
    elif sub == "where":
        slug = args[0] if args else "<slug>"
        body = (
            f"Run Bash(lies library where {shlex.quote(slug)}) and surface "
            f"the stdout stream to the user." + _leftover_note(args, 1, raw_sub) + unquoted + note
        )
    else:
        body = (
            f"Unknown subcommand {raw_sub!r}. Valid subcommands: "
            f"{_LIBRARY_SUBS}. Ask the user which to invoke."
        )
    return [Message(body)]


# ``lies ingest`` -- transcribed from the Typer signature in
# ``src/lies/library/cli.py``. There is no positional argument and no
# ``--type``: the ingest path is deterministic and never asks a model
# what kind of page a file is. A bare path in the tail is treated as
# ``--source``, which is the shape users reach for.
_INGEST_VALUE_FLAGS = frozenset(
    {
        "source",
        "batch",
        "slug-prefix",
        "collection",
        "slug",
        "title",
        "exclude-stem",
        "exclude-dir",
    }
)
# ``--title`` is free text; the rest are single-token paths, slugs, and
# collection names, and a rule that swallowed until the next flag would
# eat the collection name after ``--slug-prefix``.
_INGEST_MULTI_WORD_FLAGS = frozenset({"title"})
_INGEST_BOOL_FLAGS = frozenset({"force", "no-force", "dry-run", "no-dry-run"})
_INGEST_KNOWN_FLAGS = _INGEST_VALUE_FLAGS | _INGEST_BOOL_FLAGS
# The old prompt advertised ``--delete <slug>`` on a command that has
# no such option. Nothing in the CLI removes an ingested page, so the
# body says what does exist instead of rendering a command that exits 2.
_INGEST_DELETE_REMEDY = (
    "Deleting an ingested page has no CLI verb: `lies ingest` takes "
    "--source or --batch and nothing else. `lies library delete "
    "<collection>` removes a collection's config.yaml only. Removing a "
    "page file is a filesystem delete, so ask the user which they mean "
    "before running anything."
)


def ingest_prompt(tail: str) -> list[Message]:
    """Route a source into the library."""
    parsed = _split_tail(
        tail,
        value_flags=_INGEST_VALUE_FLAGS,
        multi_word_flags=_INGEST_MULTI_WORD_FLAGS,
        known_flags=_INGEST_KNOWN_FLAGS,
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
    force = " --force" if parsed.flag_on("force") else ""
    excludes = _render_flags(parsed, ("exclude-stem", "exclude-dir"))
    batch_dir = parsed.values.get("batch")
    source = parsed.values.get("source")
    positional = parsed.positionals[0] if parsed.positionals else None
    delete_asked = "delete" in tail

    if positional is not None:
        if source is not None:
            return [
                Message(
                    f"Cannot run ingest: {positional!r} and --source "
                    f"{source!r} both name a source, and `lies ingest` takes "
                    "one. Ask the user which to use; no command was run."
                )
            ]
        source = positional

    if batch_dir is not None:
        if source is not None:
            return [
                Message(
                    "Cannot run ingest: --batch and a single source both given. "
                    "`lies ingest` runs one mode at a time. Ask the user which "
                    "they want; no command was run."
                )
            ]
        prefix = parsed.values.get("slug-prefix")
        prefix_flag = f" --slug-prefix {shlex.quote(prefix)}" if prefix else ""
        body = (
            f"Run Bash(lies ingest --batch {shlex.quote(batch_dir)}"
            f"{prefix_flag}{excludes}{force}{dry}) — batch mode. Surface "
            f"stdout/stderr." + _leftover_note(parsed.positionals, 1, "ingest") + note
        )
    elif source is not None:
        extra = _render_flags(parsed, ("collection", "slug", "title"))
        body = (
            f"Run Bash(lies ingest --source {shlex.quote(source)}"
            f"{extra}{excludes}{force}{dry}) and surface stdout/stderr."
            + _leftover_note(parsed.positionals, 1, "ingest")
            + note
        )
    else:
        return [
            Message(
                "Cannot run ingest: no source given. Ask the user which file "
                "or URL to ingest, or pass --batch <dir>, then re-dispatch "
                'get_prompt(name="ingest", arguments={"tail": "<source>"}).'
            )
        ]
    if delete_asked:
        body += " " + _INGEST_DELETE_REMEDY
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


# ``lies sync`` -- transcribed from the Typer signature in
# ``src/lies/cli/ingestion.py``. It takes ONE positional collection and
# has no ``--only``, ``--jobs``, ``--scraper-timeout``, ``--no-ingest``,
# ``--dry-run``, or ``--data-dir``; the old prompt invented all of them
# and rendered a command that exited 2 on every invocation.
_SYNC_VALUE_FLAGS = frozenset({"source", "name"})
_SYNC_BOOL_FLAGS = frozenset(
    {
        "force",
        "no-force",
        "wait",
        "no-wait",
        "fail-busy",
        "no-fail-busy",
        "wizard",
        "skip-reindex",
    }
)
_SYNC_KNOWN_FLAGS = _SYNC_VALUE_FLAGS | _SYNC_BOOL_FLAGS
_SYNC_TAIL = (
    " Phase 3 (qmd update + embed) runs after the sync loop unless "
    "--skip-reindex is set. `--wizard` routes a missing collection "
    "through the collection_author_agent and needs a TTY."
)


def sync_prompt(tail: str) -> list[Message]:
    """Pull + ingest remote sources."""
    parsed = _split_tail(
        tail,
        value_flags=_SYNC_VALUE_FLAGS,
        known_flags=_SYNC_KNOWN_FLAGS,
    )
    note = parsed.note()
    if parsed.missing_values:
        return [
            Message(
                f"Cannot run sync: {note} Ask the user for the missing "
                "value and re-dispatch. No sync ran."
            )
        ]
    # ``all`` is a no-op marker meaning "every collection", which is
    # also the CLI's behaviour with no positional. Several names mean
    # several commands: the CLI takes one collection per invocation,
    # so rendering one call per name is how the user's request maps
    # onto the real surface. Splicing them into a single list-valued
    # flag was how an unrecognized flag's value became a collection
    # the user never named.
    names = [p for p in parsed.positionals if p.lower() != "all"]
    shared = _render_flags(
        parsed,
        (
            "source",
            "name",
            "force",
            "no-force",
            "wait",
            "no-wait",
            "fail-busy",
            "no-fail-busy",
            "wizard",
            "skip-reindex",
        ),
    )
    if names:
        # Every named collection is consumed — one command each — so
        # there is no leftover to report here. Reporting one anyway
        # contradicted the commands rendered a sentence earlier.
        commands = "; ".join(f"Run Bash(lies sync {shlex.quote(n)}{shared})" for n in names)
        rendered = (
            f"{commands}. One invocation per collection, because `lies sync` "
            f"takes a single positional. Surface each run's scrape/ingest status."
        )
    else:
        rendered = (
            f"Run Bash(lies sync{shared}) and surface each collection's scrape/ingest status."
        )
    return [Message(rendered + _SYNC_TAIL + note)]


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
