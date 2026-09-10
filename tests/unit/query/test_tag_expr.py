"""Unit tests for the tag-filter AST + exception surface."""

from __future__ import annotations

from lies.query.tag_expr import (
    And,
    Include,
    Or,
    ResolvedTagFilter,
    TagExprEmpty,
    TagExprParseError,
    TagExprUnknown,
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
