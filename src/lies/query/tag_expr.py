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

    Both halves of the F15 grammar share the same `TagExpr` shape:
    `include` and `exclude` are validated independently by the same
    recursive `resolve()` walk and evaluated by the same per-collection
    matcher. The historical split — flat string on `exclude`, AST on
    `include` — was retired in Task 3 / f15-exclude-compound because
    the F15 grammar accepts compound excludes (``-c:foo&c:bar``,
    ``-c:foo|c:bar``) that no longer fit a single atom.

    Each ``Include`` atom carries its own ``qualifier`` (default
    ``None`` — same ``t:`` / ``c:`` semantics as the include side).

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
    # wordchars so tag names like `claude-code` stay whole. `:` is in
    # wordchars so `c:opencode` / `t:airflow` qualifier prefixes stay
    # attached to their atom (otherwise `c:opencode` would tokenize
    # as `c`, `:`, `opencode`). Mirrors `parse()` which also adds
    # both `-` and `:` to wordchars.
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
    """Walk argv, peel off optional `+` chain and optional `-` chain.

    The argv here is the post-Typer positional list. Typer has
    already consumed `--flag` values; what remains is positional.

    Rules:
      - If the first token starts with `+`, the chain extends
        across subsequent tokens while the previous token ended
        in `&` or `|`.
      - If the next token (immediately after the include chain)
        starts with `-`, peel a compound ``-`` chain that mirrors
        the include path's split + extend model: each ``-`` atom
        may carry a ``t:`` / ``c:`` qualifier prefix (F15), and
        the chain extends while the next argv token starts with
        ``&`` / ``|`` (the operator-then-atom case) or the previous
        chain token ended with ``&`` / ``|``. ``&`` binds tighter
        than ``|`` (same precedence as the include path).
      - The remaining tokens join with single spaces to form
        the question.

    Returns ``(question, include_ast, exclude_ast, None)``. The
    fourth element is always ``None`` because the qualifier lives
    on each ``Include`` atom in the tree; callers that need a
    flat string can render the include AST via :func:`_render_include`
    (qualified form, includes the prefix). The exclude AST is now
    threaded through to consumers verbatim — no body-only renderer
    is needed because both halves of the F15 grammar share the
    same AST shape (Task 3 / f15-exclude-compound retired the
    legacy flat-string ``ResolvedTagFilter.exclude`` contract).

    Raises TagExprParseError on grammar errors.
    """
    if not argv:
        raise TagExprParseError("query argv is empty")

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

    # Peel the optional `-` chain. The chain mirrors the include
    # path's split + extend model: the first argv token's body
    # (after stripping the leading ``-``) is one atom; the chain
    # extends across subsequent argv tokens that START with ``&``
    # / ``|`` (the operator-then-atom case from realistic shell
    # splitting). Embedded operators inside the first argv token
    # (e.g. ``-c:foo&c:bar`` as one shell word) flow through the
    # same ``_split_argv_token_for_ops`` helper the include path
    # uses, so the ``&`` / ``|`` split semantics are symmetric
    # across both chains. Each atom may carry a ``t:`` / ``c:``
    # qualifier prefix (F15); bad prefixes raise
    # ``TagExprParseError`` via :func:`check_qualifier` here,
    # mirroring the include chain's parse_atom error path.
    if i < len(argv) and argv[i].startswith("-"):
        body = argv[i][1:]
        if not body:
            raise TagExprParseError("'-' without atom", position=i)
        check_qualifier(body, position=i)
        exclude_tokens: list[str] = [body]
        i += 1
        while i < len(argv) and argv[i].startswith(("&", "|")):
            # Continuation token: starts with the binary operator.
            # The operator is consumed here (``_split_argv_token_for_ops``
            # will re-emit it as its own flat-list element); the rest
            # of the token is the next atom and must validate as a
            # ``t:`` / ``c:`` qualified atom. Append the full token
            # (including the leading operator) so the per-token
            # operator split produces a clean flat list with one
            # operator between atoms.
            tok = argv[i]
            atom_body = tok[1:]
            if not atom_body:
                raise TagExprParseError(
                    f"dangling operator at end of exclude chain: {tok!r}",
                    position=i,
                )
            check_qualifier(atom_body, position=i)
            exclude_tokens.append(tok)
            i += 1
        # If the chain ended with `&` or `|`, peel that dangling op
        # into a parse error before going further (mirrors the
        # include path's post-loop dangling check).
        if exclude_tokens and exclude_tokens[-1].endswith(("&", "|")):
            raise TagExprParseError(
                f"dangling operator at end of exclude chain: {exclude_tokens[-1]!r}",
                position=len(argv) - 1,
            )
        # Flatten via the same argv-split helper used by the
        # include path: operators outside any quoted segment
        # become their own tokens; the leading operator on a
        # continuation argv token flows through to the flat list
        # exactly once. parse_tokens then builds the AST.
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

    Every ``Include(tag)`` — on either half — requires ``tag`` in
    ``available``; the first unknown raises :class:`TagExprUnknown`
    with the exact spelling. ``And`` / ``Or`` are recursive — both
    children must validate.

    ``expr`` may be ``None`` when the operator passed only an exclude
    chain (no ``+`` prefix). The CLI's argv parser splits the two
    halves independently and threads each one through here; the
    orchestrator's include-only path leaves ``exclude`` at its
    ``None`` default.

    Task 3 / f15-exclude-compound: the exclude half shares the same
    AST shape as the include half, so the validation walk is the
    same recursive ``resolve`` over the exclude tree. Callers that
    pre-split the two halves (CLI's explicit form, MCP's
    ``ask_question``) thread the exclude tree through ``exclude=``
    here; callers that only have the include AST leave it ``None``
    (default).

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
    """Evaluate one Include atom against one Collection.

    Dispatches on ``include.qualifier``:
        - ``"c"``: strict collection-name match (``coll.name == include.tag``).
        - ``"t"`` or ``None``: tag-or-name alias (``include.tag ∈ coll.tags ∪ {coll.name}``).

    Accepts the legacy wiki-yaml Collection shape and the library-first
    :class:`lies.library.registry.LibraryCollectionMeta` interchangeably.
    Both expose ``name`` and ``tags``, the only two attributes the
    resolver reads; the library model is the canonical source going
    forward and the wiki-yaml shape is legacy.

    Used by the retriever's ``_collections_matching`` only. Validation
    (``tag ∈ available``) lives in :func:`resolve`.
    """
    if include.qualifier == "c":
        return coll.name == include.tag
    return include.tag in (set(coll.tags) | {coll.name})


def exclude_matches(
    coll: "Collection | LibraryCollectionMeta",
    exclude: TagExpr,
) -> bool:
    """Evaluate the exclude AST against one Collection.

    Mirror of the include-side recursion in
    :func:`lies.query.synthesizer._eval_include` — walks the
    exclude tree (``Include`` / ``And`` / ``Or``) and dispatches
    every leaf via :func:`atom_matches`. ``And`` short-circuits on
    the first non-match; ``Or`` short-circuits on the first match.

    Task 3 / f15-exclude-compound replaced the historical flat-string
    ``_exclude_atom_matches`` helper because compound excludes
    (``-c:foo&c:bar``, ``-c:foo|c:bar``) need a tree walk, not a
    single-atom match. The retriever's :func:`_collections_matching`
    calls this once per collection per query.

    Accepts the legacy wiki-yaml :class:`Collection` and the
    library-first :class:`LibraryCollectionMeta` interchangeably —
    see :func:`atom_matches` for the structural contract.
    """
    if isinstance(exclude, Include):
        return atom_matches(coll, exclude)
    if isinstance(exclude, And):
        return exclude_matches(coll, exclude.left) and exclude_matches(coll, exclude.right)
    if isinstance(exclude, Or):
        return exclude_matches(coll, exclude.left) or exclude_matches(coll, exclude.right)
    raise TypeError(f"unexpected node type: {type(exclude).__name__}")
