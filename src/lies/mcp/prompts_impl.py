"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

import json
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
# Positions of the non-whitespace runs, so the question text can be
# sliced out of the original string with its internal newlines intact.
_TOKEN_RE = re.compile(r"\S+")
_BACKTICK_RUN_RE = re.compile(r"`+")


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

    **The question text itself is returned verbatim, newlines and
    all.** It used to be rebuilt as ``" ".join(text_tokens)``, which
    collapsed every internal run of whitespace: a pasted code block
    or a stack trace arrived at the search as one run-on line, and
    the agent reading the body saw a mangled question. Only the
    leading filter run is tokenized; the rest is sliced out of the
    original string by offset. (qmd is whitespace-insensitive, so
    retrieval was never affected -- but the agent reads prose, and
    the value it was told to pass verbatim was not what the user
    typed.)

    Returns ``(query_text, tag_expr, exclude_tags)``.
    """
    plus_atoms: list[str] = []
    minus_tags: list[str] = []
    rest_start: int | None = None
    for match in _TOKEN_RE.finditer(question):
        token = match.group()
        if rest_start is not None:
            continue
        if _FILTER_TOKEN_RE.match(token) and _is_tag_atom(token):
            (plus_atoms if token[0] == "+" else minus_tags).append(token[1:])
        else:
            rest_start = match.start()
    tag_expr = "|".join(plus_atoms) if plus_atoms else None
    exclude_tags = minus_tags if minus_tags else None
    query_text = "" if rest_start is None else question[rest_start:].strip()
    return query_text, tag_expr, exclude_tags


def _verbatim(label: str, value: str) -> str:
    """Render ``label`` as a fenced block for the calling agent to pass through.

    The tail's author is the same principal as the agent reading the
    body, so this is an un-escaping aid rather than a trust boundary.
    It still removes a real class of defect: a question containing a
    quote rendered through ``repr()`` arrives at the tool call
    carrying ``\\"`` the agent has to know to strip, and a tail
    containing a closing bracket can end the rendered call and append
    instructions of its own. A fenced block with an explicit
    instruction is unambiguous in both cases.

    The fence is **wider than the longest backtick run in the value**,
    per CommonMark's rule for a code span. A fixed three-backtick
    fence is breakable: a value containing ``` ``` ``` on its own line
    closes the block and appends its own lines as prose to the body.
    The question text now keeps its newlines (see
    :func:`_parse_question_filters`), so a value *can* contain a
    line-initial fence — which is exactly why the widening is here
    rather than relying on the value never carrying one.
    """
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{label} (pass this string verbatim, do not re-quote):\n{fence}\n{value}\n{fence}"


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

    ``repurposed`` holds ``(flag, value)`` pairs for a flag the caller
    did not declare as a value flag whose *following* token then fell
    through to ``positionals``. ``collections show --tag cli`` is the
    shape: the second parse knows ``show`` has no ``--tag``, records
    the flag as unknown, and the value ``cli`` becomes a positional
    that a body would hand to ``collections_read(name=…)``. Without
    this field the note said "ignored", the agent read that as "I
    dropped your flag", and the flag's own value became the collection
    name. A body that consumes positionals checks this and asks.
    """

    positionals: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    _repeats: dict[str, tuple[str, ...]] = field(default_factory=dict)
    booleans: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing_values: tuple[str, ...] = ()
    ignored_values: tuple[tuple[str, str], ...] = ()
    repurposed: tuple[tuple[str, str], ...] = ()

    def repeats_of(self, name: str) -> tuple[str, ...]:
        """Every value ``name`` was given, in order (empty if never).

        The only read path for the repeat table. The backing field is
        underscore-private because ``values`` is last-wins: a body that
        reached for the raw field on a *repeatable* flag would read
        nothing, and the flag the user typed twice would render once
        with no word.
        """
        return self._repeats.get(name, ())

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

        Every sentence here is compatible with a rendered command
        following it: each describes a flag that was dropped or
        mistyped, and dropping a flag leaves the command valid. The
        two conditions that make a command *invalid* -- a value flag
        that got no value, and a flag whose value would be re-read as
        a positional -- are refusals, not notes, and
        :func:`_refuse_unless_clean` returns them in place of a body.
        Nothing appended here ever says "no command was run": four
        bodies append this note to a live ``Bash(...)`` and the
        sentence told the agent the opposite of what the rest of the
        same paragraph said.
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
    outside it lands in ``unknown`` so the body can name the typo. An
    outside flag followed by a bare word also records the pair in
    ``repurposed``: the word is a positional the user did not type as
    one, and a body that consumes positionals must ask before passing
    it along.
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
    repurposed: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            # POSIX end-of-flags. A user who types it means "stop
            # reading flags", and rendering the token itself hands
            # Click a collection literally named ``--``; Click strips
            # a bare ``--`` before the command function sees it, so
            # ``lies sync -- pydantic`` ran with ``collection=None``
            # -- a scrape and reindex of *every* registered
            # collection. Consume the marker and push every
            # following token to ``positionals`` unparsed, which is
            # what :func:`_split_leading_flags` already did inside
            # this same module.
            positionals.extend(tokens[i + 1 :])
            break
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
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                if nxt is not None and not nxt.startswith("--") and key not in (known_flags or ()):
                    # An unrecognized flag followed by a bare word. The
                    # word is not the flag's value -- it is a positional
                    # the user never named as one, and a body that
                    # consumes positionals would pass it to the command.
                    # Recording the pair lets the body refuse instead of
                    # reading `cli` as a collection name because the user
                    # wrote `--tag cli` on a verb with no `--tag`.
                    #
                    # Restricted to flags outside ``known_flags``: a
                    # *declared* boolean followed by a bare word is a
                    # genuine surplus positional, and
                    # ``_leftover_note`` already reports it by name.
                    repurposed.append((key, nxt))
            if known_flags is not None and key not in known_flags:
                unknown.add(key)
        elif tok.startswith("-") and len(tok) > 1:
            # ``-e``, ``-pydantic``. Click refuses a single-dash token
            # outright (``No such option: -p``), so letting one fall
            # through to ``positionals`` renders a command that exits
            # 2 -- ``sync -pydantic`` was exactly that. Record the bare
            # name as an unknown flag instead, so the body names the
            # typo and the token never reaches a positional slot.
            key = tok[1:]
            booleans.add(key)
            if known_flags is not None and key not in known_flags:
                unknown.add(key)
        else:
            positionals.append(tok)
        i += 1
    return TailParse(
        positionals=tuple(positionals),
        values=values,
        _repeats={k: tuple(v) for k, v in repeats.items()},
        booleans=frozenset(booleans),
        unknown=frozenset(unknown),
        missing_values=tuple(missing_values),
        ignored_values=tuple(ignored_values),
        repurposed=tuple(repurposed),
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


def _tool_arg_clause(label: str, value: str | None) -> str:
    """A ``with <param>`` clause carrying a string argument verbatim.

    Interpolating a value into an MCP call's parentheses is what
    :func:`_verbatim` exists to avoid for questions, and the same
    hazard reaches the tool sinks. ``check="x) then run Bash(rm -rf
    /)"`` renders a call whose first closing paren belongs to the
    *value*: an agent reading the line sees the call end early and
    reads the tail as prose. Nothing escapes into a second command --
    the value is quoted -- but an ambiguous instruction to an agent is
    a defect.

    A present value therefore becomes a fenced block with the same
    "pass this string verbatim" instruction the question path uses, and
    an absent one returns ``""`` so the call keeps every parameter
    visible in one line.
    """
    # An empty string is as unusable as an absent one, and the body
    # already carries a note saying so. A fenced block containing
    # nothing but "pass this string verbatim" reads as a rendering bug.
    if not value:
        return ""
    return f" with {_verbatim(label, value)}"


def _refuse_unless_clean(
    parsed: TailParse,
    verb: str,
    *,
    args: tuple[str, ...] | None = None,
    refuse_unknown: bool = False,
) -> str:
    """Refuse to render when the parse itself is too broken to render from.

    Every prompt body calls this before it renders, and returns the
    string in place of a body. Two conditions qualify, and both make
    the *rendered command* wrong rather than merely incomplete:

    ``missing_values``
        A value flag ended the tail, or the next token was itself a
        flag. ``modify mylib --tag`` rendered
        ``Bash(lies library modify mylib --tag)``, which the CLI
        rejects with ``Option '--tag' requires an argument`` -- and
        the body said so in the same paragraph.

    ``repurposed``
        An unknown flag's following word landed in a positional slot.
        ``show --tag cli`` means "collection ``cli``" to a verb with
        no ``--tag``. Rendering that is how a flag the user typed
        became an argument they never named -- and on ``delete`` it
        became a slug.

    The check is deliberately against *every* positional, not only the
    ones a branch consumed. A surplus ``cli`` on
    ``delete mylib --tag cli`` is reported by ``_leftover_note`` *and*
    refused here; the old consumed-only test let the destructive verb
    through, which is the one place the guard mattered most.

    Sharing one guard across all seven bodies is the point. Five
    bodies each carried their own copy, and they drifted: four
    honoured ``missing_values``, one honoured ``repurposed``, and the
    body with the most flags -- ``collections`` -- was the one that
    shipped three exit-2 renders. A defect class closed once per body
    is not closed.
    """
    if parsed.missing_values:
        names = ", ".join(f"--{k}" for k in parsed.missing_values)
        verb_word = "needs" if len(parsed.missing_values) == 1 else "need"
        return (
            f"Cannot run {verb}: {names} {verb_word} a value; none was "
            f"supplied. Ask the user for the missing value and re-dispatch. "
            f"No command was run."
        )
    pool = parsed.positionals if args is None else args
    hits = [(k, v) for k, v in parsed.repurposed if v in pool]
    if hits:
        pairs = ", ".join(f"{v!r} after --{k}" for k, v in hits)
        return (
            f"Cannot run {verb}: {pairs} — that flag takes no value here, so "
            f"the word after it would have been read as an argument. Ask the "
            f"user which they meant, the flag or the argument, and re-dispatch. "
            f"No command was run."
        )
    if refuse_unknown and parsed.unknown:
        names = ", ".join(f"--{k}" for k in sorted(parsed.unknown))
        return (
            f"Cannot run {verb}: {names} is not an option this command "
            f"declares. An unrecognized flag can swallow the collection "
            f"name the user typed, and with no name left the command would "
            f"sync *every* registered collection — a scrape and reindex of "
            f"the whole library from what reads as a typo. Ask the user "
            f"which collection they meant, re-dispatch with the name alone, "
            f"and show them the supported options. No command was run."
        )
    return ""


def _quoted_value_note(parsed: TailParse, args: tuple[str, ...], verb: str) -> str:
    """Name the values that carry literal quote characters.

    The tail is split on whitespace only -- a shell lexer would raise
    on ordinary English -- so ``--tag 'cli'`` and ``--title "Pydantic
    basics"`` keep their quotes, and ``shlex.quote`` faithfully passes
    the quote characters *into* the value. ``modify mylib --tag 'cli'``
    tags the collection ``'cli'``; ``ingest --title "Pydantic basics"``
    titles the page ``"Pydantic basics"``. Both are silent corruption
    of the value rather than a refusal.

    This inspects every positional *and* every value flag, because
    quoting a flag's value is the more common typo. ``--tag="a b"``
    additionally splits at the first ``=`` and leaks ``b"`` into a
    positional, which ``_leftover_note`` reports on its own.
    """
    quoted = [a for a in args if "'" in a or '"' in a]
    quoted += [f"--{k}={v!r}" for k, v in sorted(parsed.values.items()) if "'" in v or '"' in v]
    if not quoted:
        return ""
    return (
        f" {quoted!r} — args are whitespace-separated and quotes are literal "
        f"here, so the quote characters are part of the value "
        f"{verb} will store. Re-issue without quotes, or run the command "
        f"directly with your own quoting."
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


def ask_prompt(tail: str) -> list[Message]:
    """Synthesized cited answer to a question.

    The tail arrives as a single string; ``+tag`` / ``-tag`` filter
    markers are parsed out of its leading run inside the body. The
    routed ``mcp__lies__search`` / ``mcp__lies__lib_ask`` calls carry
    the extracted filters.

    The parameter is named ``tail``, like all six siblings, and that
    name is load-bearing. The pre-0.42.0 signature was
    ``(question, tag_expr, exclude_tags)``; keeping ``question`` made
    this the one prompt where an old call did not fail. The other six
    raise ``Missing required arguments: {'tail'}`` on the old shape.
    Here, with ``question`` still declared, the old
    ``{"question": ..., "tag_expr": ...}`` rendered a body that
    searched with ``tag_expr=None`` and **silently dropped the
    filter** -- a query scoped by a tag quietly answering from the
    whole library, with nothing in the response to say so. Naming the
    parameter ``tail`` makes the retired call fail loudly, in the same
    shape as the other six. The filters move into the tail as
    ``+tag`` / ``-tag`` tokens, which is where a host's slash
    tokenizer can carry them anyway.
    """
    query_text, tag_expr, exclude_tags = _parse_question_filters(tail)
    if not query_text:
        return [
            Message(
                "No question given — only filter tokens arrived. Ask the "
                "user what they want answered, then re-dispatch "
                'get_prompt(name="ask", arguments={"tail": '
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
    bad_top_k = ""
    raw_top_k = parsed.values.get("top_k")
    if raw_top_k is not None:
        # Every other malformed tail in this module gets a named note.
        # A silently-swallowed --top_k reads to the user as "you asked
        # for 5 and got 3" with no cause attached.
        try:
            requested = int(raw_top_k)
        except ValueError:
            bad_top_k = f" --top_k={raw_top_k!r} is not an integer; using the default 3."
        else:
            top_k = max(1, min(10, requested))
            if requested != top_k:
                bad_top_k = f" --top_k={requested} is outside [1, 10]; clamped to {top_k}."
    query_text, tag_expr, exclude_tags = _parse_question_filters(question)
    refusal = _refuse_unless_clean(parsed, "ground")
    if refusal:
        return [Message(refusal)]
    if not query_text:
        return [
            Message(
                "No question given. Ask the user what to ground, then "
                're-dispatch get_prompt(name="ground", '
                f'arguments={{"tail": "<question>"}}).{bad_top_k}{parsed.note()}'.strip()
            )
        ]
    note = parsed.note() + bad_top_k
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


def _needs_name_note(raw_sub: str, what: str) -> str:
    """Refuse to render a command whose required positional is missing.

    The alternative was to render the placeholder itself --
    ``Bash(lies library where '<slug>')`` -- which exits 2 on the angle
    brackets and reads to the agent as a real argument. Asking is the
    only shape that leaves the user with a runnable next step.
    """
    return (
        f"Cannot run {raw_sub!r}: no collection {what} was given. "
        f"Ask the user which collection they mean, then re-dispatch "
        f'get_prompt(name="collections", arguments={{"tail": "{raw_sub} '
        f'<name>"}}). No command was run.'
    )


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

    A verb that requires a collection name refuses to render at all
    when the name is missing, rather than rendering the literal
    ``'<slug>'`` placeholder into a command that exits 2.

    Parsed twice: once against the union of every verb's flags to find
    the subcommand, then again against that verb's own flags to render
    it. The second parse is authoritative -- its ``unknown`` and
    ``repurposed`` sets are what name a flag the verb has no slot for,
    and refuse when that flag's value would otherwise be read as a
    positional the user did not type.
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

    # Shared with every other body. `args` excludes the subcommand, so a
    # repurposed value that landed in the subcommand slot is not
    # double-counted; `collections_prompt` reaches this before any
    # branch, which is why the verb with the most flags no longer
    # needs its own copy of the rule.
    refusal = _refuse_unless_clean(parsed, f"library {raw_sub}", args=args)
    if refusal:
        return [Message(refusal)]

    unquoted = _quoted_value_note(parsed, args, "the command")

    # ``used`` counts the positionals this branch consumed; every branch
    # sets it, and both the surplus note and the repurposed check read
    # it rather than restating a number per branch.
    if sub == "list":
        if parsed.flag_on("json"):
            body = (
                "Run Bash(lies library list --json) and render the parsed "
                "array as one markdown bullet per record."
                + _leftover_note(args, 0, raw_sub)
                + unquoted
                + note
            )
        else:
            body = (
                'Call mcp__lies__collections_read(subcommand="list") and '
                "render each entry as one markdown bullet (name, tags, "
                "source, page_count, updated_at)."
                + _leftover_note(args, 0, raw_sub)
                + unquoted
                + note
            )
    elif sub == "show":
        if not args:
            return [Message(_needs_name_note(raw_sub, "name"))]
        # An MCP tool argument is not a shell word. `shlex.quote` here
        # rendered the six-character string `'"'"'` for the slug `a'b`,
        # so an agent copying the body looked up a collection named
        # `'a'"'"'b'`. JSON is the correct spelling at a tool sink.
        body = (
            f'Call mcp__lies__collections_read(subcommand="info", '
            f"name={json.dumps(args[0])}) and render the returned metadata "
            f"envelope." + _leftover_note(args, 1, raw_sub) + unquoted + note
        )
    elif sub == "new":
        if not args:
            return [Message(_needs_name_note(raw_sub, "slug"))]
        used = 1
        slug = args[0]
        # A second positional is the source path the pre-flag form
        # carried; the flag form overrides it.
        source_flag = _render_flags(parsed, ("source", "prompt"))
        if not source_flag and len(args) > 1:
            source_flag = f" --source {shlex.quote(args[1])}"
            used = 2
        # `new_cmd` raises `BadParameter("library new requires
        # --source")` in its body -- a runtime check, not a Typer
        # parse, so no signature-derived guard can see it. `--prompt`
        # does not satisfy it: the check runs before `prompt` is read.
        # Rendering without a source produced `Bash(lies library new
        # mylib)`, which exits 2 on the *default* invocation of the verb.
        if "source" not in parsed.values and len(args) <= 1:
            return [
                Message(
                    f"Cannot run 'new': no source was given. "
                    f"`lies library new` requires --source <url-or-path> "
                    f"(wizard mode's --prompt does not substitute for it). "
                    f"Ask the user which source the collection should read, "
                    f'then re-dispatch get_prompt(name="collections", '
                    f'arguments={{"tail": "new {slug} --source <path>"}}). '
                    f"No command was run."
                )
            ]
        # A second positional IS a source the verb accepts, so the
        # generic leftover note ("the verb does not take it") names the
        # wrong cause. What happened is that a --source/--prompt flag
        # took precedence, and the user should be told their source was
        # dropped rather than merely unconsumed.
        dropped_source = ""
        if len(args) > 1 and used == 1:
            dropped_source = (
                f" {args[1]!r} was the source you gave and the flag form won, "
                f"so it is NOT in the command above. Ask the user whether to "
                f"re-issue with --source {shlex.quote(args[1])} instead."
            )
        body = (
            f"Register a new collection: run "
            f"Bash(lies library new {shlex.quote(slug)}"
            f"{source_flag}{_render_flags(parsed, ('tag',))}). "
            f"Then run qmd embed so vec/hyde queries find the new "
            f"collection." + dropped_source + _leftover_note(args, used, raw_sub) + unquoted + note
        )
    elif sub == "modify":
        if not args:
            return [Message(_needs_name_note(raw_sub, "slug"))]
        body = (
            f"Run Bash(lies library modify {shlex.quote(args[0])}"
            f"{_render_flags(parsed, ('tag', 'untag', 'set', 'from-file'))}) "
            f"and report the tool's outcome to the user verbatim."
            + _leftover_note(args, 1, raw_sub)
            + unquoted
            + note
        )
    elif sub == "tag":
        # ``tag <slug> <t1> <t2>`` is the shape users reach for;
        # ``lies library`` spells it ``modify --tag``. Both spellings
        # render, so the flag form carries ``--tag`` here too -- reading
        # tags only from the positionals dropped ``tag mylib --tag docs``
        # on the floor, and the rendered command ran with none.
        if not args:
            return [Message(_needs_name_note(raw_sub, "slug"))]
        tags = args[1:]
        used = 1 + len(tags)
        rendered = "".join(f" --tag {shlex.quote(t)}" for t in tags)
        body = (
            f"Run Bash(lies library modify {shlex.quote(args[0])}"
            f"{rendered}"
            f"{_render_flags(parsed, ('tag', 'untag', 'set', 'from-file'))}) "
            f"and report which tags the collection now carries."
            + _leftover_note(args, used, raw_sub)
            + unquoted
            + note
        )
    elif sub == "delete":
        if not args:
            return [Message(_needs_name_note(raw_sub, "slug"))]
        force = " --force" if parsed.flag_on("force") else ""
        body = (
            f"Run Bash(lies library delete {shlex.quote(args[0])}{force}) and "
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
        if not args:
            return [Message(_needs_name_note(raw_sub, "slug"))]
        body = (
            f"Run Bash(lies library where {shlex.quote(args[0])}) and surface "
            f"the stdout stream to the user." + _leftover_note(args, 1, raw_sub) + unquoted + note
        )
    else:
        return [
            Message(
                f"Unknown subcommand {raw_sub!r}. Valid subcommands: "
                f"{_LIBRARY_SUBS}. Ask the user which to invoke."
            )
        ]
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
# ``--title`` is the one free-text flag, so it eats every word up to the
# next ``--flag``. A source typed after it is consumed as part of the
# title, and the user is then told they gave no source -- the diagnosis
# points away from the cause. The note rides along on every render path
# so the ordering rule is stated where the mistake is made.
_INGEST_TITLE_NOTE = (
    " Note: `--title` takes every following word up to the next flag as "
    "its value, so put the source first or attach it with `--source "
    "<path>`; a source typed after `--title` is read as part of the title."
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
    if "title" in parsed.values:
        note += _INGEST_TITLE_NOTE
    # A token test, not a substring test. ``"delete" in tail`` fired on
    # ``/home/me/delete-stuff/x.md`` and on ``deleted.md``, appending
    # the deletion lecture to bodies that had nothing to do with it.
    delete_asked = any(t.lstrip("-").lower() in ("delete", "remove", "rm") for t in tail.split())

    def finish(text: str) -> list[Message]:
        """Every exit, so the delete remedy rides on refusals too.

        ``ingest --delete entity-x`` hits the repurposed guard before
        anything renders, and the useful answer to "delete this page"
        is the one that says what *can* delete it. Appending only on
        the render path meant the one case that most needed the
        sentence was the one that never got it.
        """
        return [Message(text + " " + _INGEST_DELETE_REMEDY if delete_asked else text)]

    refusal = _refuse_unless_clean(parsed, "ingest")
    if refusal:
        return finish(refusal)
    # ``--force/--no-force`` and ``--dry-run/--no-dry-run`` are single
    # Click options carrying both spellings, and the negated form is
    # the CLI's own default -- so reading only the positive form made
    # ``--no-force`` a silent no-op: the flag parsed, the body never
    # mentioned it, and the command ran with ``force=True``'s
    # opposite while the user believed they had said something.
    dry = " --dry-run" if parsed.flag_on("dry-run") else ""
    force = " --force" if parsed.flag_on("force") else ""
    if "no-dry-run" in parsed.booleans:
        dry = " --no-dry-run"
    if "no-force" in parsed.booleans:
        force = " --no-force"
    excludes = _render_flags(parsed, ("exclude-stem", "exclude-dir"))
    batch_dir = parsed.values.get("batch")
    source = parsed.values.get("source")
    positional = parsed.positionals[0] if parsed.positionals else None

    if positional is not None:
        if source is not None:
            return finish(
                f"Cannot run ingest: {positional!r} and --source "
                f"{source!r} both name a source, and `lies ingest` takes "
                "one. Ask the user which to use; no command was run."
            )
        source = positional

    if batch_dir is not None:
        if source is not None:
            return finish(
                "Cannot run ingest: --batch and a single source both given. "
                "`lies ingest` runs one mode at a time. Ask the user which "
                "they want; no command was run."
            )
        prefix = parsed.values.get("slug-prefix")
        prefix_flag = f" --slug-prefix {shlex.quote(prefix)}" if prefix else ""
        body = (
            f"Run Bash(lies ingest --batch {shlex.quote(batch_dir)}"
            f"{prefix_flag}{excludes}{force}{dry}) — batch mode. Surface "
            f"stdout/stderr."
            + _leftover_note(parsed.positionals, 1, "ingest")
            + _quoted_value_note(parsed, parsed.positionals, "ingest")
            + note
        )
    elif source is not None:
        # `ingest` derives a collection name from `--collection`,
        # `--slug-prefix`, or a *batch* parent directory -- never from
        # a single source. So `lies ingest --source /tmp/a.md` exits 2
        # with "no collection name could be derived from the source",
        # and that bare-path form is the one `instructions.md`
        # documents. A runtime `typer.Exit`, invisible to any
        # signature-derived guard, so the body refuses here.
        if not (parsed.values.get("collection") or parsed.values.get("slug-prefix")):
            return finish(
                f"Cannot run ingest: --source {source!r} was given with no "
                f"collection name. `lies ingest` derives one from "
                f"--collection, --slug-prefix, or a batch parent directory, "
                f"and a single source names none of those, so the command "
                f"would exit 2. Ask the user which collection this source "
                f'belongs to, then re-dispatch get_prompt(name="ingest", '
                f'arguments={{"tail": "{source} --collection <name>"}}). '
                f"No command was run."
            )
        extra = _render_flags(parsed, ("collection", "slug", "title"))
        body = (
            f"Run Bash(lies ingest --source {shlex.quote(source)}"
            f"{extra}{excludes}{force}{dry}) and surface stdout/stderr."
            + _leftover_note(parsed.positionals, 1, "ingest")
            + _quoted_value_note(parsed, parsed.positionals, "ingest")
            + note
        )
    else:
        return finish(
            "Cannot run ingest: no source given."
            + note
            + " Ask the user which file or URL to ingest, or pass "
            '--batch <dir>, then re-dispatch get_prompt(name="ingest", '
            'arguments={"tail": "<source>"}).'
        )
    return finish(body)


def lint_prompt(tail: str) -> list[Message]:
    """Health-check the corpus.

    The vocabulary is the tool's own signature: ``lint`` takes
    ``name``, ``check``, ``fix``, and ``force_repair``. ``name`` and
    ``force_repair`` were unreachable before -- the body hard-coded
    ``name=None`` and never read the fourth parameter, so a user
    scoping a lint to one wiki or asking for the flock reaped got
    neither, and the second reading was silent.

    A bare positional binds ``check``, which is what the pre-0.42.0
    signature declared first (``def _lint_prompt(check=None, ...)``).
    ``lint orphan`` is the natural migration off that shape and it used
    to vanish with no word; the sibling ``reindex`` and ``sync``
    bodies already honour their own bare-``all`` legacy spelling, so
    ``lint`` was the odd one out.
    """
    parsed = _split_tail(
        tail,
        value_flags=frozenset({"check", "name"}),
        known_flags=frozenset({"check", "name", "fix", "force-repair"}),
    )
    note = parsed.note()
    refusal = _refuse_unless_clean(parsed, "lint")
    if refusal:
        return [Message(refusal)]
    check = parsed.values.get("check")
    if check is None and parsed.positionals:
        check = parsed.positionals[0]
    name = parsed.values.get("name")
    fix = parsed.flag_on("fix")
    force_repair = parsed.flag_on("force-repair")
    if "check" in parsed.values and not parsed.values["check"].strip():
        # `--check=` binds the empty string, and
        # ``orchestrator.run_lint`` reads a blank check as *no
        # filter* -- so the user got the whole report for a filter
        # they asked for, with nothing to say so. ``ground`` already
        # names the bad value for a malformed ``--top_k``; this is the
        # same defect one parameter over.
        note += (
            f" --check={parsed.values['check']!r} is empty, and the tool treats "
            f"a blank check as no filter at all, so this would return the "
            f"full report. Re-issue with a category name."
        )
    body = (
        f"Call mcp__lies__lint(name={name!r}, "
        f"fix={fix!r}, force_repair={force_repair!r})"
        f"{_tool_arg_clause('check', check)} and surface the returned report."
        + (
            " When fix=True, narrate any repair outcomes the tool "
            "applied and re-run lint to confirm clean state."
            if fix
            else ""
        )
        + (
            " force_repair reaps the cross-process memory flock and retries "
            "once before surfacing an error; say so before setting it."
            if force_repair
            else ""
        )
        + _leftover_note(parsed.positionals, 1, "lint")
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
    refusal = _refuse_unless_clean(parsed, "reindex")
    if refusal:
        return [Message(refusal)]
    # `all` / `all_` are the legacy bare-word spelling and are
    # consumed; everything else is a positional the tool has no slot
    # for. This was the only body of seven with no surplus report, and
    # it cost the most: `--name` is the only way to scope a reindex to
    # a wiki, so `reindex pydantic` reindexed the *default* wiki and
    # said nothing about the word it dropped.
    legacy = sum(1 for p in parsed.positionals if p.rstrip("_").lower() == "all")
    destructive = cleanup or all_
    body = (
        f"Call mcp__lies__reindex(reconcile={reconcile}, embed={embed}, "
        f"force={force}, cleanup={cleanup}, all_={all_}"
        + (f", name={name!r}" if name is not None else ", name=None")
        + ") and surface the returned ReindexResult envelope. "
        + (
            "cleanup/all_ are destructive \u2014 wait for the host's "
            "elicit-confirmation step before re-dispatching. "
            if destructive
            else ""
        )
        + _leftover_note(parsed.positionals, legacy, "reindex")
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
        "no-skip-reindex",
    }
)
_SYNC_KNOWN_FLAGS = _SYNC_VALUE_FLAGS | _SYNC_BOOL_FLAGS
# A request naming more than this many collections is not a request,
# it is a pasted sentence. Refuse and ask rather than fire one
# scrape-and-reindex chain per word.
_SYNC_MAX_NAMES = 5
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
    # `refuse_unknown`: `lies sync` with no positional syncs *every*
    # registered collection, so a typo that eats the one collection
    # name turns a one-collection request into a whole-library scrape
    # and reindex. Every other body is safe on an unknown flag --
    # dropping a flag leaves a valid command -- which is why this is a
    # parameter rather than the default.
    refusal = _refuse_unless_clean(parsed, "sync", refuse_unknown=True)
    if refusal:
        return [Message(refusal)]
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
            "no-skip-reindex",
        ),
    )
    if parsed.values.get("name") is not None:
        # `--name` is documented on the CLI as "Wiki to sync", while the
        # positional is a *collection*. The two sit side by side in the
        # rendered command with nothing to distinguish them, and an
        # agent reading the body would reasonably pass a collection
        # name to the wrong one.
        note += (
            " --name is the *wiki* to sync (`$LIES_WIKI_NAME`), not a "
            "collection; the positional is the collection. Do not pass a "
            "collection name to --name."
        )
    if names:
        # One command per name, because `lies sync` takes a single
        # positional — which also means an English sentence becomes one
        # expensive scrape-and-reindex chain *per word*.
        # `sync please resync my library collections` rendered five.
        # Above the cap the body refuses rather than rendering a
        # request the user never made, and names the words so the
        # agent can ask which of them are collection names.
        if len(names) > _SYNC_MAX_NAMES:
            return [
                Message(
                    f"Cannot run sync: this tail names {len(names)} things "
                    f"({shlex.join(names)}), and `lies sync` takes one "
                    f"collection per invocation, so it would fire "
                    f"{len(names)} scrape-and-reindex chains. That is more "
                    f"than the {_SYNC_MAX_NAMES} a single request is read as. "
                    f"Ask the user which of those are collection names, then "
                    f"re-dispatch with those alone. No command was run."
                )
            ]
        commands = "; ".join(f"Run Bash(lies sync {shlex.quote(n)}{shared})" for n in names)
        rendered = (
            f"{commands}. One invocation per collection, because `lies sync` "
            f"takes a single positional — this request names "
            f"{len(names)}: {shlex.join(names)}. Surface each run's "
            f"scrape/ingest status."
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
    def _ask_prompt(tail: str) -> list[Message]:
        return ask_prompt(tail)

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
            "(list, show/info, new/add, modify, delete/remove, tag, "
            "where, enrich-tags, bootstrap-all/register-shipped)."
        ),
    )
    def _collections_prompt(tail: str) -> list[Message]:
        return collections_prompt(tail)

    @mcp.prompt(
        name="ingest",
        description=("Bring a source into the library (--source <path> or --batch <directory>)."),
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
