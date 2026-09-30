"""Every ``Bash(...)`` a prompt renders must be a command ``lies`` accepts.

Three prompt bodies shipped flags that no ``lies`` command has —
``--data-dir``, ``--only``, ``--type``, ``--delete`` — and the two
prompts whose job is mutating the library rendered nothing that ran.
Nothing caught it, because
``test_prompts_flag_vocabulary.py`` proves a flag reaches the *rendered
body* and stops there. It cannot tell a rendered command the CLI
accepts from one it rejects with exit 2.

This module closes that gap by checking the rendered command against
the live Typer app: every ``--flag`` in it must be an option the
target command declares, every value-taking option must be followed
by a token, and every required Argument must have been supplied.
Introspection only — nothing is executed, so the test has no side
effects on the host library.

**The first version of this file closed one direction and claimed
two.** Its matrices iterated the *hand-written* tables, so a flag the
command declares and the table omits generated no row and went green;
and ``_check_command`` validated flag names and an arity ceiling only,
so it accepted ``lies library new`` — a command that exits 2 for a
missing slug. Both were found by mutating the implementation and
watching the suite stay green. Three tests below are the repair, and
each one fails on a mutation that the previous version survived:

``test_vocabulary_tables_match_the_live_signatures``
    Equality, both directions, between every vocabulary table and the
    options the live app declares. An omission is a failure; so is an
    invention. Replaces the "one tail per table entry" idea, which
    could only ever find inventions.
``test_every_declared_flag_reaches_the_rendered_command``
    A declared flag that the body knows about but never renders is a
    silent loss, and no table-equality test can see it — the flag is
    declared, the table is right, the *include tuple* in the body is
    not. This renders a tail naming every option and asserts each one
    reaches the command.
``test_a_body_never_renders_a_command_with_a_starved_option``
    A value-taking option with nothing after it: ``--source`` at the
    end of a rendered line.

Runtime preconditions are a fourth thing, and introspection cannot
reach them. ``lies library new`` requires ``--source`` and ``lies
ingest --source`` requires a collection name, both enforced by
``raise``/``typer.Exit`` in a function body rather than by the
signature. The tables below are exactly equal to the signatures and
the rendered command is still invalid; the body-level regressions at
the end of this file are what hold those two closed.
"""

from __future__ import annotations

import re
import shlex

import pytest
from typer.main import get_command

from lies.cli import app as root_app
from lies.mcp.prompts_impl import (
    _INGEST_BOOL_FLAGS,
    _INGEST_VALUE_FLAGS,
    _LIBRARY_VERB_FLAGS,
    _SYNC_BOOL_FLAGS,
    _SYNC_VALUE_FLAGS,
    collections_prompt,
    ground_prompt,
    ingest_prompt,
    sync_prompt,
)
from tests.unit.mcp._prompt_body import rendered_body

# Prompt spellings that have no command of their own and render under a
# different one. ``tag`` has no ``lies library tag``; the body renders
# ``lies library modify <slug> --tag …``, so the matrix has to give it a
# slug the same way it does for ``modify``.
_LIBRARY_ALIAS_VERBS = frozenset({"tag"})

# ``Run Bash(<command>)`` — the bodies wrap the command in a prose
# sentence, sometimes several in a row. Non-greedy so two adjacent
# commands split rather than the first swallowing the second.
_BASH_RE = re.compile(r"Bash\((.*?)\)")


def _all_opts(param: object) -> list[str]:
    """Every spelling of ``param``: ``opts`` plus ``secondary_opts``.

    Typer renders a ``--x/--no-x`` boolean as two spellings, and only
    the positive one lands in ``param.opts``:

    ```
    wait          ['--wait']   ['--no-wait']
    skip_reindex  ['--skip-reindex']  ['--no-skip-reindex']
    ```

    Reading ``opts`` alone made this test reject ``--no-wait``, a flag
    ``lies sync`` declares and the sync prompt renders. The test was
    wrong, not the table -- the failure pointed at the table, and the
    tempting repair was to delete a working flag from it. That is the
    defect class this file exists to kill, reproduced inside the file.
    """
    return [*(getattr(param, "opts", None) or []), *(getattr(param, "secondary_opts", None) or [])]


def _is_argument(param: object) -> bool:
    """Whether ``param`` is a click/typer Argument rather than an Option.

    Duck-typed on purpose: Typer vendors its own click fork
    (``typer._click``), so ``isinstance(p, click.Argument)`` is False
    for every parameter of a real Typer app. The discriminator that
    holds across both forks is the one click's parser itself uses --
    an Argument's ``opts`` are bare names, an Option's all begin with a
    dash.
    """
    opts = _all_opts(param)
    return bool(opts) and not any(opt.startswith("-") for opt in opts)


def _walk(argv: list[str]) -> tuple[object, list[str]]:
    """Resolve ``argv`` against the Typer app; return the command + leftovers.

    Raises ``AssertionError`` naming the token that did not resolve, so
    a body that renders ``lies sync`` as ``lies synch`` fails with a
    message that points at the typo rather than at click internals.
    """
    cmd: object = get_command(root_app)
    rest = list(argv)
    commands = getattr(cmd, "commands", None)
    while isinstance(commands, dict) and rest:
        name = rest.pop(0)
        assert name in commands, f"{name!r} is not a command of lies; have {sorted(commands)}"
        cmd = commands[name]
        commands = getattr(cmd, "commands", None)
    return cmd, rest


def _declared_options(cmd: object) -> set[str]:
    return {opt for param in cmd.params for opt in _all_opts(param)}  # type: ignore[attr-defined]


def _max_positionals(cmd: object) -> int | None:
    """How many positionals ``cmd`` accepts, or ``None`` if variadic.

    Optional Arguments count: ``lies sync`` takes an optional
    ``collection``, and over-arity is the failure this catches.
    """
    count = 0
    for param in cmd.params:  # type: ignore[attr-defined]
        if _is_argument(param):
            if getattr(param, "nargs", 1) == -1:
                return None
            count += 1
    return count


def _classify(cmd: object) -> tuple[frozenset[str], frozenset[str]]:
    """``cmd``'s declared options, split into (value-taking, boolean).

    Names are bare (``force``, not ``--force``), matching the shape the
    vocabulary tables in ``prompts_impl`` are written in. Both
    spellings of a ``--x/--no-x`` pair land in the same bucket, because
    Typer models them as one option.
    """
    value: set[str] = set()
    boolean: set[str] = set()
    for param in cmd.params:  # type: ignore[attr-defined]
        if _is_argument(param):
            continue
        names = {opt.lstrip("-") for opt in _all_opts(param)}
        (boolean if getattr(param, "is_flag", False) else value).update(names)
    return frozenset(value), frozenset(boolean)


def _check_command(command: str) -> None:
    """Assert ``command`` names a real command that ``lies`` can actually run.

    Four things, each of which was a real defect class:

    1. every ``--flag`` is an option the target command declares;
    2. every value-taking option is followed by a token (``--source``
       with nothing after it exits 2, and the old check walked off the
       end of the token list accepting it);
    3. every required ``Argument`` received a positional (``lies
       library new`` with no slug exits 2 — the old check only tested
       an upper bound, so the *lower* bound was untested);
    4. the positional count fits the declared arity.
    """
    argv = shlex.split(command)
    assert argv and argv[0] == "lies", f"not a `lies` invocation: {command!r}"
    cmd, rest = _walk(argv[1:])
    params = {opt: param for param in cmd.params for opt in _all_opts(param)}  # type: ignore[attr-defined]
    options = set(params)
    # One scan, because only the scan knows which token is a flag's
    # value: counting bare tokens separately would file ``--name mywiki``
    # as a positional the command does not take.
    positionals: list[str] = []
    bad: list[str] = []
    starved: list[str] = []
    supplied: set[str] = set()
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            key, eq, _ = tok.partition("=")
            param = params.get(key)
            if param is None or _is_argument(param):
                bad.append(key)
                i += 1
                continue
            supplied.add(key)
            if getattr(param, "is_flag", False) or eq:
                i += 1
                continue
            # Value-taking option: the next token is its value, and it
            # has to be there. `i + 2` past the end was the old bug.
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                starved.append(key)
                i += 1
                continue
            i += 2
        else:
            positionals.append(tok)
            i += 1
    assert not bad, (
        f"{command!r} passes {bad}, which `lies {cmd.name}` does not declare. "  # type: ignore[attr-defined]
        f"Declared: {sorted(options)}"
    )
    assert not starved, (
        f"{command!r} passes {starved} with no value after it; "
        f"`lies {cmd.name}` rejects that with exit 2."
    )
    arg_params = [p for p in cmd.params if _is_argument(p)]  # type: ignore[attr-defined]
    required_args = [p for p in arg_params if getattr(p, "required", False)]
    assert len(positionals) >= len(required_args), (
        f"{command!r} supplies {len(positionals)} positional(s) but "
        f"`lies {cmd.name}` requires {len(required_args)} "  # type: ignore[attr-defined]
        f"({', '.join(_all_opts(p)[0] for p in required_args)})"
    )
    required_opts = [
        opt
        for opt, param in params.items()
        if getattr(param, "required", False) and not _is_argument(param)
    ]
    assert set(required_opts) <= supplied, (
        f"{command!r} omits required option(s) {sorted(set(required_opts) - supplied)}."
    )
    ceiling = _max_positionals(cmd)
    assert ceiling is None or len(positionals) <= ceiling, (
        f"{command!r} passes {len(positionals)} positional(s) but "
        f"`lies {cmd.name}` takes at most {ceiling}"  # type: ignore[attr-defined]
    )


# Tails chosen to exercise every branch that renders a Bash command,
# including the flag combinations that used to be fiction.
TAILS: list[tuple[object, str]] = [
    (collections_prompt, "list"),
    (collections_prompt, "list --json"),
    (collections_prompt, "show claude_code"),
    (collections_prompt, "where claude_code"),
    (collections_prompt, "new mylib --source https://example.com/docs"),
    (collections_prompt, "add mylib /abs/path --tag cli --tag docs"),
    (collections_prompt, "modify claude_code --tag cli --tag docs"),
    (collections_prompt, "modify claude_code --untag cli --set source=https://x.example"),
    (collections_prompt, "modify claude_code --from-file patch.yaml"),
    (collections_prompt, "tag claude_code cli rust"),
    (collections_prompt, "delete claude_code"),
    (collections_prompt, "remove claude_code --force"),
    (collections_prompt, "enrich-tags"),
    (collections_prompt, "bootstrap-all"),
    (collections_prompt, "register-shipped --json"),
    (ingest_prompt, "docs/a.md"),
    (ingest_prompt, "--source docs/a.md --collection mylib --slug my-slug"),
    (ingest_prompt, "--source docs/a.md --title Pydantic basics --dry-run"),
    (ingest_prompt, "--batch docs/ --slug-prefix mylib"),
    (ingest_prompt, "--batch docs/ --exclude-stem _draft --exclude-dir node_modules --force"),
    (sync_prompt, ""),
    (sync_prompt, "all"),
    (sync_prompt, "pydantic"),
    (sync_prompt, "pydantic opencode"),
    (sync_prompt, "pydantic --force --skip-reindex"),
    (sync_prompt, "--source https://example.com/docs pydantic --wizard"),
    (sync_prompt, "--name mywiki --wait"),
    # Negated booleans. These are the flags ``param.opts`` alone does
    # not carry -- the case that made the test reject a working table.
    (sync_prompt, "pydantic --no-wait"),
    (sync_prompt, "pydantic --no-skip-reindex"),
    (sync_prompt, "pydantic --no-force --no-fail-busy"),
    (ingest_prompt, "docs/a.md --collection mylib --no-dry-run"),
    (ingest_prompt, "docs/a.md --collection mylib --no-force"),
]


# The hand-written list above proves the tails someone remembered. It
# was joined by matrices built from the vocabulary tables, on the claim
# that they made "table drift impossible instead of merely detected."
# They did not: they iterated the tables, so a *missing* entry produced
# no row, and the mutations below stayed green. The three tests that
# replaced them read the live app instead of the tables, which is the
# only direction that catches an omission.
def _positional_verbs() -> frozenset[str]:
    """Library verbs that declare a positional ``slug`` Argument.

    Read from the live app rather than from a hand-written list: this
    set exists only to give the coverage test a shape the verb accepts,
    so a list of its own is another table that can drift from the
    signature it is transcribed from.
    """
    library = get_command(root_app).commands["library"]
    return frozenset(name for name, cmd in library.commands.items() if _max_positionals(cmd))


def _library_commands() -> dict[str, object]:
    return dict(get_command(root_app).commands["library"].commands)


def _real_verb(verb: str) -> str:
    """The CLI command a prompt verb renders under.

    ``tag`` is a prompt spelling with no ``lies library tag`` of its
    own; the body renders ``lies library modify``, so the table row is
    transcribed from ``modify``'s signature.
    """
    return "modify" if verb in _LIBRARY_ALIAS_VERBS else verb


@pytest.mark.parametrize("verb", sorted(_LIBRARY_VERB_FLAGS))
def test_vocabulary_tables_match_the_live_signatures(verb: str) -> None:
    """The per-verb table equals the Typer signature, in both directions.

    Deleting ``"force"`` from ``_LIBRARY_VERB_FLAGS["delete"]`` — a
    flag ``lies library delete`` really declares — kept the whole
    suite green before this test existed, because every matrix
    iterated the table it was supposed to check. Equality against the
    app catches it.
    """
    real = _real_verb(verb)
    declared_value, declared_bool = _classify(_library_commands()[real])
    table_value, table_bool = _LIBRARY_VERB_FLAGS[verb]
    assert table_value == declared_value, (
        f"`lies library {real}` value flags drifted from the table. "
        f"Only in the table: {sorted(table_value - declared_value)}. "
        f"Only in the signature: {sorted(declared_value - table_value)}."
    )
    assert table_bool == declared_bool, (
        f"`lies library {real}` boolean flags drifted from the table. "
        f"Only in the table: {sorted(table_bool - declared_bool)}. "
        f"Only in the signature: {sorted(declared_bool - table_bool)}."
    )


def test_every_library_verb_has_a_table_entry() -> None:
    """A new ``lies library`` verb cannot ship without a table row.

    The per-verb equality test above only runs for verbs already in
    the table, so adding a verb to the CLI and forgetting the table
    would skip it rather than fail. This closes that direction.
    """
    declared = set(_library_commands()) - {"--help", "--version"}
    # ``tag`` is a prompt spelling that renders under ``modify``; it is
    # not a `lies library` command, so it is dropped from the table side
    # before the comparison rather than demanded of the CLI.
    table = set(_LIBRARY_VERB_FLAGS) - _LIBRARY_ALIAS_VERBS
    assert table == declared, (
        "lies library commands and the prompt's verb table disagree: "
        f"only in the CLI={sorted(declared - table)}, "
        f"only in the table={sorted(table - declared)}"
    )


@pytest.mark.parametrize(
    ("name", "value_tbl", "bool_tbl"),
    [
        ("sync", _SYNC_VALUE_FLAGS, _SYNC_BOOL_FLAGS),
        ("ingest", _INGEST_VALUE_FLAGS, _INGEST_BOOL_FLAGS),
    ],
)
def test_vocabulary_tables_match_flat_signatures(
    name: str, value_tbl: frozenset[str], bool_tbl: frozenset[str]
) -> None:
    declared_value, declared_bool = _classify(get_command(root_app).commands[name])
    assert value_tbl == declared_value, (
        f"`lies {name}` value flags drifted. "
        f"Only in the table: {sorted(value_tbl - declared_value)}. "
        f"Only in the signature: {sorted(declared_value - value_tbl)}."
    )
    assert bool_tbl == declared_bool, (
        f"`lies {name}` boolean flags drifted. "
        f"Only in the table: {sorted(bool_tbl - declared_bool)}. "
        f"Only in the signature: {sorted(declared_bool - bool_tbl)}."
    )


def _tail_naming_every_option(
    prompt_fn: object, verb: str, value: frozenset[str], boolean: frozenset[str], slug: str
) -> str:
    """A tail asking for every option the command declares, at once.

    One tail rather than one per flag: the point is to catch a flag
    the body declines to render, and a per-flag tail would be a
    separate case for each and easy to weaken one at a time.
    """
    parts = [f"{verb}{slug}"]
    parts += [f"--{flag} value" for flag in sorted(value)]
    parts += [f"--{flag}" for flag in sorted(boolean) if not flag.startswith("no-")]
    return " ".join(parts)


@pytest.mark.parametrize("verb", sorted(_LIBRARY_VERB_FLAGS))
def test_every_declared_flag_reaches_the_rendered_command(verb: str) -> None:
    """A flag the body parses but never renders is a silent loss.

    Dropping ``"prompt"`` from ``new``'s render include-tuple left the
    vocabulary table correct and the signature equal, and every
    pre-existing test green — ``--prompt`` is a real option that the
    user could type and the command would then run without. The
    include tuple is a *fourth* hand-written table, in the body, and no
    signature-derived test could see it.
    """
    value, boolean = _LIBRARY_VERB_FLAGS[verb]
    if not value and not boolean:
        return
    positional = _positional_verbs() | _LIBRARY_ALIAS_VERBS
    slug = " mylib" if verb in positional else ""
    tail = _tail_naming_every_option(collections_prompt, verb, value, boolean, slug)
    [msg] = collections_prompt(tail)  # type: ignore[operator]
    body = rendered_body(msg)
    commands = _BASH_RE.findall(body)
    if not commands:
        # `list` without --json renders an MCP call, not a Bash line.
        # Call it through the MCP sink instead of skipping the verb.
        assert "collections_read" in body, body
        commands = []
    rendered_flags: set[str] = set()
    for command in commands:
        _check_command(command.strip())
        rendered_flags |= {
            tok.partition("=")[0] for tok in shlex.split(command) if tok.startswith("--")
        }
    # `new` needs a source, so the body renders `--source` from the
    # tail above; `tag` renders under `modify`. Everything the table
    # declares must reach the command line.
    expected = {f"--{flag}" for flag in value | boolean if not flag.startswith("no-")}
    assert expected <= rendered_flags, (
        f"`{verb}` parsed {sorted(expected - rendered_flags)} but did not render it.\n{body}"
    )


def test_a_body_never_renders_a_command_with_a_starved_option() -> None:
    """A value-taking option with nothing after it is the exit-2 case.

    ``_check_command`` walked past the end of the token list accepting
    ``--source`` with no value; this pins the check itself against a
    command that is genuinely unrunnable.
    """
    for broken in (
        "lies library new --source",
        "lies library modify mylib --tag",
        "lies ingest --source",
        "lies sync pydantic --name",
    ):
        try:
            _check_command(broken)
        except AssertionError:
            continue
        raise AssertionError(f"_check_command accepted an unrunnable command: {broken!r}")


def test_a_body_never_renders_a_command_missing_a_required_argument() -> None:
    """The lower bound on arity, which the ceiling check never tested."""
    for broken in (
        "lies library new",
        "lies library modify",
        "lies library show",
        "lies library where",
        "lies library delete",
    ):
        try:
            _check_command(broken)
        except AssertionError:
            continue
        raise AssertionError(f"_check_command accepted a command missing its argument: {broken!r}")


# --- runtime preconditions -------------------------------------------------
# `lies library new` raises `BadParameter("library new requires
# --source")` in its body and `lies ingest --source` exits 2 without a
# collection name, both below the signature. Introspection cannot see
# either, and both are the *default* invocation of their verb, so the
# body has to refuse. These are the two shapes that made the
# "the CLI accepts every rendered command" claim false.


@pytest.mark.parametrize(
    "tail",
    ["new mylib", "new mylib --tag cli", "new mylib --prompt wizard.md"],
)
def test_collections_new_refuses_without_a_source(tail: str) -> None:
    body = rendered_body(collections_prompt(tail)[0])
    assert body.startswith("Cannot run 'new': no source was given"), body
    assert "Run Bash(" not in body, body


@pytest.mark.parametrize("tail", ["/tmp/real.md", "--source /tmp/real.md"])
def test_ingest_refuses_a_source_with_no_collection(tail: str) -> None:
    body = rendered_body(ingest_prompt(tail)[0])
    assert body.startswith("Cannot run ingest: --source"), body
    assert "Run Bash(" not in body, body


def test_ingest_renders_when_a_collection_is_named() -> None:
    body = rendered_body(ingest_prompt("/tmp/real.md --collection mylib")[0])
    assert "Run Bash(lies ingest --source /tmp/real.md --collection mylib)" in body, body


def test_a_bare_double_dash_is_a_terminator_not_a_collection_name() -> None:
    """``--`` reaches Click stripped, and a bare ``lies sync`` syncs all.

    ``sync -- pydantic`` rendered ``Bash(lies sync --)`` as well as the
    real one. Click removes a bare ``--`` before the command function
    runs, so the first invocation received ``collection=None`` — every
    registered collection, scraped and reindexed, from a request about
    one.
    """
    body = rendered_body(sync_prompt("-- pydantic")[0])
    assert "lies sync --)" not in body, body
    assert body.count("Run Bash(") == 1, body
    assert "Bash(lies sync pydantic)" in body, body


def test_a_single_dash_token_is_never_an_argument() -> None:
    """``-pydantic`` is a typo, and a bare ``lies sync`` syncs everything.

    Letting it fall through to a positional rendered
    ``Bash(lies sync -pydantic)`` (exit 2). Recording it as an unknown
    flag emptied the name list instead, which rendered the *far* worse
    ``Bash(lies sync)`` — every collection. ``sync`` therefore refuses
    on an unrecognized flag rather than dropping it.
    """
    body = rendered_body(sync_prompt("-pydantic")[0])
    assert body.startswith("Cannot run sync:"), body
    assert "Run Bash(" not in body, body


@pytest.mark.parametrize(
    ("prompt_fn", "tail"),
    TAILS,
    ids=[f"{getattr(fn, '__name__', fn)}:{tail}" for fn, tail in TAILS],
)
def test_rendered_bash_command_matches_the_real_cli(prompt_fn: object, tail: str) -> None:
    [msg] = prompt_fn(tail)  # type: ignore[operator]
    body = rendered_body(msg)
    commands = _BASH_RE.findall(body)
    if not commands:
        pytest.skip(f"{tail!r} renders no Bash command (a refusal or an MCP call)")
    for command in commands:
        _check_command(command.strip())


def test_the_old_invented_flags_were_real_defects() -> None:
    """Pin the regression, so the fix cannot be undone by widening a table.

    These four are the exact strings the review found. Each exited 2
    from the real CLI. They live here as negative fixtures because
    ``_check_command`` alone would go green if every prompt simply
    stopped rendering a command at all.
    """
    root = get_command(root_app)
    for invented in ("--data-dir", "--only", "--jobs", "--scraper-timeout", "--dry-run"):
        assert invented not in _declared_options(root.commands["sync"]), (
            f"{invented} is now real; the prompt table should offer it"
        )
    for invented in ("--data-dir", "--type", "--delete"):
        assert invented not in _declared_options(root.commands["ingest"]), (
            f"{invented} is now real; the ingest body should offer it"
        )


def test_ground_keeps_flag_shaped_words_in_the_question() -> None:
    """A question about an option flag grounds the option flag.

    The same defect the filter parser guards against, one layer up:
    running the full flag grammar over a question deleted its
    ``--only`` and searched for the remainder.
    """
    for question, expected in (
        ("what is the --only flag", "what is the --only flag"),
        ("what does --dry-run do", "what does --dry-run do"),
        ("how does --jobs 8 differ from --jobs 4", "how does --jobs 8 differ from --jobs 4"),
    ):
        [msg] = ground_prompt(question)
        body = rendered_body(msg)
        assert f"\n{expected}\n```" in body, f"{question!r} lost words:\n{body}"


def test_ground_still_parses_a_leading_top_k() -> None:
    for tail in ("what changed --top_k=5", "--top_k 5 what changed"):
        [msg] = ground_prompt(tail)
        body = rendered_body(msg)
        assert "top_k=5" in body, f"{tail!r} lost --top_k:\n{body}"


def test_ground_honours_the_double_dash_terminator() -> None:
    """``--`` says the flags stop; the rest is text, flags and all.

    ``top_k`` stays at its default, because a flag after ``--`` is the
    user quoting a flag *in a question*, not setting one.
    """
    [msg] = ground_prompt("-- --top_k 5 what changed")
    body = rendered_body(msg)
    assert "\n--top_k 5 what changed\n```" in body, body
    assert "top_k=3 entries" in body, body
