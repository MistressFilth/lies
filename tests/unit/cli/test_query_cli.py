"""CLI tests for the ``lies query`` tag-filter surface (Bundle C / F15).

``lies query`` takes variadic positional tokens now. The argv walker
peels an optional ``+`` include chain and an optional ``-`` exclude
atom off the front; whatever remains joins with single spaces into
the question. An explicit ``--tag-expr`` / ``--exclude-tag`` form is
accepted for callers that don't want the prefix syntax; it mirrors
the MCP tool's argument shape and converges on the same
``ResolvedTagFilter``.

Back-compat is the load-bearing contract: both the quoted single-token
form (``lies query "what is X?"``) and the shell-split bare form
(``lies query what is X?`` → tokens ``["what", "is", "X?"]``) must
still reach the orchestrator with ``tag_filter=None``.

Grammar and resolver errors (``TagExprParseError``, ``TagExprUnknown``)
exit 2 per the spec's error model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.config_io import save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig
from lies.query.models import SynthesizedAnswer
from lies.wiki.wiki import Wiki

runner = CliRunner()

_NOW = datetime(2026, 9, 9, tzinfo=UTC)

# name -> tags. The implicit self-tag means the collection name itself
# is also an addressable tag, so ``+amazon`` resolves even though it is
# only incidentally in the tag list.
_COLLECTIONS = {
    "airflow": ["airflow", "provider"],
    "amazon": ["amazon", "aws"],
    "pyspark": ["pyspark"],
    "python": ["python"],
}


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A real on-disk wiki with four tagged collections.

    Tag resolution is library-first, so this fixture seeds both the
    wiki's yaml configs (legacy layout) AND the library's
    collections_root (current layout). Collections: airflow, amazon,
    pyspark, python.
    """
    import shutil

    from lies.constants import LIES_DATA_SUBDIR

    name = "tagfilter"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    Library.open.cache_clear()
    w = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    w.data_root.mkdir(parents=True, exist_ok=True)
    w.collections_dir.mkdir(parents=True, exist_ok=True)

    # Seed the library with the collection names + tags so tag
    # resolution (library-first) finds them. Post-cutover (Task 8) the
    # wiki-yaml configs in ``wiki.collections_dir/`` are no longer the
    # resolver's source of truth, so the wiki-yaml seeding the legacy
    # test fixture did is intentionally dropped here.
    lib_root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
    if lib_root.exists():
        shutil.rmtree(lib_root)
    lib_root.mkdir(parents=True, exist_ok=True)
    (lib_root / "collections").mkdir(parents=True, exist_ok=True)
    for coll_name in _COLLECTIONS:
        coll_dir = lib_root / "collections" / coll_name
        coll_dir.mkdir()
        config = LibraryCollectionConfig(
            name=coll_name,
            source=f"https://example.com/{coll_name}",
            tags=tuple(_COLLECTIONS[coll_name]),
            language="en",
            version="1.0.0",
            created_at=_NOW,
            updated_at=_NOW,
            config={},
        )
        save_config(config)

    return w


def _invoke(*argv: str):
    """Run ``lies query`` with a mocked orchestrator; return (result, call)."""
    with mock.patch("lies.cli.Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(answer="X.")
        result = runner.invoke(app, ["query", *argv])
        call = orch_cls.return_value.run_query.call_args
    return result, call


# ---------------------------------------------------------------------------
# Back-compat: no filter
# ---------------------------------------------------------------------------


def test_query_quoted_question_back_compat(wiki: Wiki) -> None:
    """A single quoted token is the question verbatim; no filter."""
    result, call = _invoke("what is X?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    # F18/F19: the CLI translates the legacy ``ResolvedTagFilter`` into
    # ``tag_expr`` / ``exclude_tags`` kwargs on ``Orchestrator.run_query``.
    # No filter parsed → both are None.
    assert call.kwargs["tag_expr"] is None
    assert call.kwargs["exclude_tags"] is None


def test_query_shell_split_bare_form(wiki: Wiki) -> None:
    """Unquoted `lies query what is X?` arrives shell-split and rejoins.

    Pins the back-compat contract for the variadic positional: no token
    starts with ``+`` or ``-``, so no filter is parsed and the tokens
    join with single spaces.
    """
    result, call = _invoke("what", "is", "X?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_expr"] is None
    assert call.kwargs["exclude_tags"] is None


# ---------------------------------------------------------------------------
# Prefix form
# ---------------------------------------------------------------------------


def test_query_with_plus_tag(wiki: Wiki) -> None:
    """``+airflow what are DAGs?`` splits into filter + question."""
    result, call = _invoke("+airflow", "what", "are", "DAGs?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what are DAGs?"
    assert call.kwargs["tag_expr"] == "airflow"
    assert call.kwargs["exclude_tags"] is None


def test_query_with_and_chain_and_exclude(wiki: Wiki) -> None:
    """``+airflow&amazon -python compare X and Y`` — chain, exclude, question."""
    result, call = _invoke("+airflow&amazon", "-python", "compare", "X", "and", "Y")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "compare X and Y"
    assert call.kwargs["tag_expr"] == "airflow&amazon"
    assert call.kwargs["exclude_tags"] == ["python"]


def test_query_with_or_chain(wiki: Wiki) -> None:
    """``|`` binds looser than ``&`` and resolves both branches."""
    result, call = _invoke("+airflow|pyspark", "what", "is", "X?")
    assert result.exit_code == 0, result.output
    assert call.kwargs["tag_expr"] == "airflow|pyspark"


def test_query_bare_exclude_only(wiki: Wiki) -> None:
    """A bare ``-tag`` with no ``+`` chain is the natural mirror of bare ``+``."""
    result, call = _invoke("-amazon", "what", "is", "S3?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is S3?"
    assert call.kwargs["tag_expr"] is None
    assert call.kwargs["exclude_tags"] == ["amazon"]


# ---------------------------------------------------------------------------
# Explicit form
# ---------------------------------------------------------------------------


def test_query_explicit_tag_expr_and_exclude_tag(wiki: Wiki) -> None:
    """``--tag-expr`` / ``--exclude-tag`` produce the same ResolvedTagFilter."""
    result, call = _invoke(
        "what",
        "is",
        "X?",
        "--tag-expr",
        "airflow&amazon",
        "--exclude-tag",
        "python",
    )
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_expr"] == "airflow&amazon"
    assert call.kwargs["exclude_tags"] == ["python"]


def test_query_explicit_exclude_tag_only(wiki: Wiki) -> None:
    """``--exclude-tag`` alone yields an include-less filter."""
    result, call = _invoke("what is X?", "--exclude-tag", "amazon")
    assert result.exit_code == 0, result.output
    assert call.kwargs["exclude_tags"] == ["amazon"]


def test_query_explicit_form_does_not_split_prefixes(wiki: Wiki) -> None:
    """With the explicit form, positional tokens are the question verbatim."""
    result, call = _invoke("+airflow", "is", "cool", "--tag-expr", "amazon")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "+airflow is cool"
    assert call.kwargs["tag_expr"] == "amazon"


# ---------------------------------------------------------------------------
# Error model — exit 2
# ---------------------------------------------------------------------------


def test_query_unknown_tag_exits_2(wiki: Wiki) -> None:
    """An include atom outside the registry exits 2 with the exact spelling."""
    result, _ = _invoke("+nope", "what", "is", "X?")
    assert result.exit_code == 2
    assert "unknown tag: 'nope'" in result.output


def test_query_explicit_unknown_tag_exits_2(wiki: Wiki) -> None:
    """The explicit form validates against the same available set."""
    result, _ = _invoke("what is X?", "--tag-expr", "nope")
    assert result.exit_code == 2
    assert "unknown tag: 'nope'" in result.output


def test_query_filter_without_question_exits_2(wiki: Wiki) -> None:
    """``lies query +airflow`` — filter present but no question."""
    result, _ = _invoke("+airflow")
    assert result.exit_code == 2
    assert "filter present but no question" in result.output


def test_query_plus_without_atom_exits_2(wiki: Wiki) -> None:
    """A bare ``+`` is a grammar error, not a question token."""
    result, _ = _invoke("+", "what", "is", "X?")
    assert result.exit_code == 2
    assert "without atom" in result.output


def test_query_dangling_operator_exits_2(wiki: Wiki) -> None:
    """``+a&`` with nothing after the operator is a grammar error."""
    result, _ = _invoke("+airflow&", "what", "is", "X?")
    assert result.exit_code == 2
    assert "dangling operator" in result.output


# ---------------------------------------------------------------------------
# F15 — t:/c: qualifier prefix on argv include + exclude + explicit kwarg
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_query_cli_plus_c_qualifier(wiki: Wiki) -> None:
    """``+c:airflow`` parses to ``Include(tag='airflow', qualifier='c')``.

    The argv splitter peels ``+`` off the front, then ``parse_tokens``
    strips the ``c:`` qualifier. The CLI's compat layer renders the
    include AST back to ``tag_expr='c:airflow'`` so the F18
    ``librarian_agent`` sees the qualified atom.
    """
    result, call = _invoke("+c:airflow", "what", "is", "X?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_expr"] == "c:airflow"
    assert call.kwargs["exclude_tags"] is None


def test_query_cli_t_python_c_python_exclude(wiki: Wiki) -> None:
    """Canonical example: ``+t:python -c:python`` resolves both qualifiers.

    The CLI renders ``t:python`` into ``tag_expr`` and strips the
    ``c:`` prefix from the exclude (the F18 ``librarian_agent``
    consumes the body, not the qualifier; only the MCP path keeps
    the ``exclude_qualifier`` on the wire).
    """
    result, call = _invoke("+t:python", "-c:python", "what", "is", "X?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_expr"] == "t:python"
    assert call.kwargs["exclude_tags"] == ["python"]


def test_query_cli_explicit_tag_expr_with_qualifiers(wiki: Wiki) -> None:
    """The explicit form accepts prefixes in both ``--tag-expr`` and ``--exclude-tag``."""
    result, call = _invoke(
        "what",
        "is",
        "X?",
        "--tag-expr",
        "t:python&c:amazon",
        "--exclude-tag",
        "c:python",
    )
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_expr"] == "t:python&c:amazon"
    assert call.kwargs["exclude_tags"] == ["python"]


def test_query_cli_bad_qualifier_exits_2(wiki: Wiki) -> None:
    """``+x:foo`` is a parse error — ``x:`` is not a known qualifier."""
    result, _ = _invoke("+x:foo", "what", "is", "X?")
    assert result.exit_code == 2
    assert "unknown qualifier" in result.output


def test_query_cli_bad_qualifier_on_exclude_exits_2(wiki: Wiki) -> None:
    """``-x:foo`` on the argv exclude is a parse error."""
    result, _ = _invoke("+airflow", "-x:foo", "what", "is", "X?")
    assert result.exit_code == 2
    assert "unknown qualifier" in result.output


def test_query_explicit_empty_tag_expr_exits_2(wiki: Wiki) -> None:
    """``--tag-expr ""`` raises ``TagExprEmpty`` via CLI → exit 2.

    Empty string must reach ``parse(...)`` so it raises ``TagExprEmpty``;
    the falsy-``tag_expr`` shortcut silently swallowed it before, so
    the explicit form diverged from MCP behavior. MCP already raises
    ``ToolError("empty tag expression: ...")`` at the same input.
    """
    result, _ = _invoke("what", "is", "X?", "--tag-expr", "")
    assert result.exit_code == 2
    assert "no atoms" in result.output
