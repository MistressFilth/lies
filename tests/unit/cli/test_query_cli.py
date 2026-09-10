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
from lies.collections.record import Collection, save_collection
from lies.query.models import SynthesizedAnswer
from lies.query.tag_expr import And, Include, Or
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
}


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A real on-disk wiki with three tagged collections."""
    name = "tagfilter"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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
    for coll_name, tags in _COLLECTIONS.items():
        save_collection(
            w,
            Collection(
                name=coll_name,
                path=w.data_root / "raw" / coll_name,
                source=f"https://example.com/{coll_name}",
                tags=list(tags),
                scraper_cmd=None,
                doc_path=None,
                mapper_model=None,
                language="en",
                version="1.0.0",
                created_at=_NOW,
                updated_at=_NOW,
                config={},
            ),
        )
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
    assert call.kwargs["tag_filter"] is None


def test_query_shell_split_bare_form(wiki: Wiki) -> None:
    """Unquoted `lies query what is X?` arrives shell-split and rejoins.

    Pins the back-compat contract for the variadic positional: no token
    starts with ``+`` or ``-``, so no filter is parsed and the tokens
    join with single spaces.
    """
    result, call = _invoke("what", "is", "X?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    assert call.kwargs["tag_filter"] is None


# ---------------------------------------------------------------------------
# Prefix form
# ---------------------------------------------------------------------------


def test_query_with_plus_tag(wiki: Wiki) -> None:
    """``+airflow what are DAGs?`` splits into filter + question."""
    result, call = _invoke("+airflow", "what", "are", "DAGs?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what are DAGs?"
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter is not None
    assert tag_filter.include == Include("airflow")
    assert tag_filter.exclude is None


def test_query_with_and_chain_and_exclude(wiki: Wiki) -> None:
    """``+airflow&provider -amazon compare X and Y`` — chain, exclude, question."""
    result, call = _invoke("+airflow&provider", "-amazon", "compare", "X", "and", "Y")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "compare X and Y"
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter.include == And(Include("airflow"), Include("provider"))
    assert tag_filter.exclude == "amazon"


def test_query_with_or_chain(wiki: Wiki) -> None:
    """``|`` binds looser than ``&`` and resolves both branches."""
    result, call = _invoke("+airflow|pyspark", "what", "is", "X?")
    assert result.exit_code == 0, result.output
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter.include == Or(Include("airflow"), Include("pyspark"))


def test_query_bare_exclude_only(wiki: Wiki) -> None:
    """A bare ``-tag`` with no ``+`` chain is the natural mirror of bare ``+``."""
    result, call = _invoke("-amazon", "what", "is", "S3?")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is S3?"
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter.include is None
    assert tag_filter.exclude == "amazon"


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
        "airflow&provider",
        "--exclude-tag",
        "amazon",
    )
    assert result.exit_code == 0, result.output
    assert call.args[0] == "what is X?"
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter.include == And(Include("airflow"), Include("provider"))
    assert tag_filter.exclude == "amazon"


def test_query_explicit_exclude_tag_only(wiki: Wiki) -> None:
    """``--exclude-tag`` alone yields an include-less filter."""
    result, call = _invoke("what is X?", "--exclude-tag", "amazon")
    assert result.exit_code == 0, result.output
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter.include is None
    assert tag_filter.exclude == "amazon"


def test_query_explicit_form_does_not_split_prefixes(wiki: Wiki) -> None:
    """With the explicit form, positional tokens are the question verbatim."""
    result, call = _invoke("+airflow", "is", "cool", "--tag-expr", "amazon")
    assert result.exit_code == 0, result.output
    assert call.args[0] == "+airflow is cool"
    assert call.kwargs["tag_filter"].include == Include("amazon")


# ---------------------------------------------------------------------------
# Error model — exit 2
# ---------------------------------------------------------------------------


def test_query_unknown_tag_exits_2(wiki: Wiki) -> None:
    """An include atom outside the registry exits 2 with the exact spelling."""
    result, _ = _invoke("+nope", "what", "is", "X?")
    assert result.exit_code == 2
    assert "unknown tag: nope" in result.output


def test_query_explicit_unknown_tag_exits_2(wiki: Wiki) -> None:
    """The explicit form validates against the same available set."""
    result, _ = _invoke("what is X?", "--tag-expr", "nope")
    assert result.exit_code == 2
    assert "unknown tag: nope" in result.output


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
