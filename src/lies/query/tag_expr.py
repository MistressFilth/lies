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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from lies.library.record import LibraryCollectionConfig as Collection
    from lies.library.registry import LibraryCollectionMeta


class TagExpr:
    """Base class for the tag-filter AST.

    Concrete variants: `Include`, `And`, `Or`. The include and the
    exclude share the same tree shape — both halves of the F15
    grammar parse to the same AST, both halves are validated by the
    same recursive `resolve()` walk, and both halves are evaluated by
    the same `atom_matches` / `exclude_matches` recursion.
    """


@dataclass(frozen=True)
class Include(TagExpr):
    """A single `+tag` atom in the include chain.

    `qualifier`:
        - None (default): tag-or-name alias — `tag ∈ coll.tags ∪ {coll.name}`.
        - "t": explicit tag-or-name alias (same as None).
        - "c": strict collection-name match — `coll.name == tag`.
    """

    tag: str
    qualifier: Literal["t", "c"] | None = None


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

    Attributes:
        include: Validated include AST (Include / And / Or), or None
            when the operator passed no ``+`` chain.
        exclude: Validated exclude AST (Include / And / Or), or None
            when the operator passed no ``-`` chain.
    """

    include: TagExpr | None = None
    exclude: TagExpr | None = None


class TagExprParseError(Exception):
    """Raised when the filter expression has bad grammar.

    Carries the offending input verbatim. No fuzzy match,
    no did-you-mean. The operator owns the spelling.
    """

    def __init__(self, message: str, *, position: int = -1) -> None:
        super().__init__(message)
        self.position = position


class TagExprUnknown(Exception):
    """Raised when the filter references a tag not in the registry.

    Carries the offending spelling on ``tag`` and the addressable tag
    set on ``available`` so callers (CLI / MCP) can build a self-
    explanatory error that lists what tags DO exist. Without
    ``available`` the message would read ``unknown tag: opencode`` and
    leave the operator guessing which wiki, or which spelling, was the
    intended target — see regression test
    ``test_query_unknown_tag_lists_available_tags``.
    """

    def __init__(self, tag: str, *, available: frozenset[str] | set[str] = frozenset()) -> None:
        super().__init__(f"unknown tag: {tag}")
        self.tag = tag
        self.available: frozenset[str] = frozenset(available)


class TagExprEmpty(Exception):
    """Raised when the include chain is present but has no atoms."""


# ---------------------------------------------------------------------------
# Qualifier prefix strip
# ---------------------------------------------------------------------------

QUALIFIER_PREFIX_RE = re.compile(r"^(t|c):(.+)$")


def _split_qualifier(tag: str) -> tuple[Literal["t", "c"] | None, str]:
    """Strip the leading `t:` or `c:` qualifier from a tag string.

    Returns `(qualifier, tag_without_prefix)`. If no qualifier is present,
    returns `(None, tag)` unchanged. Bad qualifiers (e.g. `x:foo`) are NOT
    stripped here — the parser raises `TagExprParseError` for them.
    """
    m = QUALIFIER_PREFIX_RE.match(tag)
    if m is None:
        return None, tag
    qualifier: Literal["t", "c"] = m.group(1)  # ty: ignore[invalid-assignment]
    return qualifier, m.group(2)


def check_qualifier(raw: str, *, position: int) -> tuple[Literal["t", "c"] | None, str]:
    """Strip `t:` / `c:` prefix from a raw tag string; raise on bad qualifier."""
    if not raw or ":" not in raw:
        return None, raw
    if raw.startswith('"') and raw.endswith('"'):
        return None, raw
    prefix, body = raw.split(":", 1)
    if prefix in ("t", "c"):
        if not body:
            raise TagExprParseError(f"qualifier {prefix!r} without atom", position=position)
        return prefix, body
    raise TagExprParseError(f"unknown qualifier: {prefix!r}", position=position)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

import shlex  # noqa: E402  (module-level grouping with parser below)


def parse_tokens(tokens: list[str]) -> TagExpr:
    """Parse an already-tokenized include expression.

    Each token is a bare tag or a quoted tag (quotes already
    stripped by the token source). Operators (`&`, `|`) are
    separate tokens. Raises `TagExprParseError` on bad grammar.

    A token that arrives with literal surrounding double quotes
    (typical when the caller is argv-based and the shell did not
    strip them) is unwrapped here so callers don't have to.
    """
    if not tokens:
        raise TagExprEmpty("include expression has no atoms")

    # Recursive-descent: parseOr handles `|` (lowest precedence);
    # parseAtom handles `&` (higher precedence) and a single atom.
    pos = 0

    def parse_atom() -> TagExpr:
        nonlocal pos
        if pos >= len(tokens):
            raise TagExprParseError("expected atom, got end of input", position=pos)
        tok = tokens[pos]
        if tok in ("&", "|"):
            raise TagExprParseError(f"unexpected operator {tok!r}", position=pos)
        # Unwrap a literal "..." argv token. shlex already strips
        # quotes for the single-string `parse` path; for argv, the
        # shell hands us the raw token.
        if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
            tok = tok[1:-1]
        pos += 1
        qualifier, tag = _split_qualifier(tok)
        # Empty body after a known qualifier prefix. Mirror the exclude-side
        # ``check_qualifier`` and raise rather than letting ``c:""" slip through to
        # ``Include(tag='c:', qualifier=None)`` and surface as an unknown tag.
        # ``_split_qualifier`` requires ``.+``, so it returns ``(None, "c:")`` here.
        if qualifier is None and tag in ("c:", "t:"):
            raise TagExprParseError(f"qualifier {tag[:-1]!r} without atom", position=pos - 1)
        if qualifier is None and ":" in tag and not (tag.startswith('"') and tag.endswith('"')):
            # Has a colon but not a known qualifier; reject.
            prefix = tag.split(":", 1)[0]
            if prefix not in ("t", "c"):
                raise TagExprParseError(f"unknown qualifier: {prefix!r}", position=pos - 1)
        return Include(tag, qualifier=qualifier)

    def parse_and() -> TagExpr:
        nonlocal pos
        node = parse_atom()
        while pos < len(tokens) and tokens[pos] == "&":
            pos += 1
            right = parse_atom()
            node = And(node, right)
        return node

    def parse_or() -> TagExpr:
        nonlocal pos
        node = parse_and()
        while pos < len(tokens) and tokens[pos] == "|":
            pos += 1
            right = parse_and()
            node = Or(node, right)
        return node

    tree = parse_or()
    if pos != len(tokens):
        raise TagExprParseError(f"unexpected token {tokens[pos]!r}", position=pos)
    return tree


def parse(expr: str) -> TagExpr:
    """Parse a single-string include expression using shlex.

    Quoted segments (`"airflow provider"`) are preserved as one
    atom. Operators (`&`, `|`) must appear outside quotes.
    Raises `TagExprParseError` on bad grammar.
    """
    try:
        # posix shlex keeps ``&`` / ``|`` as metacharacters while still honoring
        # quoted segments; ``whitespace_split=True`` would flatten them into words.
        # ``-`` and ``:`` are added to ``wordchars`` so ``claude-code`` and
        # ``t:airflow`` each stay one token.
        lexer = shlex.shlex(expr, posix=True)
        lexer.wordchars += "-:"
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise TagExprParseError(f"unparseable expression: {exc}") from exc
    if not tokens:
        raise TagExprEmpty("include expression has no atoms")
    return parse_tokens(tokens)


# `parse_include` is an alias for `parse` — keep one name, expose both for
# spec compatibility.
parse_include = parse


def _render_include(node: TagExpr) -> str:
    """Render a tag-filter AST back to its include-expression string.

    Inverse of `parse`. Used by `parse_query_argv` callers that
    want to introspect the filter; not part of the public surface
    for general use.
    """
    if isinstance(node, Include):
        # Quote only when the tag would round-trip wrong: a leading ``-`` reads as an
        # exclude atom, whitespace splits the token, and ``&`` / ``|`` are operators.
        # Internal ``-`` round-trips fine (it is in ``wordchars``).
        tag = node.tag
        if tag.startswith("-"):
            rendered_tag = f'"{tag}"'
        elif any(c.isspace() or c in "&|" for c in tag):
            rendered_tag = f'"{tag}"'
        else:
            rendered_tag = tag
        prefix = f"{node.qualifier}:" if node.qualifier else ""
        return f"{prefix}{rendered_tag}"
    if isinstance(node, And):
        return f"{_render_include(node.left)}&{_render_include(node.right)}"
    if isinstance(node, Or):
        return f"{_render_include(node.left)}|{_render_include(node.right)}"
    raise TypeError(f"unexpected node type: {type(node).__name__}")


def _split_argv_token_for_ops(token: str) -> list[str]:
    """Split a single argv chain token into atoms + operators."""
    # Scan for an operator outside quoted segments. Operators
    # inside quotes (e.g. `"a&b"`) are part of the atom; the
    # outer atom wrapping decides what happens to them.
    in_quote = False
    has_op = False
    for c in token:
        if c == '"':
            in_quote = not in_quote
        elif not in_quote and c in "&|":
            has_op = True
            break
    if not has_op:
        return [token]
    # Operators detected with posix shlex: quoted segments stay one token,
    # ``&`` / ``|`` become their own tokens, and ``-`` / ``:`` in ``wordchars`` keep
    # ``claude-code`` and ``c:opencode`` whole. Mirrors ``parse()``.
    try:
        lexer = shlex.shlex(token, posix=True)
        lexer.wordchars += "-:"
        lexer.commenters = ""
        return list(lexer)
    except ValueError as exc:
        raise TagExprParseError(f"unparseable token {token!r}: {exc}") from exc


def parse_query_argv(
    argv: list[str],
) -> tuple[str, TagExpr | None, TagExpr | None, None]:
    """Walk argv, peel off optional `+` chain and optional `-` chain."""
    if not argv:
        raise TagExprParseError("query argv is empty")

    # Peel the optional `+` chain.
    if argv[0].startswith("+"):
        body = argv[0][1:]  # strip leading `+`
        chain_tokens = [body] if body else []
        i = 1
        # Surface a dangling operator on the first chain token BEFORE the extension
        # loop absorbs more argv tokens, so ``["+a&", "what", ...]`` raises instead
        # of silently consuming ``"what"`` and parsing wrong.
        if chain_tokens and chain_tokens[-1].endswith(("&", "|")):
            raise TagExprParseError(
                f"dangling operator at end of chain: {chain_tokens[-1]!r}",
                position=len(argv) - 1,
            )
        while i < len(argv):
            tok = argv[i]
            # Extend the chain when the previous token ended in an operator, or the
            # current one starts with ``&`` / ``|`` — the operator-then-atom case from
            # realistic shell splitting. The bare operator is appended as-is and
            # re-emitted by ``_split_argv_token_for_ops`` as its own flat element.
            if chain_tokens and chain_tokens[-1].endswith(("&", "|")):
                chain_tokens.append(tok)
                i += 1
                continue
            if tok in ("&", "|"):
                chain_tokens.append(tok)
                i += 1
                continue
            break
        # If the chain tokens ended with `&` or `|`, peel that dangling op
        # into a parse error before going further.
        if chain_tokens and chain_tokens[-1].endswith(("&", "|")):
            raise TagExprParseError(
                f"dangling operator at end of chain: {chain_tokens[-1]!r}",
                position=len(argv) - 1,
            )
        # Each argv chain token is one atom, possibly carrying internal whitespace
        # from bash or embedded operators from explicit quoting. The per-token split
        # keeps internal whitespace whole while separating outside ``&`` / ``|``.
        if not chain_tokens:
            raise TagExprParseError("'+' without atom", position=0)
        flat: list[str] = []
        for ct in chain_tokens:
            flat.extend(_split_argv_token_for_ops(ct))
        include_ast = parse_tokens(flat)
    else:
        include_ast = None
        i = 0

    # Peel the optional ``-`` chain with the include path's split + extend model:
    # the first token's body is one atom, the chain extends across tokens starting
    # with ``&`` / ``|``, and embedded operators go through the same helper, so
    # the semantics are symmetric. Atoms may carry ``t:`` / ``c:``; a bad one raises.
    if i < len(argv) and argv[i].startswith("-"):
        body = argv[i][1:]
        if not body:
            raise TagExprParseError("'-' without atom", position=i)
        check_qualifier(body, position=i)
        exclude_tokens: list[str] = [body]
        i += 1
        while i < len(argv):
            tok = argv[i]
            # Bare operator: append it and immediately absorb the next token as the
            # following atom. The operator must be followed by an atom or the chain is
            # dangling — realistic shell splits produce this shape whenever the operator
            # has surrounding whitespace.
            if tok in ("&", "|"):
                exclude_tokens.append(tok)
                i += 1
                if i >= len(argv):
                    raise TagExprParseError(
                        f"dangling operator at end of exclude chain: {tok!r}",
                        position=i - 1,
                    )
                next_tok = argv[i]
                if next_tok.startswith("-"):
                    raise TagExprParseError(
                        f"unexpected exclude prefix inside chain: {next_tok!r}",
                        position=i,
                    )
                check_qualifier(next_tok, position=i)
                exclude_tokens.append(next_tok)
                i += 1
                continue
            # Operator-prefixed atom (e.g. `&c:bar`).
            if tok.startswith(("&", "|")):
                atom_body = tok[1:]
                if not atom_body:
                    raise TagExprParseError(
                        f"dangling operator at end of exclude chain: {tok!r}",
                        position=i,
                    )
                check_qualifier(atom_body, position=i)
                exclude_tokens.append(tok)
                i += 1
                continue
            break
        # If the chain ended with `&` or `|`, peel that dangling op
        # into a parse error before going further (mirrors the
        # include path's post-loop dangling check).
        if exclude_tokens and exclude_tokens[-1].endswith(("&", "|")):
            raise TagExprParseError(
                f"dangling operator at end of exclude chain: {exclude_tokens[-1]!r}",
                position=len(argv) - 1,
            )
        # Flatten through the same argv-split helper as the include path: outside
        # operators become their own tokens and a continuation token's leading
        # operator flows through exactly once. ``parse_tokens`` then builds the AST.
        flat_exclude: list[str] = []
        for ct in exclude_tokens:
            flat_exclude.extend(_split_argv_token_for_ops(ct))
        exclude_ast = parse_tokens(flat_exclude)
    else:
        exclude_ast = None

    # Remaining tokens are the question.
    question_tokens = argv[i:]
    if include_ast is not None or exclude_ast is not None:
        if not question_tokens:
            raise TagExprParseError("filter present but no question")
    question = " ".join(question_tokens)
    return question, include_ast, exclude_ast, None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(
    expr: TagExpr | None,
    *,
    available: set[str],
    exclude: TagExpr | None = None,
) -> ResolvedTagFilter:
    """Validate the include (and optional exclude) AST against the available tag set.

    Returns:
        :class:`ResolvedTagFilter` carrying the validated include
        tree (and the validated exclude tree when one was supplied).
    """
    include = _resolve_tree(expr, available) if expr is not None else None
    exclude_tree = _resolve_tree(exclude, available) if exclude is not None else None
    return ResolvedTagFilter(include=include, exclude=exclude_tree)


def _resolve_tree(expr: TagExpr, available: set[str]) -> TagExpr:
    """Recursively validate one ``TagExpr`` tree against ``available``.

    Returns the validated tree unchanged (``Include`` atoms whose
    ``tag`` is in ``available``; ``And`` / ``Or`` whose both /
    either child validated). Raises :class:`TagExprUnknown` on the
    first unknown atom.

    Used by :func:`resolve` to validate both halves of a
    :class:`ResolvedTagFilter` with one recursion shape.
    """
    if isinstance(expr, Include):
        if expr.tag not in available:
            raise TagExprUnknown(expr.tag, available=available)
        return expr
    if isinstance(expr, And):
        return And(_resolve_tree(expr.left, available), _resolve_tree(expr.right, available))
    if isinstance(expr, Or):
        return Or(_resolve_tree(expr.left, available), _resolve_tree(expr.right, available))
    raise TypeError(f"unexpected node type: {type(expr).__name__}")


# ---------------------------------------------------------------------------
# Retriever helpers
# ---------------------------------------------------------------------------


def atom_matches(coll: "Collection | LibraryCollectionMeta", include: Include) -> bool:
    """Evaluate one Include atom against one Collection."""
    if include.qualifier == "c":
        return coll.name == include.tag
    return include.tag in (set(coll.tags) | {coll.name})


def exclude_matches(
    coll: "Collection | LibraryCollectionMeta",
    exclude: TagExpr,
) -> bool:
    """Evaluate the exclude AST against one Collection."""
    if isinstance(exclude, Include):
        return atom_matches(coll, exclude)
    if isinstance(exclude, And):
        return exclude_matches(coll, exclude.left) and exclude_matches(coll, exclude.right)
    if isinstance(exclude, Or):
        return exclude_matches(coll, exclude.left) or exclude_matches(coll, exclude.right)
    raise TypeError(f"unexpected node type: {type(exclude).__name__}")
