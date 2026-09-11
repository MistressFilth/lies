"""Unit tests for the tag-filter AST + exception surface."""

from __future__ import annotations

import pytest

from lies.query.tag_expr import (
    And,
    Include,
    Or,
    ResolvedTagFilter,
    TagExprEmpty,
    TagExprParseError,
    TagExprUnknown,
    _render_include,
    parse_include,
    parse_query_argv,
    resolve,
)


def test_include_node():
    node = Include("airflow")
    assert node.tag == "airflow"


def test_and_node():
    left = Include("airflow")
    right = Include("provider")
    node = And(left, right)
    assert node.left == left
    assert node.right == right


def test_or_node():
    left = And(Include("airflow"), Include("provider"))
    right = Include("pyspark")
    node = Or(left, right)
    assert isinstance(node.left, And)
    assert node.right == Include("pyspark")


def test_resolved_tag_filter_default():
    f = ResolvedTagFilter()
    assert f.include is None
    assert f.exclude is None


def test_exceptions_exist():
    # Ensure the exception types are importable and distinct.
    assert issubclass(TagExprParseError, Exception)
    assert issubclass(TagExprUnknown, Exception)
    assert issubclass(TagExprEmpty, Exception)


# --- parse_include -------------------------------------------------------


def test_parse_include_single_tag():
    assert parse_include("airflow") == Include("airflow")


def test_parse_include_and():
    assert parse_include("a&b") == And(Include("a"), Include("b"))


def test_parse_include_or():
    assert parse_include("a|b") == Or(Include("a"), Include("b"))


def test_parse_include_precedence_and_binds_tighter():
    # a&b|c  ==  (a&b)|c
    assert parse_include("a&b|c") == Or(And(Include("a"), Include("b")), Include("c"))


def test_parse_include_three_atom_and():
    assert parse_include("a&b&c") == And(And(Include("a"), Include("b")), Include("c"))


def test_parse_include_quoted_tag():
    # shlex keeps "airflow provider" as one token
    assert parse_include('"airflow provider"') == Include("airflow provider")


def test_parse_include_quoted_with_operator():
    assert parse_include('"airflow provider"&"machine learning"') == And(
        Include("airflow provider"), Include("machine learning")
    )


@pytest.mark.parametrize(
    "bad",
    [
        "&a",  # leading &
        "a&",  # trailing &
        "a|",  # trailing |
        "&",  # dangling
        "|",  # dangling
        "a&&b",  # consecutive operators
    ],
)
def test_parse_include_errors(bad):
    from lies.query.tag_expr import TagExprParseError

    with pytest.raises(TagExprParseError):
        parse_include(bad)


# --- parse_query_argv ----------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected_filter,expected_exclude,expected_query",
    [
        # No filter at all
        (["what is X?"], None, None, "what is X?"),
        # Single +tag
        (["+airflow", "what", "are", "DAGs?"], "airflow", None, "what are DAGs?"),
        # +tag&tag
        (
            ["+airflow&provider", "what", "connectors?"],
            "airflow&provider",
            None,
            "what connectors?",
        ),
        # +tag|tag (note: | inside argv is one token due to shell split)
        (
            ["+claude-code|claude-platform", "what", "is", "a", "hook?"],
            "claude-code|claude-platform",
            None,
            "what is a hook?",
        ),
        # +chain -exclude
        (
            ["+airflow&provider", "-amazon", "compare", "X", "and", "Y"],
            "airflow&provider",
            "amazon",
            "compare X and Y",
        ),
        # Quoted tag with space
        (
            ['+"airflow provider"', "what", "is", "X?"],
            '"airflow provider"',
            None,
            "what is X?",
        ),
        # +a +b — first is filter, second is query
        (["+a", "+b"], "a", None, "+b"),
        # +a -b -c — first is filter, -b consumed, -c is query
        (["+a", "-b", "-c"], "a", "b", "-c"),
        # -amazon without +chain
        (["-amazon", "what", "is", "S3?"], None, "amazon", "what is S3?"),
    ],
)
def test_parse_query_argv_happy(argv, expected_filter, expected_exclude, expected_query):
    # expected_filter as string is re-parsed to compare; we
    # compare on (question, include_ast.render() if include else None, exclude).
    from lies.query.tag_expr import _render_include  # see step 7

    question, include_ast, exclude = parse_query_argv(argv)
    assert question == expected_query
    if expected_filter is None:
        assert include_ast is None
    else:
        assert include_ast is not None
        assert _render_include(include_ast) == expected_filter
    assert exclude == expected_exclude


def test_parse_query_argv_with_exclude():
    argv = ["+airflow&provider", "-amazon", "compare", "X", "and", "Y"]
    question, include_ast, exclude = parse_query_argv(argv)
    assert question == "compare X and Y"
    assert exclude == "amazon"


def test_parse_query_argv_filter_no_question_errors():
    from lies.query.tag_expr import TagExprParseError

    with pytest.raises(TagExprParseError):
        parse_query_argv(["+airflow"])


@pytest.mark.parametrize("argv", [["+"], ["-"], ["+"], ["+a&"]])
def test_parse_query_argv_dangling_operator_errors(argv):
    with pytest.raises(TagExprParseError):
        parse_query_argv(argv + ["question"])


def test_parse_query_argv_dangling_operator_error_message():
    """The error message identifies the dangling operator explicitly."""
    from lies.query.tag_expr import TagExprParseError

    with pytest.raises(TagExprParseError) as excinfo:
        parse_query_argv(["+a&", "what", "is", "X?"])
    assert "dangling" in str(excinfo.value).lower()


def test_parse_query_argv_realistic_bash_quoted_tag():
    """Real bash strips outer quotes; internal space is preserved as one atom.

    `python script.py +"airflow provider" what is X?` produces
    argv = ["+airflow provider", "what", "is", "X?"]. The shell
    word-split on whitespace does NOT split inside a quoted
    segment; the parser must treat the argv element with
    internal whitespace as a single atom.
    """
    argv = ["+airflow provider", "what", "is", "X?"]
    question, include_ast, exclude = parse_query_argv(argv)
    assert question == "what is X?"
    assert include_ast == Include("airflow provider")
    assert exclude is None


def test_parse_query_argv_realistic_bash_quoted_tag_and_exclude():
    """Realistic bash: +"airflow provider" -amazon compare X and Y."""
    argv = ["+airflow provider", "-amazon", "compare", "X", "and", "Y"]
    question, include_ast, exclude = parse_query_argv(argv)
    assert question == "compare X and Y"
    assert include_ast == Include("airflow provider")
    assert exclude == "amazon"


def test_render_include_round_trip_multiatom_with_internal_space():
    """Rendering then re-parsing yields the same AST."""
    ast = Include("airflow provider")
    rendered = _render_include(ast)
    assert rendered == '"airflow provider"'
    assert parse_include(rendered) == ast


# --- resolve ---------------------------------------------------------------


def test_resolve_valid_tag():
    tree = parse_include("airflow")
    result = resolve(tree, available={"airflow", "amazon"})
    assert result.include == Include("airflow")
    assert result.exclude is None


def test_resolve_valid_and():
    tree = parse_include("airflow&provider")
    result = resolve(tree, available={"airflow", "provider", "amazon"})
    assert isinstance(result.include, And)


def test_resolve_unknown_tag_raises():
    tree = parse_include("nope")
    with pytest.raises(TagExprUnknown):
        resolve(tree, available={"airflow", "provider"})


def test_resolve_and_one_branch_unknown():
    tree = parse_include("airflow&nope")
    with pytest.raises(TagExprUnknown):
        resolve(tree, available={"airflow", "provider"})


def test_resolve_or_one_branch_unknown():
    tree = parse_include("airflow|nope")
    with pytest.raises(TagExprUnknown):
        resolve(tree, available={"airflow", "provider"})


def test_resolve_nested_unknown_in_left_subtree():
    # Re-test via parse_tokens instead
    from lies.query.tag_expr import parse_tokens

    tree = parse_tokens(["airflow", "&", "nope", "|", "provider"])
    with pytest.raises(TagExprUnknown):
        resolve(tree, available={"airflow", "provider"})


def test_resolve_valid_or():
    tree = parse_include("airflow|provider")
    result = resolve(tree, available={"airflow", "provider", "amazon"})
    assert isinstance(result.include, Or)
    # OR is structurally symmetric with AND; this pins it.
