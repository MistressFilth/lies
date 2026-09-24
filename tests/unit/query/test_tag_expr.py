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
    # compare on (question, include_ast.render() if include else None,
    # exclude_ast.render() if exclude else None).
    from lies.query.tag_expr import _render_include  # see step 7

    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == expected_query
    if expected_filter is None:
        assert include_ast is None
    else:
        assert include_ast is not None
        assert _render_include(include_ast) == expected_filter
    if expected_exclude is None:
        assert exclude_ast is None
    else:
        assert exclude_ast is not None
        # The exclude half keeps its qualifier on each Include atom; render
        # the AST to compare against the expected flat-string spelling.
        assert _render_include(exclude_ast) == expected_exclude


def test_parse_query_argv_with_exclude():
    argv = ["+airflow&provider", "-amazon", "compare", "X", "and", "Y"]
    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == "compare X and Y"
    assert exclude_ast == Include("amazon")


def test_parse_query_argv_with_c_qualifier_exclude():
    """``-c:airflow`` keeps the qualifier on the Include atom."""
    argv = ["+t:airflow", "-c:airflow", "what", "is", "X?"]
    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == "what is X?"
    assert include_ast == Include("airflow", qualifier="t")
    assert exclude_ast == Include("airflow", qualifier="c")


def test_parse_query_argv_with_t_qualifier_exclude():
    """``-t:airflow`` keeps the explicit t: qualifier on the Include atom."""
    argv = ["+airflow", "-t:airflow", "what", "is", "X?"]
    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == "what is X?"
    assert include_ast == Include("airflow")
    assert exclude_ast == Include("airflow", qualifier="t")


def test_parse_query_argv_bad_exclude_qualifier_errors():
    """``-x:foo`` on the exclude atom is a parse error."""
    from lies.query.tag_expr import TagExprParseError

    with pytest.raises(TagExprParseError, match="unknown qualifier"):
        parse_query_argv(["+airflow", "-x:foo", "what", "is", "X?"])


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
    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == "what is X?"
    assert include_ast == Include("airflow provider")
    assert exclude_ast is None


def test_parse_query_argv_realistic_bash_quoted_tag_and_exclude():
    """Realistic bash: +"airflow provider" -amazon compare X and Y."""
    argv = ["+airflow provider", "-amazon", "compare", "X", "and", "Y"]
    question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    assert question == "compare X and Y"
    assert include_ast == Include("airflow provider")
    assert exclude_ast == Include("amazon")


def test_render_include_round_trip_multiatom_with_internal_space():
    """Rendering then re-parsing yields the same AST."""
    ast = Include("airflow provider")
    rendered = _render_include(ast)
    assert rendered == '"airflow provider"'
    assert parse_include(rendered) == ast


# --- F15 exclude compound chain (Task 2) --------------------------------


def test_parse_query_argv_exclude_and_chain():
    """``-c:foo&c:bar`` builds an And-tree on the exclude half.

    Mirrors the include chain's operator split: ``_split_argv_token_for_ops``
    breaks the embedded ``&`` outside any quoted segment, then ``parse_tokens``
    builds the tree. The fourth tuple element is always ``None`` because
    the qualifier lives on each ``Include`` atom.
    """
    argv = ["-c:foo&c:bar", "What is X?"]
    question, include_ast, exclude_ast, fourth = parse_query_argv(argv)
    assert question == "What is X?"
    assert include_ast is None
    assert exclude_ast == And(
        Include("foo", qualifier="c"),
        Include("bar", qualifier="c"),
    )
    assert fourth is None


def test_parse_query_argv_exclude_or_chain():
    """``-c:foo|c:bar`` builds an Or-tree on the exclude half.

    ``|`` is the lower-precedence binary operator; ``parse_tokens`` builds
    the ``Or`` node directly. Same qualifier-on-atom contract as And.
    """
    argv = ["-c:foo|c:bar", "What is X?"]
    question, include_ast, exclude_ast, fourth = parse_query_argv(argv)
    assert question == "What is X?"
    assert include_ast is None
    assert exclude_ast == Or(
        Include("foo", qualifier="c"),
        Include("bar", qualifier="c"),
    )
    assert fourth is None


def test_parse_query_argv_exclude_precedence_and_binds_tighter():
    """``-c:foo&c:bar|t:baz`` builds Or(And(...), Include) — & first.

    Mirrors the include chain's precedence: ``&`` binds tighter than ``|``,
    so the AST is ``Or(And(Include("foo", "c"), Include("bar", "c")), Include("baz", "t"))``.
    Regression for the case where the original exclude peel only took one
    atom and silently dropped the rest of the chain (session 82a266a9).
    """
    argv = ["-c:foo&c:bar|t:baz", "What is X?"]
    question, include_ast, exclude_ast, fourth = parse_query_argv(argv)
    assert question == "What is X?"
    assert include_ast is None
    assert exclude_ast == Or(
        And(
            Include("foo", qualifier="c"),
            Include("bar", qualifier="c"),
        ),
        Include("baz", qualifier="t"),
    )
    assert fourth is None


def test_parse_query_argv_exclude_chain_across_argv_tokens():
    """``-c:foo &c:bar`` (split across argv tokens) builds the And-tree.

    Realistic bash delivers operator-then-atom as a separate argv token
    (e.g. ``-c:foo '&c:bar' What?``); the loop absorbs it when the next
    token starts with ``&`` / ``|``. The flat list after splitting is
    ``["c:foo", "&", "c:bar"]`` and ``parse_tokens`` builds the And.
    """
    argv = ["-c:foo", "&c:bar", "What?"]
    question, include_ast, exclude_ast, fourth = parse_query_argv(argv)
    assert question == "What?"
    assert include_ast is None
    assert exclude_ast == And(
        Include("foo", qualifier="c"),
        Include("bar", qualifier="c"),
    )
    assert fourth is None


# --- F15 argv token split preserves c:/t: qualifier ---------------------


def test_split_argv_token_for_ops_keeps_qualifier_attached():
    """`c:opencode|c:claude_platform` shlex-splits on `|` only; qualifier
    colon stays glued to its atom so downstream parse_tokens sees a clean
    token list (`["c:opencode", "|", "c:claude_platform"]`).

    Regression for session 82a266a9 line 61: a slash command tokenized
    `+c:opencode|c:claude_platform` as one argv token. Without `:` in
    wordchars, shlex split it into `["c", ":", "opencode", ...]` and
    parse_tokens choked on the leaked `":"` token.
    """
    from lies.query.tag_expr import _split_argv_token_for_ops, parse_tokens

    tokens = _split_argv_token_for_ops("c:opencode|c:claude_platform")
    assert tokens == ["c:opencode", "|", "c:claude_platform"]
    assert parse_tokens(tokens) == Or(
        Include("opencode", qualifier="c"),
        Include("claude_platform", qualifier="c"),
    )


def test_split_argv_token_for_ops_no_op_passthrough():
    """A token with no `&` / `|` outside quotes is one atom (whitespace intact)."""
    from lies.query.tag_expr import _split_argv_token_for_ops

    assert _split_argv_token_for_ops("c:opencode") == ["c:opencode"]
    assert _split_argv_token_for_ops('"airflow provider"') == ['"airflow provider"']


def test_split_argv_token_for_ops_keeps_t_qualifier_attached():
    """t: qualifier round-trips through the argv split path."""
    from lies.query.tag_expr import _split_argv_token_for_ops, parse_tokens

    tokens = _split_argv_token_for_ops("t:airflow|t:provider")
    assert tokens == ["t:airflow", "|", "t:provider"]
    assert parse_tokens(tokens) == Or(
        Include("airflow", qualifier="t"),
        Include("provider", qualifier="t"),
    )


def test_split_argv_token_for_ops_and_with_qualifier():
    """`&` keeps qualifier attached too, mirroring `|` behavior."""
    from lies.query.tag_expr import _split_argv_token_for_ops, parse_tokens

    tokens = _split_argv_token_for_ops("c:opencode&t:provider")
    assert tokens == ["c:opencode", "&", "t:provider"]
    assert parse_tokens(tokens) == And(
        Include("opencode", qualifier="c"),
        Include("provider", qualifier="t"),
    )


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


# --- F15 t:/c: qualifier prefix ------------------------------------------


def test_include_node_default_qualifier_none():
    node = Include("airflow")
    assert node.qualifier is None


def test_include_node_with_t_qualifier():
    node = Include("airflow", qualifier="t")
    assert node.qualifier == "t"


def test_include_node_with_c_qualifier():
    node = Include("airflow", qualifier="c")
    assert node.qualifier == "c"


def test_split_qualifier_t():
    from lies.query.tag_expr import _split_qualifier

    assert _split_qualifier("t:airflow") == ("t", "airflow")


def test_split_qualifier_c():
    from lies.query.tag_expr import _split_qualifier

    assert _split_qualifier("c:airflow") == ("c", "airflow")


def test_split_qualifier_none():
    from lies.query.tag_expr import _split_qualifier

    assert _split_qualifier("airflow") == (None, "airflow")


def test_split_qualifier_with_hyphen():
    """Qualifier strip works on names that already contain '-'."""
    from lies.query.tag_expr import _split_qualifier

    assert _split_qualifier("c:claude-code") == ("c", "claude-code")


def test_split_qualifier_with_quoted():
    """Strip works on quoted-tag-with-space; quotes preserved."""
    from lies.query.tag_expr import _split_qualifier

    assert _split_qualifier('c:"airflow provider"') == ("c", '"airflow provider"')


def test_split_qualifier_bad_qualifier():
    """Bad qualifier 'x:' is NOT matched by the prefix regex (returns None for tag)."""
    from lies.query.tag_expr import _split_qualifier

    # Regex only matches 't:' or 'c:'. 'x:foo' falls through as no qualifier.
    assert _split_qualifier("x:foo") == (None, "x:foo")


def test_parse_include_t_qualifier():
    from lies.query.tag_expr import parse_include

    assert parse_include("t:airflow") == Include("airflow", qualifier="t")


def test_parse_include_c_qualifier():
    from lies.query.tag_expr import parse_include

    assert parse_include("c:airflow") == Include("airflow", qualifier="c")


def test_parse_include_quoted_c_qualifier():
    from lies.query.tag_expr import parse_include

    assert parse_include('c:"airflow provider"') == Include("airflow provider", qualifier="c")


def test_parse_include_t_and_c_mixed_chain():
    """t:airflow & c:provider parses with mixed qualifiers."""
    from lies.query.tag_expr import And, parse_include

    assert parse_include("t:airflow&c:provider") == And(
        Include("airflow", qualifier="t"),
        Include("provider", qualifier="c"),
    )


def test_parse_include_bad_qualifier_errors():
    """x:foo is not a known qualifier — parser raises TagExprParseError."""
    from lies.query.tag_expr import TagExprParseError, parse_include

    with pytest.raises(TagExprParseError):
        parse_include("x:foo")


def test_render_include_emits_qualifier():
    """_render_include emits 'c:' prefix when qualifier set."""
    from lies.query.tag_expr import _render_include

    assert _render_include(Include("airflow", qualifier="c")) == "c:airflow"
    assert _render_include(Include("airflow", qualifier="t")) == "t:airflow"


def test_round_trip_with_qualifier():
    """parse -> render -> re-parse -> same AST."""
    from lies.query.tag_expr import _render_include, parse_include

    original = parse_include("t:airflow&c:provider")
    rendered = _render_include(original)
    reparsed = parse_include(rendered)
    assert reparsed == original


def test_resolved_tag_filter_exclude_carries_ast():
    """Task 3: ``ResolvedTagFilter.exclude`` is a ``TagExpr | None`` AST.

    The flat-string ``exclude`` field was retired in favor of the
    same ``TagExpr`` AST shape the include side already used, so
    compound excludes (``-c:foo&c:bar``, ``-c:foo|c:bar``) thread
    through unchanged. The historical ``exclude_qualifier`` field
    is gone — the qualifier now lives on each ``Include`` atom in
    the tree.
    """
    exclude_tree = Or(Include("foo", qualifier="c"), Include("bar", qualifier="c"))
    f = ResolvedTagFilter(include=None, exclude=exclude_tree)
    assert f.exclude == exclude_tree
    # The historical ``exclude_qualifier`` attribute is gone — the
    # qualifier lives on each Include atom now.
    assert not hasattr(f, "exclude_qualifier")


def test_resolved_tag_filter_exclude_and_tree():
    """Compound ``-c:foo&c:bar`` carries the And-tree on exclude."""
    exclude_tree = And(Include("foo", qualifier="c"), Include("bar", qualifier="c"))
    f = ResolvedTagFilter(include=Include("airflow"), exclude=exclude_tree)
    assert f.exclude == exclude_tree
    assert f.include == Include("airflow")


def test_resolve_validates_exclude_tree():
    """``resolve(include, exclude=, available=)`` validates the exclude tree too.

    Both halves of the F15 grammar share the same recursive
    validation walk. An unknown atom on the exclude side raises
    ``TagExprUnknown`` just like the include side does.
    """
    include_tree = parse_include("airflow")
    exclude_tree = parse_include("amazon")  # available
    result = resolve(
        include_tree,
        exclude=exclude_tree,
        available={"airflow", "amazon"},
    )
    assert result.include == Include("airflow")
    assert result.exclude == Include("amazon")


def test_resolve_exclude_unknown_atom_errors():
    """An unknown exclude atom raises ``TagExprUnknown``."""
    include_tree = parse_include("airflow")
    exclude_tree = parse_include("nope")
    with pytest.raises(TagExprUnknown):
        resolve(
            include_tree,
            exclude=exclude_tree,
            available={"airflow", "amazon"},
        )


def test_resolve_exclude_compound_validates_each_atom():
    """An And-tree on exclude validates both leaves recursively."""
    include_tree = parse_include("airflow")
    exclude_tree = And(Include("foo", qualifier="c"), Include("bar", qualifier="c"))
    result = resolve(
        include_tree,
        exclude=exclude_tree,
        available={"airflow", "foo", "bar"},
    )
    assert result.exclude == exclude_tree


# --- F15 atom_matches / exclude_matches helpers ---------------------------


def _make_collection(name: str, tags: list[str]):
    """Minimal Collection helper for atom_matches unit tests."""
    from datetime import datetime, timezone

    from lies.library.record import LibraryCollectionConfig as Collection

    return Collection(
        name=name,
        source="https://example.com",
        tags=tags,
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        config={},
    )


def test_atom_matches_c_qualifier_matches_collection_name():
    """c:foo matches only collections named foo (NOT collections tagged foo)."""
    from lies.query.tag_expr import atom_matches

    coll = _make_collection(name="airflow", tags=["provider"])
    assert atom_matches(coll, Include("airflow", qualifier="c")) is True
    # A collection named cnn with airflow in its tags does NOT match c:airflow.
    other = _make_collection(name="cnn", tags=["airflow", "news"])
    assert atom_matches(other, Include("airflow", qualifier="c")) is False


def test_atom_matches_c_qualifier_does_not_match_by_tag():
    """c:foo does NOT match a collection that has foo in tags but a different name."""
    from lies.query.tag_expr import atom_matches

    other = _make_collection(name="cnn", tags=["airflow", "news"])
    assert atom_matches(other, Include("airflow", qualifier="c")) is False


def test_atom_matches_t_qualifier_matches_tag_or_name():
    """t:foo matches collections named foo OR with foo in tags."""
    from lies.query.tag_expr import atom_matches

    airflow = _make_collection(name="airflow", tags=["provider"])
    cnn = _make_collection(name="cnn", tags=["airflow", "news"])
    assert atom_matches(airflow, Include("airflow", qualifier="t")) is True
    assert atom_matches(cnn, Include("airflow", qualifier="t")) is True


def test_atom_matches_no_qualifier_aliases_t():
    """No qualifier = same as 't' (today's implicit-self-tag behavior)."""
    from lies.query.tag_expr import atom_matches

    airflow = _make_collection(name="airflow", tags=["provider"])
    cnn = _make_collection(name="cnn", tags=["airflow", "news"])
    # No-qualifier Include matches the same set as 't' qualifier.
    assert atom_matches(airflow, Include("airflow")) is True
    assert atom_matches(cnn, Include("airflow")) is True


def test_atom_matches_c_does_not_match_unrelated():
    """c:foo does not match collections that don't have foo as name."""
    from lies.query.tag_expr import atom_matches

    other = _make_collection(name="spark", tags=["provider"])
    assert atom_matches(other, Include("airflow", qualifier="c")) is False


def test_exclude_matches_c_strict():
    """``exclude_matches`` with a ``c``-qualified atom matches only the named collection.

    Task 3 / f15-exclude-compound: the legacy single-atom
    ``_exclude_atom_matches`` shim was replaced by a recursive
    ``exclude_matches(coll, tree)`` walker that dispatches every leaf
    through ``atom_matches`` so compound excludes
    (``-c:foo&c:bar``, ``-c:foo|c:bar``) drop on the same dispatcher.
    A single-atom ``Include(..., qualifier="c")`` is the simplest tree
    the walker accepts; it must behave identically to the historical
    ``_exclude_atom_matches(coll, "foo", "c")``.
    """
    from lies.query.tag_expr import exclude_matches

    airflow = _make_collection(name="airflow", tags=["provider"])
    cnn = _make_collection(name="cnn", tags=["airflow", "news"])
    assert exclude_matches(airflow, Include("airflow", qualifier="c")) is True
    assert exclude_matches(cnn, Include("airflow", qualifier="c")) is False


def test_exclude_matches_no_qualifier_aliases_t():
    """``exclude_matches`` with no qualifier (or explicit ``t``) aliases t: semantics.

    Mirrors ``test_atom_matches_no_qualifier_aliases_t`` on the include
    side. A bare ``Include("foo")`` matches collections named ``foo``
    OR tagged ``foo``; an explicit ``Include("foo", qualifier="t")``
    behaves identically (same dispatch).
    """
    from lies.query.tag_expr import exclude_matches

    airflow = _make_collection(name="airflow", tags=["provider"])
    cnn = _make_collection(name="cnn", tags=["airflow", "news"])
    assert exclude_matches(airflow, Include("airflow")) is True
    assert exclude_matches(cnn, Include("airflow")) is True
    assert exclude_matches(airflow, Include("airflow", qualifier="t")) is True
    assert exclude_matches(cnn, Include("airflow", qualifier="t")) is True


def test_exclude_matches_t_strict():
    """``exclude_matches`` with explicit ``t`` qualifier matches name-or-tag.

    Same dispatch as the no-qualifier form (``atom_matches`` treats
    ``None`` and ``"t"`` identically). A collection named ``foo`` and
    a collection tagged ``foo`` both match.
    """
    from lies.query.tag_expr import exclude_matches

    airflow = _make_collection(name="airflow", tags=["provider"])
    cnn = _make_collection(name="cnn", tags=["airflow", "news"])
    assert exclude_matches(airflow, Include("airflow", qualifier="t")) is True
    assert exclude_matches(cnn, Include("airflow", qualifier="t")) is True


# --- F15 check_qualifier helper (cross-surface unify) -------------------


def test_check_qualifier_bare_tag():
    """No colon → no qualifier."""
    from lies.query.tag_expr import check_qualifier

    assert check_qualifier("airflow", position=0) == (None, "airflow")


def test_check_qualifier_t():
    from lies.query.tag_expr import check_qualifier

    assert check_qualifier("t:airflow", position=0) == ("t", "airflow")


def test_check_qualifier_c():
    from lies.query.tag_expr import check_qualifier

    assert check_qualifier("c:airflow", position=0) == ("c", "airflow")


def test_check_qualifier_empty_body():
    """`c:` (qualifier, no atom) → TagExprParseError."""
    from lies.query.tag_expr import TagExprParseError, check_qualifier

    with pytest.raises(TagExprParseError) as exc:
        check_qualifier("c:", position=0)
    assert "qualifier" in str(exc.value).lower() or "atom" in str(exc.value).lower()


def test_check_qualifier_bad_prefix():
    """`x:foo` → TagExprParseError('unknown qualifier')."""
    from lies.query.tag_expr import TagExprParseError, check_qualifier

    with pytest.raises(TagExprParseError) as exc:
        check_qualifier("x:foo", position=0)
    assert "unknown qualifier" in str(exc.value)


def test_check_qualifier_quoted_atom_preserved():
    """`"airflow provider"` (quoted, no prefix) → (None, '"airflow provider"')."""
    from lies.query.tag_expr import check_qualifier

    assert check_qualifier('"airflow provider"', position=0) == (
        None,
        '"airflow provider"',
    )


# --- F15 PR-review: empty-body include consistency ------------------------


def test_parse_tokens_empty_body_errors():
    """`c:` and `t:` (empty body) raise TagExprParseError on the include side,
    mirroring the exclude side's behavior in check_qualifier.

    Per PR #64 review — empty-body include consistency. Before this fix,
    parse_tokens silently produced Include(tag='c:', qualifier=None) which
    then confused the resolver with "unknown tag: 'c:'". The exclude side
    already raised cleanly via check_qualifier.
    """
    from lies.query.tag_expr import TagExprParseError, parse_query_argv, parse_tokens

    # Direct token-level access — the include-side parse_atom path.
    with pytest.raises(TagExprParseError):
        parse_tokens(["c:"])
    with pytest.raises(TagExprParseError):
        parse_tokens(["t:"])

    # Full argv path — include side surfaces the same error via parse_tokens.
    with pytest.raises(TagExprParseError):
        parse_query_argv(["+c:", "what", "is", "X?"])
    with pytest.raises(TagExprParseError):
        parse_query_argv(["+t:", "what", "is", "X?"])

    # Full argv path — exclude side has raised cleanly since the helper
    # was extracted; pin that here so the cross-surface symmetry holds.
    with pytest.raises(TagExprParseError):
        parse_query_argv(["+airflow", "-c:", "what", "is", "X?"])
    with pytest.raises(TagExprParseError):
        parse_query_argv(["+airflow", "-t:", "what", "is", "X?"])


# --- Bug G + H fix: bare-operator argv tokens ------------------------------
# Realistic shell splitting produces argv lists where the binary operator
# is its own token (surrounded by whitespace) rather than glued to the
# following atom. shlex.split("-c:foo & c:bar What?") yields
# ["-c:foo", "&", "c:bar", "What?"]. Before this fix, both the include
# path and the exclude path rejected this form with a dangling-operator
# parse error. The argv chain-peel loops now extend on either side
# (previous-token-ends-with-op OR current-token-is-op), and the exclude
# path's bare-operator branch absorbs the operator + the following atom
# together.


def test_parse_query_argv_include_bare_and_op():
    """`+c:foo & c:bar What?` argv-shlex-split: And-include of c:foo&c:bar."""
    from lies.query.tag_expr import parse_query_argv

    q, inc, exc, _ = parse_query_argv(["+c:foo", "&", "c:bar", "What", "is", "X?"])
    assert _render_include(inc) == "c:foo&c:bar"
    assert exc is None
    assert q == "What is X?"


def test_parse_query_argv_include_bare_or_op():
    """`+c:foo | c:bar What?`: Or-include of c:foo|c:bar."""
    from lies.query.tag_expr import parse_query_argv

    q, inc, exc, _ = parse_query_argv(["+c:foo", "|", "c:bar", "What", "is", "X?"])
    assert _render_include(inc) == "c:foo|c:bar"
    assert exc is None
    assert q == "What is X?"


def test_parse_query_argv_exclude_bare_and_op():
    """`-c:foo & c:bar What?`: And-exclude of c:foo&c:bar."""
    from lies.query.tag_expr import parse_query_argv

    q, inc, exc, _ = parse_query_argv(["-c:foo", "&", "c:bar", "What", "is", "X?"])
    assert _render_include(exc) == "c:foo&c:bar"
    assert inc is None
    assert q == "What is X?"


def test_parse_query_argv_exclude_bare_or_op():
    """`-c:foo | c:bar What?`: Or-exclude of c:foo|c:bar."""
    from lies.query.tag_expr import parse_query_argv

    q, inc, exc, _ = parse_query_argv(["-c:foo", "|", "c:bar", "What", "is", "X?"])
    assert _render_include(exc) == "c:foo|c:bar"
    assert inc is None
    assert q == "What is X?"


def test_parse_query_argv_dangling_bare_op_still_errors():
    """Trailing bare `&` with no following token raises dangling-operator.

    Ensures the new bare-operator branch doesn't swallow the dangling
    case — `-c:foo &` with no atom after `&` must still error.
    """
    from lies.query.tag_expr import TagExprParseError, parse_query_argv

    with pytest.raises(TagExprParseError) as exc:
        parse_query_argv(["-c:foo", "&"])
    assert "dangling" in str(exc.value).lower()
