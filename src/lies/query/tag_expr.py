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
        return Include(tok)

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
        # NOTE: shlex's `whitespace_split=True` flattens `&` / `|` into
        # word characters and would parse `a&b` as a single token. The
        # default posix shlex treats `&` / `|` as metacharacters and
        # still honors quoted segments, which is what the grammar wants.
        # Hyphen is added to wordchars so tag names like `claude-code`
        # stay one token (shlex otherwise splits on `-`).
        lexer = shlex.shlex(expr, posix=True)
        lexer.wordchars += "-"
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
        # Quote only if the tag would otherwise round-trip wrong:
        # leading `-` would be read as the start of an exclude atom,
        # whitespace would split the token, and `&` / `|` are the
        # grammar's binary operators. Internal `-` (e.g. `claude-code`)
        # round-trips fine because shlex is configured with `-` in
        # wordchars.
        tag = node.tag
        if tag.startswith("-"):
            return f'"{tag}"'
        if any(c.isspace() or c in "&|" for c in tag):
            return f'"{tag}"'
        return tag
    if isinstance(node, And):
        return f"{_render_include(node.left)}&{_render_include(node.right)}"
    if isinstance(node, Or):
        return f"{_render_include(node.left)}|{_render_include(node.right)}"
    raise TypeError(f"unexpected node type: {type(node).__name__}")


def parse_query_argv(
    argv: list[str],
) -> tuple[str, TagExpr | None, str | None]:
    """Walk argv, peel off optional `+` chain and optional `-` atom.

    The argv here is the post-Typer positional list. Typer has
    already consumed `--flag` values; what remains is positional.

    Rules:
      - If the first token starts with `+`, the chain extends
        across subsequent tokens while the previous token ended
        in `&` or `|`.
      - If the next token (immediately after the chain) starts
        with `-`, it is the exclude atom.
      - The remaining tokens join with single spaces to form
        the question.

    Returns (question, include_ast, exclude_tag).

    Raises TagExprParseError on grammar errors.
    """
    if not argv:
        raise TagExprParseError("query argv is empty")

    chain_tokens: list[str] = []
    exclude_tag: str | None = None

    # Peel the optional `+` chain.
    if argv[0].startswith("+"):
        body = argv[0][1:]  # strip leading `+`
        chain_tokens = [body] if body else []
        i = 1
        while i < len(argv):
            tok = argv[i]
            # Extend the chain if the previous chain token ended in & or |.
            if chain_tokens and chain_tokens[-1].endswith(("&", "|")):
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
        # Parse the include chain. Argv tokens are shell-split on
        # whitespace only, so a token like `"+airflow&provider"` arrives
        # as a single argv element with `&` embedded. Re-run shlex over
        # the joined chain so `&` / `|` separate into operators while
        # quoted segments stay intact.
        if not chain_tokens:
            raise TagExprParseError("'+' without atom", position=0)
        chain_str = " ".join(chain_tokens)
        try:
            re_lexer = shlex.shlex(chain_str, posix=True)
            re_lexer.wordchars += "-"
            re_lexer.commenters = ""
            chain_tokens = list(re_lexer)
        except ValueError as exc:
            raise TagExprParseError(f"unparseable chain: {exc}") from exc
        include_ast = parse_tokens(chain_tokens)
    else:
        include_ast = None
        i = 0

    # Peel the optional single `-` atom.
    if i < len(argv) and argv[i].startswith("-"):
        body = argv[i][1:]
        if not body:
            raise TagExprParseError("'-' without atom", position=i)
        exclude_tag = body
        i += 1

    # Remaining tokens are the question.
    question_tokens = argv[i:]
    if include_ast is not None or exclude_tag is not None:
        if not question_tokens:
            raise TagExprParseError("filter present but no question")
    question = " ".join(question_tokens)
    return question, include_ast, exclude_tag
