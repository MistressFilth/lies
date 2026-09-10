"""Tag-filter expression AST + parser + resolver.

Mirrors the predecessor's `ROUTING.md` §"Tag-filter syntax". The
expression has at most one `+` chain (atoms joined by `&` / `|`,
where `&` binds tighter) and at most one `-` atom. The exclude
flattens to a single string on `ResolvedTagFilter`; only the
include is modeled as a tree.

The CLI / MCP surface converges here: a single parser, a single
resolver, a single AST. See the canonical spec at
`<notes-root>/superpowers/specs/2026-09-09-bundle-c-tag-filter-design.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


class TagExpr:
    """Base class for the tag-filter AST.

    Concrete variants: `Include`, `And`, `Or`. The exclude lives
    on `ResolvedTagFilter.exclude` as a flat string; it is not
    part of the tree (the grammar restricts to at most one
    `-` atom).
    """


@dataclass(frozen=True)
class Include(TagExpr):
    """A single `+tag` atom in the include chain."""

    tag: str


@dataclass(frozen=True)
class And(TagExpr):
    """Logical AND of two include expressions. `&` binds tighter than `|`."""

    left: TagExpr
    right: TagExpr


@dataclass(frozen=True)
class Or(TagExpr):
    """Logical OR of two include expressions."""

    left: TagExpr
    right: TagExpr


@dataclass(frozen=True)
class ResolvedTagFilter:
    """Flattened form the retriever consumes.

    The include expression is the validated AST tree (may be
    `Include`, `And`, `Or`, or `None` if no filter was given).
    The exclude is a single tag string or `None` — the resolver
    does not validate it against the registry; the retriever
    does that when it resolves the filter against the collection
    set.
    """

    include: TagExpr | None = None
    exclude: str | None = None


class TagExprParseError(Exception):
    """Raised when the filter expression has bad grammar.

    Carries the offending input verbatim. No fuzzy match,
    no did-you-mean. The operator owns the spelling.
    """

    def __init__(self, message: str, *, position: int = -1) -> None:
        super().__init__(message)
        self.position = position


class TagExprUnknown(Exception):
    """Raised when the filter references a tag not in the registry."""

    def __init__(self, tag: str) -> None:
        super().__init__(f"unknown tag: {tag}")
        self.tag = tag


class TagExprEmpty(Exception):
    """Raised when the include chain is present but has no atoms."""
