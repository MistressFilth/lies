"""Every flag a prompt advertises must reach the command it renders.

Three of the defects this branch shipped came from one root cause: the
flag vocabulary in a prompt body and the vocabulary the parser was told
about drift apart, and nothing checked. ``sync --only`` rendered the
flag into its own command and then reported it as unrecognized;
``collections`` advertised ``--tags`` that no parser knew, so the value
fell through as a bare positional; ``reindex --name X all`` swallowed
the destructive marker into the name.

The table below is the contract. Each row is a flag a prompt's
``instructions.md`` entry or body promises, and the assertion is that
the flag survives the parse into the rendered output -- not that it
merely fails to be reported as unknown. "Not reported" is how all three
defects hid.
"""

from __future__ import annotations

import pytest

from lies.mcp.prompts_impl import (
    collections_prompt,
    ground_prompt,
    ingest_prompt,
    lint_prompt,
    reindex_prompt,
    sync_prompt,
)
from tests.unit.mcp._prompt_body import rendered_body

# (prompt fn, tail, substring that must appear, substring that must not)
FlagCase = tuple[object, str, str, str | None]

FLAG_VOCABULARY: list[FlagCase] = [
    # -- ingest ------------------------------------------------------
    # Every flag below is declared by the Typer signature in
    # ``src/lies/library/cli.py``. ``lies ingest`` has no positional
    # argument, so a bare path binds to ``--source``.
    (ingest_prompt, "docs/a.md", "--source docs/a.md", None),
    (
        ingest_prompt,
        "docs/a.md --collection mylib",
        "--collection mylib",
        None,
    ),
    (ingest_prompt, "docs/a.md --slug my-slug", "--slug my-slug", None),
    (
        ingest_prompt,
        "docs/a.md --title Pydantic basics",
        "--title 'Pydantic basics'",
        None,
    ),
    (
        ingest_prompt,
        "--batch docs/ --slug-prefix mylib",
        "--slug-prefix mylib",
        None,
    ),
    (ingest_prompt, "docs/a.md --dry-run", "--dry-run", None),
    (ingest_prompt, "docs/a.md --force", "--force", None),
    (ingest_prompt, "--batch docs/ --exclude-stem _draft", "--exclude-stem _draft", None),
    # -- lint --------------------------------------------------------
    (lint_prompt, "--check orphans", "check='orphans'", None),
    (lint_prompt, "--fix", "fix=True", "fix=False"),
    (lint_prompt, "--check orphans --fix", "fix=True", "fix=False"),
    # -- reindex -----------------------------------------------------
    (reindex_prompt, "--reconcile", "reconcile=True", "reconcile=False"),
    (reindex_prompt, "--embed", "embed=True", "embed=False"),
    (reindex_prompt, "--force", "force=True", "force=False"),
    (reindex_prompt, "--cleanup", "cleanup=True", "cleanup=False"),
    (reindex_prompt, "--all", "all_=True", "all_=False"),
    (reindex_prompt, "all", "all_=True", "all_=False"),
    (reindex_prompt, "all_", "all_=True", "all_=False"),
    (reindex_prompt, "--all=true", "all_=True", "all_=False"),
    (reindex_prompt, "--name pydantic", "name='pydantic'", "name=None"),
    # -- sync --------------------------------------------------------
    (sync_prompt, "all", "Run Bash(lies sync)", "--only"),
    (sync_prompt, "pydantic --force", "--force", None),
    (sync_prompt, "pydantic --skip-reindex", "--skip-reindex", None),
    (sync_prompt, "--name mywiki --wait", "--name mywiki --wait", None),
    # -- ground ------------------------------------------------------
    (ground_prompt, "--top_k 5 what changed", "top_k=5", None),
    (ground_prompt, "--top_k=5 what changed", "top_k=5", None),
    # -- collections -------------------------------------------------
    (collections_prompt, "add mylib /abs/path", "lies library new mylib", None),
    (collections_prompt, "new mylib --source /abs/path", "--source /abs/path", None),
    (collections_prompt, "new mylib --source /abs/path --tag cli", "--tag cli", None),
    (collections_prompt, "modify claude_code --tag cli", "--tag cli", None),
    (collections_prompt, "modify claude_code --untag cli", "--untag cli", None),
    (collections_prompt, "modify claude_code --set tags=a,b", "--set tags=a,b", None),
    (collections_prompt, "tag claude_code cli rust", "--tag cli --tag rust", None),
    (collections_prompt, "remove claude_code", "lies library delete claude_code", None),
    (collections_prompt, "remove claude_code --force", "--force", None),
    (collections_prompt, "info claude_code", 'subcommand="info"', None),
    (collections_prompt, "where claude_code", "lies library where claude_code", None),
    (
        collections_prompt,
        "register-shipped",
        "lies library bootstrap-all",
        None,
    ),
    (collections_prompt, "register-shipped --json", "--json", None),
    (collections_prompt, "list --json", "lies library list --json", "needs a value"),
    (collections_prompt, "enrich-tags", "lies library enrich-tags", None),
]


@pytest.mark.parametrize(
    ("prompt_fn", "tail", "expected", "forbidden"),
    FLAG_VOCABULARY,
    ids=[f"{getattr(fn, '__name__', fn)}:{tail}" for fn, tail, _, _ in FLAG_VOCABULARY],
)
def test_advertised_flag_survives_into_the_rendered_body(
    prompt_fn: object,
    tail: str,
    expected: str,
    forbidden: str | None,
) -> None:
    [msg] = prompt_fn(tail)  # type: ignore[operator]
    body = rendered_body(msg)
    if expected:
        assert expected in body, f"{tail!r} lost {expected!r}:\n{body}"
    if forbidden is not None:
        assert forbidden not in body, f"{tail!r} should not render {forbidden!r}:\n{body}"


class TestNoSilentFlagLoss:
    """A flag the user typed is never dropped without a word about it."""

    @pytest.mark.parametrize(
        ("prompt_fn", "tail", "marker"),
        [
            (reindex_prompt, "--all=true", "all_=True"),
            (reindex_prompt, "--cleanup=1", "cleanup=True"),
            (lint_prompt, "--fix=yes", "fix=True"),
            (sync_prompt, "--dry-run=1", "--dry-run"),
            (ingest_prompt, "docs/a.md --type concept --dry-run=true", "--dry-run"),
        ],
    )
    def test_boolean_written_with_equals_is_honoured(
        self, prompt_fn: object, tail: str, marker: str
    ) -> None:
        """``--flag=true`` is a flag the user set, however they spelled it.

        The value is meaningless on a boolean, so the parser discards
        it -- but the flag itself must land, and the discard is named
        in the body so the agent can tell the user their spelling was
        off rather than silently guessing.
        """
        [msg] = prompt_fn(tail)  # type: ignore[operator]
        body = rendered_body(msg)
        assert marker in body, f"{tail!r} silently dropped the flag:\n{body}"
        assert "takes no value" in body


class TestDestructiveMarkerSurvives:
    def test_name_does_not_swallow_the_all_marker(self) -> None:
        """The regression: ``--name`` was declared multi-word.

        ``reindex --name pydantic all`` bound ``name='pydantic all'``
        and left ``all_=False``, so a destructive full rebuild ran
        without the confirmation the body promises.
        """
        [msg] = reindex_prompt("--name pydantic all")
        body = rendered_body(msg)
        assert "name='pydantic'" in body
        assert "all_=True" in body
        assert "destructive" in body

    def test_name_with_equals_form_also_leaves_all_alone(self) -> None:
        [msg] = reindex_prompt("--name=pydantic all")
        body = rendered_body(msg)
        assert "name='pydantic'" in body
        assert "all_=True" in body
