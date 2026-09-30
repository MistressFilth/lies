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
target command declares, and the positional count must fit the
arguments it declares. Introspection only — nothing is executed, so
the test has no side effects on the host library.

A body that invents a flag fails here even if every other test
passes, which is the point: the vocabulary tables in
``prompts_impl`` are transcribed by hand, and this file is what keeps
the transcription honest.
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


def _check_command(command: str) -> None:
    """Assert ``command`` names a real command with real flags and arity."""
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
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            key = tok.partition("=")[0]
            param = params.get(key)
            if param is None or _is_argument(param):
                bad.append(key)
                i += 1
                continue
            if not getattr(param, "is_flag", False) and "=" not in tok:
                i += 2
                continue
        else:
            positionals.append(tok)
        i += 1
    assert not bad, (
        f"{command!r} passes {bad}, which `lies {cmd.name}` does not declare. "  # type: ignore[attr-defined]
        f"Declared: {sorted(options)}"
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
    (ingest_prompt, "docs/a.md --no-dry-run"),
    (ingest_prompt, "docs/a.md --no-force"),
]


# The hand-written list above proves the tails someone remembered. The
# matrix below proves the rest: one tail per flag in every vocabulary
# table, so a flag the table gains and the command does not declare --
# or a flag the command declares and the table omits, which renders a
# command missing the switch the user asked for -- is a failing test
# rather than a defect the next reviewer has to find. Generating the
# tails from the tables is what makes table drift impossible instead of
# merely detected.
def _positional_verbs() -> frozenset[str]:
    """Library verbs that declare a positional ``slug`` Argument.

    Read from the live app rather than from a hand-written list: this
    set exists only to give the matrix a shape the verb accepts, so a
    list of its own is another table that can drift from the signature
    it is transcribed from.
    """
    library = get_command(root_app).commands["library"]
    return frozenset(name for name, cmd in library.commands.items() if _max_positionals(cmd))


def _library_matrix() -> list[tuple[object, str]]:
    rows: list[tuple[object, str]] = []
    positional = _positional_verbs() | _LIBRARY_ALIAS_VERBS
    for verb, (values, bools) in _LIBRARY_VERB_FLAGS.items():
        # Verbs whose *rendered* command has a ``slug`` Argument need
        # one; the rest take none, and a spare positional there would be
        # surplus the body reports (still a valid command, but not the
        # shape under test). ``_LIBRARY_ALIAS_VERBS`` covers the
        # spellings that render under a different command name --
        # ``tag`` renders ``lies library modify``, and reading the
        # names from the live app alone would miss that.
        slug = " mylib" if verb in positional else ""
        for flag in sorted(values):
            rows.append((collections_prompt, f"{verb}{slug} --{flag} value"))
        for flag in sorted(bools):
            rows.append((collections_prompt, f"{verb}{slug} --{flag}"))
    return rows


def _sync_matrix() -> list[tuple[object, str]]:
    rows: list[tuple[object, str]] = [
        (sync_prompt, f"pydantic --{flag} value") for flag in sorted(_SYNC_VALUE_FLAGS)
    ]
    rows += [(sync_prompt, f"pydantic --{flag}") for flag in sorted(_SYNC_BOOL_FLAGS)]
    return rows


def _ingest_matrix() -> list[tuple[object, str]]:
    rows: list[tuple[object, str]] = [
        (ingest_prompt, f"docs/a.md --{flag} value") for flag in sorted(_INGEST_VALUE_FLAGS)
    ]
    rows += [(ingest_prompt, f"docs/a.md --{flag}") for flag in sorted(_INGEST_BOOL_FLAGS)]
    return rows


TAILS += _library_matrix() + _sync_matrix() + _ingest_matrix()


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
