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
    from lies.collections.record import Collection


class TagExpr:
    """Base class for the tag-filter AST.

    Concrete variants: `Include`, `And`, `Or`. The exclude lives
    on `ResolvedTagFilter.exclude` as a flat string; it is not
    part of the tree (the grammar restricts to at most one
    `-` atom).
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

    include: the validated include AST tree (may be Include / And / Or, or None).
    exclude: at most one tag string, or None.
    exclude_qualifier:
        - None (default): tag-or-name alias.
        - "t": explicit alias.
        - "c": strict collection-name match.
    """

    include: TagExpr | None = None
    exclude: str | None = None
    exclude_qualifier: Literal["t", "c"] | None = None


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
    """Strip `t:` / `c:` prefix from a raw tag string; raise on bad qualifier.

    Public qualifier validator used by every surface (CLI argv, CLI
    explicit, MCP) to validate + split the qualifier from the tag body.
    Unifies the 8-line duplicate across three sites and makes
    empty-body qualifiers (`c:`, `t:`) raise `TagExprParseError`
    consistently.

    Returns `(qualifier, tag_without_prefix)`. Raises `TagExprParseError`
    with `position` on:
      - Known qualifier followed by empty body (`c:`)
      - Unknown qualifier prefix (`x:foo`)
    A bare tag (no `:`) returns `(None, raw)` unchanged.
    """
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
        # Empty body after a known qualifier prefix (`c:` / `t:`). Mirror
        # the exclude-side check in `check_qualifier`: surface as a parse
        # error rather than letting an empty body slip through to
        # Include(tag='c:', qualifier=None) and confuse the resolver with
        # "unknown tag: 'c:'". `_split_qualifier`'s regex requires `.+`
        # body chars, so it returns (None, "c:") for `c:` — detect that
        # case here.
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
        # NOTE: shlex's `whitespace_split=True` flattens `&` / `|` into
        # word characters and would parse `a&b` as a single token. The
        # default posix shlex treats `&` / `|` as metacharacters and
        # still honors quoted segments, which is what the grammar wants.
        # Hyphen is added to wordchars so tag names like `claude-code`
        # stay one token (shlex otherwise splits on `-`).
        # Colon is added to wordchars so `t:` / `c:` qualifier prefixes
        # stay attached to their atom (otherwise `t:airflow` would
        # tokenize as `t`, `:`, `airflow`). Quoted segments are still
        # treated as one token by shlex regardless of wordchars.
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
        # Quote only if the tag would otherwise round-trip wrong:
        # leading `-` would be read as the start of an exclude atom,
        # whitespace would split the token, and `&` / `|` are the
        # grammar's binary operators. Internal `-` (e.g. `claude-code`)
        # round-trips fine because shlex is configured with `-` in
        # wordchars.
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
    """Split a single argv chain token into atoms + operators.

    Each argv token is one shell word: shell quoting may have
    delivered a multi-word atom (e.g. realistic bash's
    `+airflow provider` — internal whitespace preserved as one
    token with no surrounding quotes) or an operator-bearing
    token (e.g. `airflow&provider` or
    `"airflow provider"&"machine learning"`).

    If the token has no `&` / `|` operator OUTSIDE any quoted
    segment, the whole token is one atom (internal whitespace
    is preserved). If an outside-quote operator exists, posix
    shlex splits operators out while keeping quoted segments
    intact — so `"airflow provider"&"machine learning"`
    tokenizes as `["airflow provider", "&", "machine learning"]`.
    """
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
    # Operators detected — split with posix shlex. Quoted segments
    # stay one token; `&` / `|` become their own tokens; `-` is in
    # wordchars so tag names like `claude-code` stay whole.
    try:
        lexer = shlex.shlex(token, posix=True)
        lexer.wordchars += "-"
        lexer.commenters = ""
        return list(lexer)
    except ValueError as exc:
        raise TagExprParseError(f"unparseable token {token!r}: {exc}") from exc


def parse_query_argv(
    argv: list[str],
) -> tuple[str, TagExpr | None, str | None, Literal["t", "c"] | None]:
    """Walk argv, peel off optional `+` chain and optional `-` atom.

    The argv here is the post-Typer positional list. Typer has
    already consumed `--flag` values; what remains is positional.

    Rules:
      - If the first token starts with `+`, the chain extends
        across subsequent tokens while the previous token ended
        in `&` or `|`.
      - If the next token (immediately after the chain) starts
        with `-`, it is the exclude atom. The atom may carry a
        ``t:`` / ``c:`` qualifier prefix (F15); the prefix is
        stripped here and returned as ``exclude_qualifier``.
      - The remaining tokens join with single spaces to form
        the question.

    Returns ``(question, include_ast, exclude_tag, exclude_qualifier)``.
    ``exclude_qualifier`` is ``None`` for an unqualified exclude
    (the ``t`` alias) or ``"t"`` / ``"c"`` for an explicit prefix.

    Raises TagExprParseError on grammar errors.
    """
    if not argv:
        raise TagExprParseError("query argv is empty")

    chain_tokens: list[str] = []
    exclude_tag: str | None = None
    exclude_qualifier: Literal["t", "c"] | None = None

    # Peel the optional `+` chain.
    if argv[0].startswith("+"):
        body = argv[0][1:]  # strip leading `+`
        chain_tokens = [body] if body else []
        i = 1
        # Surface a dangling operator on the very first chain
        # token BEFORE the extension loop absorbs additional argv
        # tokens into the chain. argv `["+a&", "what", ...]`
        # raises "dangling operator at end of chain" instead of
        # silently consuming "what" and parsing wrong.
        if chain_tokens and chain_tokens[-1].endswith(("&", "|")):
            raise TagExprParseError(
                f"dangling operator at end of chain: {chain_tokens[-1]!r}",
                position=len(argv) - 1,
            )
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
        # Parse the include chain. Each argv chain token is one
        # atom (which may carry internal whitespace from realistic
        # bash, or operators embedded in a quoted token from
        # explicit shell quoting). Per-token operator split keeps
        # internal whitespace whole while separating `&` / `|`
        # outside any quoted segment.
        if not chain_tokens:
            raise TagExprParseError("'+' without atom", position=0)
        flat: list[str] = []
        for ct in chain_tokens:
            flat.extend(_split_argv_token_for_ops(ct))
        include_ast = parse_tokens(flat)
    else:
        include_ast = None
        i = 0

    # Peel the optional single `-` atom. The atom may carry a
    # ``t:`` / ``c:`` qualifier prefix (F15). Bad qualifiers raise
    # ``TagExprParseError`` here, mirroring the include chain's
    # parse_atom error path.
    if i < len(argv) and argv[i].startswith("-"):
        body = argv[i][1:]
        if not body:
            raise TagExprParseError("'-' without atom", position=i)
        exclude_qualifier, exclude_tag = check_qualifier(body, position=i)
        i += 1

    # Remaining tokens are the question.
    question_tokens = argv[i:]
    if include_ast is not None or exclude_tag is not None:
        if not question_tokens:
            raise TagExprParseError("filter present but no question")
    question = " ".join(question_tokens)
    return question, include_ast, exclude_tag, exclude_qualifier


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(expr: TagExpr, *, available: set[str]) -> ResolvedTagFilter:
    """Validate the include AST against the available tag set.

    Every `Include(tag)` requires `tag` in `available`; the first
    unknown raises `TagExprUnknown` with the exact spelling.
    `And` / `Or` are recursive — both children must validate.
    The exclude lives on `ResolvedTagFilter.exclude`; this
    function does not validate it (the retriever does).

    Returns:
        `ResolvedTagFilter(include=validated_tree, exclude=None)`.
        Exclude is filled by the caller (`parse_query_argv` already
        extracted it; this function only handles the include AST).
    """
    if isinstance(expr, Include):
        if expr.tag not in available:
            raise TagExprUnknown(expr.tag)
        return ResolvedTagFilter(include=expr)
    if isinstance(expr, And):
        left = resolve(expr.left, available=available)
        right = resolve(expr.right, available=available)
        return ResolvedTagFilter(include=And(left.include, right.include))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    if isinstance(expr, Or):
        left = resolve(expr.left, available=available)
        right = resolve(expr.right, available=available)
        return ResolvedTagFilter(include=Or(left.include, right.include))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    raise TypeError(f"unexpected node type: {type(expr).__name__}")


# ---------------------------------------------------------------------------
# Retriever helpers
# ---------------------------------------------------------------------------


def atom_matches(coll: "Collection", include: Include) -> bool:
    """Evaluate one Include atom against one Collection.

    Dispatches on ``include.qualifier``:
        - ``"c"``: strict collection-name match (``coll.name == include.tag``).
        - ``"t"`` or ``None``: tag-or-name alias (``include.tag ∈ coll.tags ∪ {coll.name}``).

    Used by the retriever's ``_collections_matching`` only. Validation
    (``tag ∈ available``) lives in :func:`resolve`.
    """
    if include.qualifier == "c":
        return coll.name == include.tag
    return include.tag in (set(coll.tags) | {coll.name})


def _exclude_atom_matches(
    coll: "Collection",
    exclude: str,
    exclude_qualifier: Literal["t", "c"] | None,
) -> bool:
    """Evaluate the exclude atom against one Collection.

    Same dispatch as :func:`atom_matches` but for the flat exclude
    string field on :class:`ResolvedTagFilter`.
    """
    if exclude_qualifier == "c":
        return coll.name == exclude
    return exclude in (set(coll.tags) | {coll.name})
