"""FastMCP server exposing LIES as MCP tools, resources, and a prompt.

The server speaks stdio only (v1). Tools are thin wrappers around the
existing ``Orchestrator`` API; resources read raw markdown via the
``Wiki`` accessor interface. See
``docs/superpowers/specs/2026-07-27-lies-mcp-design.md`` for the full
surface.

All tools take a wiki ``name`` (or ``None`` to use the env-default) and
resolve it through :func:`lies.mcp.resolution.resolve_wiki` to a
:class:`Wiki` with role-routed XDG paths. ``init_wiki`` is the only
tool that creates a wiki; every other tool/resouce requires the wiki
to already be registered under ``$LIES_XDG_DATA_HOME``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

try:
    from fastmcp import Context
except ImportError:  # FastMCP < 3.4.5 with Context.elicit
    Context: type | None = None  # type: ignore[assignment,misc]

from lies import __version__, xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.errors import WikiAlreadyExists, WikiNotRegistered
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.mcp.instructions_loader import load_instructions, load_prompt
from lies.mcp.resolution import resolve_wiki
from lies.orchestrator import Orchestrator
from lies.query.tag_expr import (
    TagExpr,
    TagExprEmpty,
    TagExprParseError,
    TagExprUnknown,
    _render_include,
    parse,
    parse_query_argv,
    resolve,
)
from lies.wiki.layout import WikiLayout, copy_default_schema, git_init_initial
from lies.wiki.wiki import Wiki
from lies.mcp.grounding import ArchivistCoverageError, ground

mcp = FastMCP(
    "lies",
    instructions=load_instructions(),
)


# ---------------------------------------------------------------------------
# ground — F19 grounding digest (Task 3 of the grounding-archivist plan)
# ---------------------------------------------------------------------------


@mcp.tool(name="ground")
def mcp_ground(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    top_k: int = 3,
    name: str | None = None,
) -> dict:
    """Return a grounding digest for ``question``.

    Wraps :func:`lies.mcp.grounding.ground` (Task 2) at the MCP tool
    surface. The tag-filter dispatch (F15) and the F18 librarian run
    inside the helper; this wrapper only translates the result to a
    JSON-serializable ``dict`` for the FastMCP wire format.

    Args:
        question: The user's natural-language question.
        tag_expr: Body of a single include token (no leading sigil),
            e.g. ``"airflow&postgres"``. ``None`` for untagged.
        exclude_tags: NOT tags without leading sigil (wire-level
            ``list[str]``). The F15 grammar permits at most one entry;
            each entry is a **full F15 expression** and may carry
            compound operators (``c:foo&c:bar``, ``c:foo|c:bar``),
            qualifier prefixes (``t:`` / ``c:``), and quoted
            multi-word atoms. Parsed via :func:`parse` at this
            boundary into an ``Include`` / ``And`` / ``Or`` AST and
            validated against the registered collection set (Task 4
            / f15-exclude-compound) before threading to the librarian
            unchanged — the AST shape is the canonical post-Task-3
            surface end-to-end.
        top_k: Maximum excerpts requested from the librarian (clamped
            to ``[1, 10]``).
        name: Wiki name to resolve against. Defaults to the
            env-default (``LIES_WIKI_NAME`` or ``"default"``).
            Threaded to :func:`ground` so the librarian's tool
            closures bind to the named wiki's
            :class:`WikiMemoryService`.

    Returns:
        A JSON-serializable :class:`ArchivistDigest` carrying up to
        ``top_k`` citation snippets of ≤200 chars each.

    Raises:
        ToolError: when the F15 tag-filter dispatch cannot resolve the
            include expression (caller may retry untagged or surface).
    """
    # Translate the wire-level ``exclude_tags: list[str]`` to the
    # internal ``TagExpr | None`` AST the F18 librarian consumes
    # (Task 3 / f15-exclude-compound). The wire format keeps the
    # list-of-strings envelope so existing MCP callers don't break;
    # the conversion happens here at the boundary so the librarian
    # thread sees the AST shape end-to-end.
    #
    # Task 4 / f15-exclude-compound: each ``exclude_tags[i]`` is now a
    # **full F15 expression** (``c:foo&c:bar``, ``c:foo|c:bar``, etc.),
    # not a single atom. ``parse`` returns the same ``Include`` /
    # ``And`` / ``Or`` AST shape that the include half uses; ``resolve``
    # validates every atom against the registered tag set via
    # :func:`_collect_available_tags_mcp` — same surface as the
    # ``query`` / ``answer`` / ``ground`` include validators, so bare
    # tag atoms (``-harness``, ``-claude|cli``) validate consistently
    # end to end. Pre-v0.37.9 the exclude side used the
    # collection-names-only set, which rejected ``-harness`` even
    # though ``harness`` is a real tag on multiple library collections.
    exclude_expr: TagExpr | None = None
    if exclude_tags:
        try:
            exclude_expr = parse(exclude_tags[0])
            resolve(
                None,
                available=_collect_available_tags_mcp(None),
                exclude=exclude_expr,
            )
        except TagExprParseError as exc:
            raise ToolError(f"invalid tag expression: {exc}") from exc
        except TagExprEmpty as exc:
            raise ToolError(f"empty tag expression: {exc}") from exc
        except TagExprUnknown as exc:
            raise ToolError(format_unknown_tag_error(exc)) from exc

    try:
        digest = ground(
            question=question,
            tag_expr=tag_expr,
            exclude_expr=exclude_expr,
            top_k=top_k,
            wiki_name=name,
        )
    except ArchivistCoverageError as exc:
        raise ToolError(str(exc)) from exc
    # MCP tool handlers must return JSON-serializable structures.
    # ``ArchivistDigest`` is a frozen dataclass with only primitive +
    # nested dataclass fields; ``asdict`` flattens both layers without
    # a custom JSON encoder.
    return asdict(digest)


# ---------------------------------------------------------------------------
# init_wiki — bootstrap a new wiki
# ---------------------------------------------------------------------------


@mcp.tool
def init_wiki(name: str) -> dict[str, object]:
    """Initialize a new LIES wiki under ``$LIES_XDG_DATA_HOME/lies/<name>``.

    Creates the five XDG role directories (data, config, cache, state,
    runtime), copies the default schema to ``wiki.config_root/schema.md``,
    runs ``git init`` in ``wiki.data_root``, and makes an initial
    commit. The wiki name must not already be registered.

    Args:
        name: The wiki name. Validated against the same rules as ``Wiki``
            (no path separators, no leading dot, etc.).

    Returns:
        A dict with the wiki's name and the five role-root paths.
    """
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    if wiki.data_root.exists():
        raise WikiAlreadyExists(name, wiki.data_root)
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    WikiLayout(wiki.data_root).init()
    copy_default_schema(wiki.schema_path)
    git_init_initial(wiki.data_root)
    return {
        "name": wiki.name,
        "data_root": str(wiki.data_root),
        "config_root": str(wiki.config_root),
        "cache_root": str(wiki.cache_root),
        "state_root": str(wiki.state_root),
        "runtime_root": str(wiki.runtime_root),
        "version": __version__,
    }


# ---------------------------------------------------------------------------
# _confirm_destructive — destructive-flag elicitation (used by reindex)
# ---------------------------------------------------------------------------


class _ConfirmDestructive(BaseModel):
    """Schema for the destructive-flag elicit prompt.

    Mirrors ask's ``_ConfirmDestructive``
    (``ask/scripts/_server_helpers.py:228``).
    """

    model_config = ConfigDict(frozen=True)

    confirm: bool
    reason: str = ""


async def _confirm_destructive(ctx: Context | None, message: str) -> str | None:  # type: ignore[valid-type]
    """Prompt the user; return ``None`` to proceed or an error string to abort.

    Mirrors ask's ``_confirm_destructive``
    (``ask/scripts/_server_helpers.py:237``). Hosts that don't implement
    ``ctx.elicit`` raise on the call; we return a clear error string so
    the caller treats it as decline (no work runs). ``ctx`` may be
    ``None`` for programmatic callers; the ``try/except`` below catches
    the resulting ``AttributeError`` and surfaces the same "elicitation
    unavailable" error path the existing callers rely on.
    """
    try:
        result: Any = await ctx.elicit(  # ty: ignore[unresolved-attribute]
            message,
            response_type=cast(Any, _ConfirmDestructive),
        )
    except Exception as exc:  # pragma: no cover — host-dependent
        return f"elicitation unavailable: {exc}"

    if result.action != "accept":
        return "operation declined by user"
    if result.data is None or not result.data.confirm:
        return "operation declined by user"
    return None


# ---------------------------------------------------------------------------
# reindex — rebuild qmd index; gate destructive flags (F38)
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Reindex QMD collections. --cleanup/--all are destructive and "
        "elicit confirmation. Returns ReindexResult with reconciled/"
        "indexed/embedded/cleaned flags and errors list."
    ),
    annotations=ToolAnnotations(
        title="Reindex: rebuild QMD index",
        destructive_hint=True,
    ),
)
async def reindex(
    cleanup: bool = False,
    all_: bool = False,
    embed: bool = False,
    force: bool = False,
    reconcile: bool = False,
    name: str | None = None,
    ctx: Context | None = None,  # type: ignore[valid-type]
) -> dict[str, object]:
    """Rebuild qmd index; gate destructive flags.

    Mirrors ``lies reindex`` CLI. Destructive flags (``cleanup`` /
    ``all_``) elicit confirmation via ``_confirm_destructive``. Decline
    or elicit unavailable → no work runs, error returned in the
    ``ReindexResult`` envelope. ``ToolAnnotations(destructive_hint=True)``
    signals the destructive nature to host UIs even when the user hasn't
    passed the destructive flags yet (the gate runs before qmd).
    """
    from lies.etl.sync_helper import collection_names, sync_collection
    from lies.qmd import _models
    from lies.qmd.cli import qmd_reindex

    wiki = resolve_wiki(name)

    result = _models.ReindexResult()

    # Optional pre-step: sync each collection before reindex so the
    # rebuild sees fresh raw mirrors. Mirrors ``lies reindex --reconcile``.
    if reconcile:
        for coll_name in collection_names(wiki, None):
            sync_collection(wiki, coll_name, force=False)
        result.reconciled = True

    # Gate destructive flags. When ``ctx`` is None (programmatic caller)
    # ``_confirm_destructive`` raises on ``ctx.elicit`` and the helper
    # returns ``elicitation unavailable``; we treat that as decline so
    # no work runs (safety preserved through the helper's exception
    # path, symmetric with the CLI's refuse-on-non-TTY-without-yes).
    if cleanup or all_:
        if all_:
            prompt = "Reindex --all will run cleanup + reindex + embed (full rebuild). Confirm?"
        else:
            prompt = "Cleanup will vacuum the FTS5 db and drop orphan rows. Confirm?"
        decision = await _confirm_destructive(ctx, prompt)
        if decision is not None:
            # Detect elicitation-unavailable specifically so the LLM
            # caller gets a bypass path rather than a generic
            # protocol-version error string. Bug E (session f39c9ef8):
            # the host's MCP connection was older than
            # ``2026-07-28`` and rejected server-initiated elicitation;
            # ``_confirm_destructive`` returned
            # ``"elicitation unavailable: <inner-exc>"`` and the LLM
            # had no actionable signal to route on. Surface the
            # concrete workaround here so the caller can either
            # restart the MCP daemon (which negotiates a newer
            # protocol version) or invoke ``lies reindex --cleanup``
            # directly from the shell, where destructive flags run
            # without the MCP gate.
            if decision.startswith("elicitation unavailable"):
                bypass_msg = (
                    "cleanup requires confirmation; MCP server-initiated "
                    "elicitation unavailable on this connection. Run "
                    "`lies mcp down && lies mcp up` and retry, or invoke "
                    "`lies reindex --cleanup` directly from the shell."
                )
                return _models.ReindexResult(
                    reconciled=result.reconciled, errors=[bypass_msg]
                ).model_dump()
            return _models.ReindexResult(
                reconciled=result.reconciled, errors=[decision]
            ).model_dump()

    reindex_outcome = qmd_reindex(
        wiki.wiki_dir,
        embed=embed,
        cleanup=cleanup,
        all_=all_,
        force=force,
    )
    result.indexed = reindex_outcome.indexed
    result.embedded = reindex_outcome.embedded
    result.cleaned = reindex_outcome.cleaned
    result.errors = reindex_outcome.errors
    return result.model_dump()


# ---------------------------------------------------------------------------
# ask_question — parser for /answer-style slash input
# ---------------------------------------------------------------------------


@mcp.tool(
    name="ask_question",
    description=(
        "Parse the question argument (with optional +tag_expr / "
        "-exclude_tags filter prefixes, including compound exclude "
        "chains joined with & or |) and return the parsed kwargs. "
        "The calling LLM uses this to extract filter args from a "
        "slash-style invocation where Claude Code's slash-command "
        "dispatcher would otherwise tokenize the input. After calling "
        "ask_question, the LLM should call the `synthesize` tool with the "
        "returned kwargs verbatim."
    ),
)
def ask_question(text: str) -> dict[str, object]:
    """Parse ``+c:<name>`` / ``-<tag>`` filter syntax out of ``text``.

    Bypass for Claude Code's slash-command dispatcher: the dispatcher
    tokenizes the slash input on whitespace BEFORE invoking an MCP
    prompt function, dropping everything past the first token even
    when the prompt has a single positional arg. Tools are invoked
    with structured JSON args where multi-word strings round-trip
    intact, so this tool exposes the parser as a regular MCP tool
    the LLM can call when the user reaches for the ``/answer`` slash.

    The exclude chain supports compound expressions: ``-c:foo&c:bar``
    (AND) and ``-c:foo|c:bar`` (OR) are parsed into the F15 AST and
    re-rendered as a single ``exclude_tags`` element so the
    downstream ``synthesize`` boundary can re-parse the string.

    Returns a dict with keys ``question``, ``tag_expr``,
    ``exclude_tags``. Always returns; surface parse errors as a
    structured envelope with an ``error`` key so the LLM can decide
    what to do instead of catching an exception trace.
    """
    import shlex

    argv = shlex.split(text) if text.strip() else []
    if not argv:
        return {"error": "empty input", "original": text}

    try:
        parsed_question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    except (TagExprParseError, TagExprEmpty) as exc:
        return {"error": str(exc), "original": text}

    tag_expr = _render_include(include_ast) if include_ast is not None else None
    # ``_render_include`` handles ``Include`` / ``And`` / ``Or`` trees so
    # compound excludes round-trip correctly through the MCP boundary; the
    # orchestrator still receives ``exclude_tags`` as a single-element
    # list, matching the F18 librarian's list contract.
    exclude_tags = [_render_include(exclude_ast)] if exclude_ast is not None else []

    return {
        "question": parsed_question,
        "tag_expr": tag_expr,
        "exclude_tags": exclude_tags,
    }


@mcp.tool(
    name="ask_ground_question",
    description=(
        "Parse the question argument (with optional +tag_expr / "
        "-exclude_tags filter prefixes, including compound exclude "
        "chains joined with & or |) and return the parsed kwargs for "
        "the `ground` tool. The calling LLM uses this to extract "
        "filter args from a slash-style invocation where Claude "
        "Code's slash-command dispatcher would otherwise drop or "
        "truncate the input: the `/mcp__lies__cite` slash dispatcher "
        "drops the `text` argument entirely when the input begins "
        "with `+` (session df653c3d, 2026-09-24), raising "
        "`ProtocolError: Missing required arguments: {'text'}`, and "
        "the `/answer` slash dispatcher tokenizes on whitespace. "
        "Bypass both by calling ask_ground_question with the user's "
        "full multi-word input as the `text` argument, then forward "
        "the returned kwargs verbatim to the `ground` tool (the "
        "`top_k` default is 3, matching `ground`'s default)."
    ),
)
def ask_ground_question(text: str) -> dict[str, object]:
    """Parse ``+c:<name>`` / ``-<tag>`` filter syntax out of ``text``.

    Bypass for Claude Code's slash-command dispatcher — same
    pattern as :func:`ask_question`, but the returned kwargs are
    shaped for the ``ground`` MCP tool (``question``,
    ``tag_expr``, ``exclude_tags``, ``top_k``) instead of
    ``answer`` / ``query``. The LLM forwards the returned kwargs
    verbatim to ``ground``.

    The exclude chain supports compound expressions: ``-c:foo&c:bar``
    (AND) and ``-c:foo|c:bar`` (OR) are parsed into the F15 AST and
    re-rendered as a single ``exclude_tags`` element so the
    downstream ``ground`` boundary can re-parse the string.

    Returns a dict with keys ``question``, ``tag_expr``,
    ``exclude_tags``, ``top_k``. Always returns; surface parse errors
    as a structured envelope with an ``error`` key so the LLM can
    decide what to do instead of catching an exception trace.
    """
    import shlex

    argv = shlex.split(text) if text.strip() else []
    if not argv:
        return {"error": "empty input", "original": text}

    try:
        parsed_question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    except (TagExprParseError, TagExprEmpty) as exc:
        return {"error": str(exc), "original": text}

    tag_expr = _render_include(include_ast) if include_ast is not None else None
    # ``_render_include`` handles ``Include`` / ``And`` / ``Or`` trees so
    # compound excludes round-trip correctly through the MCP boundary; the
    # ``ground`` tool still receives ``exclude_tags`` as a single-element
    # list, matching the F18 librarian's list contract.
    exclude_tags = [_render_include(exclude_ast)] if exclude_ast is not None else []

    return {
        "question": parsed_question,
        "tag_expr": tag_expr,
        "exclude_tags": exclude_tags,
        "top_k": 3,
    }


def _collect_available_tags_mcp(wiki: Wiki | None) -> set[str]:
    """Return every addressable tag in the library (MCP surface).

    Thin shim over :func:`lies.library.registry.library_collection_names`
    — kept so the MCP ``query`` / ``answer`` boundary has the same
    helper name as its CLI counterpart. ``wiki`` is accepted for
    signature uniformity with the legacy per-wiki resolution but is
    intentionally ignored: collections live in the library, not in
    any wiki.

    Each collection name is added both bare and with the ``c:``
    qualifier prefix so the F15 tag-expression validator recognizes
    ``c:<name>`` atoms as addressable on the MCP ``query`` / ``answer``
    path (Fix 3 / Task 3 brief). Each ``LibraryCollectionConfig.tags``
    entry is added both bare and with the ``t:`` qualifier prefix so
    ``t:<tag>`` filters against a library-collection tag do not raise
    ``TagExprUnknown`` (Fix 6 / Task 8 brief) — and so bare ``+tag``
    expressions validate too, since F15 treats a bare atom as the
    implicit-t alias for ``+t:tag`` (``atom_matches`` matches the tag
    against ``coll.tags ∪ {coll.name}`` and a bare expression's
    ``Include.tag`` is the unqualified string). Both lookups are
    wrapped in ``try/except`` so an uninitialized library — or any
    other registry failure — does not break the validator; the
    function still returns a set, just one that does not include
    library tags of the failed surface.
    """
    from lies.library.registry import library_collection_names, library_collection_tags

    try:
        names = library_collection_names()
    except Exception:
        # Library uninitialized (or any other registry failure) is fine
        # — the validator just sees no library-collection names.
        names = frozenset()
    tags: set[str] = set(names) | {f"c:{name}" for name in names}
    try:
        for tag in library_collection_tags():
            tags.add(tag)  # bare tag (implicit-t: alias)
            tags.add(f"t:{tag}")  # explicit t: qualifier form
    except Exception:
        # ``library_collection_tags`` raises when the library is
        # uninitialized or its config-yaml surface fails to read. The
        # validator still works against the wiki side.
        pass
    return tags


def format_unknown_tag_error(exc: TagExprUnknown) -> str:
    """Build a self-explanatory ``unknown tag`` ToolError message.

    Library-first surface. Up to three lines:

      - offending tag spelling (``'opencode'``)
      - the library's collections sorted (``the library's collections: ...``),
        when the library is initialized with at least one collection
      - when the library is empty / absent, a distinct line that
        tells the operator whether to initialize the library or to
        ingest something into it (two separate failure modes).

    The library is NOT a wiki and is never referred to as one. The
    original ``unknown tag: <tag>`` prefix is preserved so log
    scrapers and existing tests that grep on the literal string keep
    working.
    """
    from lies.library.registry import (
        library_has_no_collections,
        library_initialized,
    )

    parts: list[str] = [f"unknown tag: {exc.tag!r}"]
    if exc.available:
        sorted_tags = sorted(exc.available)
        parts.append(f"the library's collections: {', '.join(sorted_tags)}")
    elif not library_initialized():
        parts.append(
            "the library is not initialized; collections live in the library, "
            "not in wikis. Initialize it before querying with tag filters."
        )
    elif library_has_no_collections():
        parts.append(
            "the library has no collections; ingest something first "
            "(see `lies ingest --help`) before querying with tag filters."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# lint — deterministic health-check
# ---------------------------------------------------------------------------


@mcp.tool
def lint(
    name: str | None = None,
    fix: bool = False,
    force_repair: bool = False,
) -> str:
    """Run lint; with ``fix=True`` also apply the repair plan.

    When ``fix=True`` and ``force_repair=True``, the cross-process
    memory flock is unconditionally reaped + retried once before
    surfacing ``WikiFlockUnrepairable`` if a live contender still
    holds it; without the flag, a live contender raises
    ``WikiLockBusy``. Flock errors are caught and returned as an
    ``error:``-prefixed string so the MCP server stays up; other
    exceptions propagate to the MCP error path.
    """
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki=wiki)
    try:
        return orch.run_lint(apply=fix, force_repair=force_repair)
    except (WikiFlockUnrepairable, WikiLockBusy):
        return f"error: {sys.exc_info()[1]}"


# ---------------------------------------------------------------------------
# synthesize — prose answer for human reading (library-mode read surface)
# ---------------------------------------------------------------------------


from lies.mcp.synth import synthesize as _synthesize  # noqa: E402


@mcp.tool(
    description=(
        "Synthesize a prose answer for human reading. Returns "
        "`{answer, citations, pages_read, fallback_used, synthesis_used}`. "
        "Use when the human asks a question and wants a complete, "
        "synthesized response (vs. `/cite` which returns snippets for "
        "agent context)."
    ),
    annotations=ToolAnnotations(
        title="Synthesize: prose answer for human reading",
    ),
)
async def synthesize(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    file_back: bool = False,
) -> dict:
    """Synthesize a prose answer from library collections.

    Args:
        question: Natural-language question.
        tag_expr: Body of a single include expression (no leading sigil).
        exclude_tags: At most one entry (F15 grammar).
        file_back: Reserved; raises ToolError until write-tool spec lands.
    """
    if exclude_tags is not None and len(exclude_tags) > 1:
        raise ToolError(f"exclude_tags accepts at most one tag; got {len(exclude_tags)}")

    exclude_expr: TagExpr | None = None
    if exclude_tags:
        from lies.query.tag_expr import parse

        exclude_expr = parse(exclude_tags[0])

    envelope = await _synthesize(
        question=question,
        tag_expr=tag_expr,
        exclude_expr=exclude_expr,
        file_back=file_back,
    )
    return {
        "question": envelope.question,
        "tag_expr": envelope.tag_expr,
        "answer": envelope.answer,
        "citations": [asdict(c) for c in envelope.citations],
        "pages_read": envelope.pages_read,
        "fallback_used": envelope.fallback_used,
        "synthesis_used": envelope.synthesis_used,
        "fallback_reason": envelope.fallback_reason,
    }


# ---------------------------------------------------------------------------
# Resources — raw wiki reads, no LLM round-trip
# ---------------------------------------------------------------------------
#
# Each resource is defined as an ``_impl`` function (takes ``name``,
# does the real work) plus a thin zero-argument forwarder that FastMCP
# registers as the static-resource handler. The forwarder pattern is a
# FastMCP 3.4.5 constraint: static-resource handlers must be
# zero-argument. Keeping the impl beside the forwarder lets tests and
# direct callers exercise the real logic with an explicit ``name``.


def _register_static_resource(
    uri: str, fn: Callable[..., str], description: str | None = None
) -> Callable[..., str]:
    """Register ``fn`` as a *static* MCP resource even when it has params.

    FastMCP's ``@mcp.resource(...)`` decorator auto-routes any callable
    with parameters to :class:`ResourceTemplate`, which then rejects
    URIs without ``{...}`` placeholders. The ``query`` / ``answer``
    tools accept a wiki ``name`` kwarg (resolving through
    :func:`lies.mcp.resolution.resolve_wiki`); the resource surface
    needs the same parity for direct Python callers and tests, so we
    bypass the decorator's auto-routing by registering a
    :class:`FunctionResource` directly. FastMCP's read path still
    invokes the registered ``fn`` with no positional arguments; any
    ``name``-style kwargs are passed only by Python callers (tests,
    REPL, direct imports), not by the MCP wire.
    """
    from fastmcp.resources.function_resource import FunctionResource

    resource = FunctionResource.from_function(
        fn=fn,
        uri=uri,
        description=description,
    )
    mcp.add_resource(resource)
    return fn


def _wiki_status_impl(name: str | None = None) -> str:
    """Return qmd status plus the last 10 lines of ``wiki/log.md``.

    If qmd is unavailable, the error is embedded in the returned string
    (resource reads must never fail loudly for a degraded-but-functional
    state).
    """
    wiki = resolve_wiki(name)
    out = "=== qmd status ===\n"
    try:
        from lies.qmd import qmd_status as _qmd_status

        out += _qmd_status(wiki.data_root)
    except Exception as exc:  # noqa: BLE001 - degraded path must surface, not crash
        out += f"qmd unavailable: {exc}"
    out += "\n\n=== last 10 log entries ===\n"
    log_path = wiki.wiki_dir / "log.md"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-10:]:
            out += line + "\n"
    else:
        out += "(no log yet)\n"
    return out


def _wiki_status_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://status``.

    FastMCP's read path calls this with no arguments; ``name`` falls
    back to the env-default wiki (resolved inside the impl). Tests
    should import the public :func:`wiki_status` instead, which accepts
    an explicit ``name`` kwarg.
    """
    return _wiki_status_impl()


_register_static_resource(
    "wiki://status",
    _wiki_status_mcp,
    description="qmd status + last 10 log lines.",
)


def wiki_status(name: str | None = None) -> str:
    """qmd status + last 10 log lines.

    Direct Python entry point — accepts an explicit ``name`` kwarg so
    tests and REPL callers don't have to mutate ``LIES_WIKI_NAME``.
    Mirrors the ``query`` / ``answer`` / ``init_wiki`` tool surface.
    The FastMCP wire protocol calls the registered zero-arg handler
    above; this function is the parity surface for direct callers.
    """
    return _wiki_status_impl(name)


def _wiki_index_impl(name: str | None = None) -> str:
    """Raw ``wiki/index.md`` contents (JSON envelope in library mode).

    Wiki mode (a wiki is registered and its ``data_root`` exists on
    disk): returns the raw markdown of ``wiki/index.md``. The empty-file
    case returns ``""`` so an LLM caller can distinguish "no wiki
    catalog rendered yet" from "wiki is registered, with a populated
    index".

    Library mode (no wiki registered, or the resolved wiki's
    ``data_root`` does not exist on disk) returns a stable envelope
    ``{"mode": "library"}``. The mode discriminator lets an LLM caller
    distinguish a wiki-mode index dump from a library-mode response
    without parsing the shape, mirroring the F4b ``wiki://catalog``
    envelope contract.
    """
    import json

    try:
        wiki = resolve_wiki(name)
    except WikiNotRegistered:
        return json.dumps({"mode": "library"}, indent=2)

    if not wiki.data_root.exists():
        return json.dumps({"mode": "library"}, indent=2)

    index_path = wiki.wiki_dir / "index.md"
    if not index_path.exists():
        return ""
    return index_path.read_text(encoding="utf-8")


def _wiki_index_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://index``."""
    return _wiki_index_impl()


_register_static_resource(
    "wiki://index",
    _wiki_index_mcp,
    description="Raw contents of wiki/index.md.",
)


def wiki_index(name: str | None = None) -> str:
    """Raw contents of ``wiki/index.md`` (JSON envelope in library mode).

    Returns the raw markdown of ``wiki/index.md`` when a wiki is
    registered; returns ``""`` if the wiki exists but the index file
    has not been rendered yet; returns ``'{"mode": "library"}'`` when
    no wiki is registered (library mode). See
    :func:`_wiki_index_impl` for the full contract.

    Direct Python entry point — accepts an explicit ``name`` kwarg so
    tests and REPL callers don't have to mutate ``LIES_WIKI_NAME``.
    Mirrors the ``query`` / ``answer`` / ``init_wiki`` tool surface.
    The FastMCP wire protocol calls the registered zero-arg handler
    above; this function is the parity surface for direct callers.
    """
    return _wiki_index_impl(name)


def _wiki_log_impl(name: str | None = None) -> str:
    wiki = resolve_wiki(name)
    log_path = wiki.wiki_dir / "log.md"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


def _wiki_log_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://log``."""
    return _wiki_log_impl()


_register_static_resource(
    "wiki://log",
    _wiki_log_mcp,
    description="Raw contents of wiki/log.md.",
)


def wiki_log(name: str | None = None) -> str:
    """Raw contents of ``wiki/log.md`` (empty string if absent).

    Direct Python entry point — accepts an explicit ``name`` kwarg so
    tests and REPL callers don't have to mutate ``LIES_WIKI_NAME``.
    Mirrors the ``query`` / ``answer`` / ``init_wiki`` tool surface.
    The FastMCP wire protocol calls the registered zero-arg handler
    above; this function is the parity surface for direct callers.
    """
    return _wiki_log_impl(name)


def _wiki_lint_report_impl(name: str | None = None) -> str:
    """Raw ``wiki/lint-report.md`` contents (JSON envelope in library mode).

    Wiki mode (a wiki is registered and its ``data_root`` exists on
    disk): returns the raw markdown of ``wiki/lint-report.md``. The
    empty-file case returns ``""`` so an LLM caller can distinguish "no
    lint has run yet" from "lint has run and reported findings".

    Library mode (no wiki registered, or the resolved wiki's
    ``data_root`` does not exist on disk) returns a stable envelope
    ``{"mode": "library", "status": "no_wiki"}``. The ``status`` field
    gives the LLM caller a concrete reason string to route on (the
    wiki never existed, so a lint report is meaningless). Mirrors the
    F4b ``wiki://catalog`` envelope contract.
    """
    import json

    try:
        wiki = resolve_wiki(name)
    except WikiNotRegistered:
        return json.dumps({"mode": "library", "status": "no_wiki"}, indent=2)

    if not wiki.data_root.exists():
        return json.dumps({"mode": "library", "status": "no_wiki"}, indent=2)

    report_path = wiki.wiki_dir / "lint-report.md"
    if not report_path.exists():
        return ""
    return report_path.read_text(encoding="utf-8")


def _wiki_lint_report_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://lint-report``."""
    return _wiki_lint_report_impl()


_register_static_resource(
    "wiki://lint-report",
    _wiki_lint_report_mcp,
    description="Raw contents of wiki/lint-report.md.",
)


def wiki_lint_report(name: str | None = None) -> str:
    """Raw contents of ``wiki/lint-report.md`` (JSON envelope in library mode).

    Returns the raw markdown of ``wiki/lint-report.md`` when a wiki is
    registered; returns ``""`` if the wiki exists but no lint has run
    yet; returns ``'{"mode": "library", "status": "no_wiki"}'`` when
    no wiki is registered (library mode). See
    :func:`_wiki_lint_report_impl` for the full contract.

    Direct Python entry point — accepts an explicit ``name`` kwarg so
    tests and REPL callers don't have to mutate ``LIES_WIKI_NAME``.
    Mirrors the ``query`` / ``answer`` / ``init_wiki`` tool surface.
    The FastMCP wire protocol calls the registered zero-arg handler
    above; this function is the parity surface for direct callers.
    """
    return _wiki_lint_report_impl(name)


# ---------------------------------------------------------------------------
# library://catalog — read-through for the library collection registry
# ---------------------------------------------------------------------------


def _count_pages(name: str) -> int:
    """Count ``*.md`` files under a library collection's doc tree.

    Each collection is rooted at ``<library>/collections/<name>/`` and
    carries its markdown under ``doc/`` (post-ingest). We walk the
    whole subtree for ``*.md`` files; empty / missing trees return 0.
    Used by ``library://catalog`` and ``library://catalog/{slug}`` to
    surface a ``page_count`` field so LLM callers can rank
    collections by corpus size without re-reading the raw mirrors.
    """
    from lies.library.paths import Library

    root = Library.open().collections_root / name / "doc"
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def _library_collection_payload(slug: str) -> dict | None:
    """Build the per-collection metadata envelope for ``slug``.

    Returns ``None`` when ``slug`` is not a registered collection —
    lets the single-slug resource distinguish "absent" from "present
    with empty fields" cheaply (the resource handler returns ``""``
    in that case). ``load_config`` raises :class:`CollectionNotFound`
    when the config is absent; we treat the absent case as "not a
    collection" rather than crashing.
    """
    from lies.library.config_io import load_config
    from lies.library.errors import CollectionNotFound

    try:
        cfg = load_config(slug)
    except CollectionNotFound:
        return None
    return {
        "name": cfg.name,
        "tags": list(cfg.tags),
        "source": cfg.source,
        "page_count": _count_pages(slug),
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


@mcp.resource("library://catalog")
def library_catalog() -> str:
    """All library collection metadata as a per-collection grouping.

    Returns JSON of shape::

        {
          "<collection-name>": {
            "name": "<collection-name>",
            "tags": ["..."],
            "source": "<source-url>",
            "page_count": <int>,
            "updated_at": "<iso-8601>"
          },
          ...
        }

    Collections without a ``config.yaml`` (not yet bootstrapped) are
    silently skipped — the resource only surfaces collections the
    library knows about through its registry. Empty library → ``{}``.
    """
    import json

    from lies.library.registry import library_collection_names

    out: dict[str, dict] = {}
    for name in sorted(library_collection_names()):
        payload = _library_collection_payload(name)
        if payload is None:
            continue
        out[name] = payload
    return json.dumps(out, indent=2)


@mcp.resource("library://catalog/{slug}")
def library_catalog_slug(slug: str) -> str:
    """Single library collection metadata; empty string when not found.

    Returns ``""`` (not a JSON object) when ``slug`` is not a
    registered collection so an LLM caller can distinguish "missing"
    from "present with empty fields" cheaply. Empty-string is the
    same contract the retired ``wiki://catalog/{slug}`` resource
    used — clients that pattern-matched on it keep working.
    """
    import json

    payload = _library_collection_payload(slug)
    if payload is None:
        return ""
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Prompt — starter templates for the synthesizer paths
# ---------------------------------------------------------------------------


@mcp.prompt(name="answer")
def ask_wiki_answer(text: str) -> str:
    """Starter prompt that templates a ``synthesize`` tool invocation.

    Single-arg form: the entire slash-command input is passed verbatim
    as ``text``. The filter-syntax parser runs here so the calling
    LLM never has to fill ``tag_expr`` / ``exclude_tags`` slots.

    Chat-surface counterpart to the synthesizer path: the LLM calls
    the ``synthesize`` tool and surfaces the ``answer`` field of the
    returned dict verbatim in chat. Use this when the response needs
    to render as plain prose rather than behind a collapsible JSON block.

    **Known limitation — Claude Code slash dispatcher tokenizes the
    input on whitespace before invoking this prompt, so multi-word
    filter prefixes are truncated to the first token.** Even with the
    single-arg signature, ``/mcp__lies__answer +c:opencode Where does
    opencode keep settings?`` arrives at the prompt as just
    ``+c:opencode`` and the question is dropped. The reliable
    workaround is the ``ask_question`` MCP tool: call it with the
    user's full multi-word input as the ``text`` argument, then
    forward the returned kwargs verbatim to the ``synthesize`` tool. This
    prompt is still useful for plain questions without filter syntax,
    where the input is a single token anyway.

    Filter syntax (parsed out of the ``text`` argument here, so the
    calling LLM never has to fill ``tag_expr`` / ``exclude_tags``
    slots — that was the live hallucination bug):

    - ``+c:<name>`` — include the named library collection
    - ``+t:<tag>`` — include pages tagged ``<tag>``
    - ``+a&b`` — AND two include atoms (no spaces)
    - ``+a|b`` — OR (lower precedence than ``&``)
    - ``-c:<name>`` / ``-t:<tag>`` — exclude the named collection or tag
    - ``-"airflow provider"`` — exclude with quoted tag
    - ``-c:foo&c:bar`` — exclude AND (drop collections matching both)
    - ``-c:foo|c:bar`` — exclude OR (drop collections matching either)

    Everything after the include chain and the optional exclude is
    the question text. The parser stops at the first non-filter
    token.

    Examples::

        /answer +c:opencode Where does opencode keep settings?
            tag_expr: c:opencode
            question: Where does opencode keep settings?

        /answer +t:linux +c:opencode -draft how do I configure...
            tag_expr: c:opencode&t:linux
            exclude_tags: [draft]
            question: how do I configure...

        /answer +c:opencode|c:claude_platform Where does opencode keep settings?
            tag_expr: c:opencode|c:claude_platform
            question: Where does opencode keep settings?

        /answer -c:claude_platform&t:claude Where does opencode keep settings?
            exclude_tags: [c:claude_platform&t:claude]
            question: Where does opencode keep settings?

    Mirrors the CLI grammar exactly (see
    ``features/tag-filter-language/2026-09-02-tag-filter-language-design.md``
    and ``src/lies/query/tag_expr.py:parse_query_argv``).

    The ``name="answer"`` override registers the prompt as the
    ``/answer`` slash command even though the Python function is named
    ``ask_wiki_answer`` (the bare name conflicts with the ``synthesize``
    tool defined elsewhere in this module).
    """
    import shlex

    argv = shlex.split(text) if text.strip() else []
    if not argv:
        return _filter_parse_error_prompt(text, ValueError("empty input"))

    try:
        parsed_question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    except (TagExprParseError, TagExprEmpty) as exc:
        return _filter_parse_error_prompt(text, exc)

    tag_expr = _render_include(include_ast) if include_ast is not None else None
    exclude_tags = [_render_include(exclude_ast)] if exclude_ast is not None else []

    return _render_answer_prompt_body(
        question=parsed_question,
        tag_expr=tag_expr,
        exclude_tags=exclude_tags,
    )


def _render_answer_prompt_body(
    *,
    question: str,
    tag_expr: str | None,
    exclude_tags: list[str],
) -> str:
    """Render the prompt body for the parsed args.

    No fillable slots for ``tag_expr`` / ``exclude_tags``: the slash
    prompt parses them out of the ``question`` argument before the
    calling LLM sees the body. The LLM only has to forward the
    rendered kwargs verbatim to the ``synthesize`` tool. The
    rendered kwargs match ``synthesize``'s actual signature
    (``question, tag_expr, exclude_tags, file_back``); the
    ``file_back`` slot is reserved (raises ToolError until write-tool
    spec lands) and intentionally omitted so the LLM never forwards
    it. The ``name`` / ``collection`` kwargs from the old ``answer``
    tool shape are gone — FastMCP would raise ``TypeError: unexpected
    keyword argument`` on a faithful forward.
    """
    return (
        f"Call the `synthesize` MCP tool with the following args, then surface "
        f"the answer body verbatim in your reply:\n\n"
        f"  question: {question}\n"
        f"  tag_expr: {tag_expr!r}\n"
        f"  exclude_tags: {exclude_tags!r}\n\n"
        f"Do NOT modify these values before passing them to the tool. "
        f"If the parsed args look wrong, surface the parse error verbatim "
        f"and stop; do not retry with hand-rewritten args."
    )


def _filter_parse_error_prompt(question: str, exc: Exception) -> str:
    """Render a parse-error prompt body.

    Surfaces the parser's message verbatim — the calling LLM tells
    the operator what went wrong instead of hallucinating a fix. The
    fallback would be silent (pass the question through with no
    filter), which is exactly the bug we're closing.
    """
    return (
        f"Filter parse error: {exc}\n\n"
        f"Original argument: {question!r}\n\n"
        f"Filter syntax: tokens prefixed with `+` are include atoms "
        f"(e.g. `+c:opencode`), tokens prefixed with `-` are exclude "
        f"atoms (e.g. `-draft`). Use double-quotes for tags with spaces "
        f'(`-"airflow provider"`). Everything else is the question.'
    )


@mcp.prompt(name="orient")
def orient(wiki: str | None = None) -> str:
    """Return LIES orientation prose. Root of the prompts surface.

    Args:
        wiki: Wiki name to substitute into the prompt body; ``None``
            renders an ``(unspecified)`` placeholder.

    Returns:
        Rendered markdown body the LLM reads for orientation.
    """
    body = load_prompt("orient")
    return body.format(version=__version__, wiki=wiki or "(unspecified)")


@mcp.prompt(name="ingest")
def ingest(source: str) -> str:
    """Return the source-ingestion walkthrough prose.

    ``source`` is captured for MCP introspection only; the reference
    prose is static and does not substitute this value.
    """
    return load_prompt("ingest").format(version=__version__, source=source)


@mcp.prompt(name="lint")
def lint_prompt() -> str:
    """Return the lint walkthrough prose."""
    return load_prompt("lint").format(version=__version__)


@mcp.prompt(name="sync")
def sync_prompt(collection: str) -> str:
    """Return the sync walkthrough prose.

    ``collection`` is captured for MCP introspection only; the
    reference prose is static and does not substitute this value.
    """
    return load_prompt("sync").format(version=__version__, collection=collection)


@mcp.prompt(
    name="cite",
    description=(
        "Slash that drives the `ground` MCP tool. Single-arg form, "
        "parses filter syntax internally; the calling LLM forwards "
        "the rendered kwargs to the `ground` tool verbatim. "
        "**Known limitation — Claude Code's slash dispatcher "
        "truncates on the first whitespace, so a `/cite +c:foo "
        "What is X?` invocation only sees `+c:foo` at the prompt.** "
        "The prompt detects that truncated shape and substitutes a "
        "default question so the search still runs (session "
        "df653c3d, 2026-09-24). For end-to-end question preservation, "
        "call the `ask_ground_question` MCP tool directly with the "
        "user's full multi-word text as the `text` argument and "
        "forward the returned kwargs verbatim to `ground`. Mirrors "
        "the `/answer` slash prompt's filter-parse shape."
    ),
)
def cite(text: str) -> str:
    """Slash that routes to the Archivist (F34 ground tool) and renders
    the digest as ``[[collection/slug]] (Title): "<snippet>"`` lines.

    Mirrors the ``/answer`` prompt's filter-parse shape: single
    ``text`` positional arg carries the entire slash-input. The
    filter-syntax parser runs here so the calling LLM never has to
    fill ``tag_expr`` / ``exclude_tags`` / ``top_k`` slots.

    The parent LLM does the rendering; the prompt only templates
    the tool call and the render form. On ``ArchivistCoverageError``
    the prompt tells the LLM to surface verbatim — no silent retry.

    **Known limitation — Claude Code slash dispatcher truncates
    ``/cite`` input on the first whitespace.** Surfaced in session
    df653c3d (2026-09-24T01:29:30Z): the dispatcher forwards
    ``/mcp__lies__cite +c:opencode|c:minimax|c:llama_cpp Configure my opencode...``
    with only the first token (``+c:opencode|c:minimax|c:llama_cpp``)
    as the ``text`` argument; the rest of the line is dropped before
    the prompt runs. The prompt detects the truncated shape (every
    surviving argv token is a filter atom, no plain question words)
    and substitutes a default question so the search still runs.
    This is a graceful-degradation workaround for a Claude Code
    dispatcher bug, not a fix for the dispatcher itself — operators
    who want their literal question preserved end-to-end should call
    the ``ask_ground_question`` MCP tool directly with their full
    multi-word input as ``text`` (the dispatcher bypasses that tool
    entirely; it's the reliable path when the slash is lossy).

    Filter syntax (parsed out of the ``text`` argument here, so the
    calling LLM never has to fill ``tag_expr`` / ``exclude_tags``
    slots — that was the live hallucination bug):

    - ``+c:<name>`` — include the named library collection
    - ``+t:<tag>`` — include pages tagged ``<tag>``
    - ``+a&b`` — AND two include atoms (no spaces)
    - ``+a|b`` — OR (lower precedence than ``&``)
    - ``-c:<name>`` / ``-t:<tag>`` — exclude the named collection or tag
    - ``-"airflow provider"`` — exclude with quoted tag
    - ``-c:foo&c:bar`` — exclude AND (drop collections matching both)
    - ``-c:foo|c:bar`` — exclude OR (drop collections matching either)

    Everything after the include chain and the optional exclude is
    the question text. The parser stops at the first non-filter
    token.

    Args:
        text: Entire slash-input, parsed for filter syntax. Same
            single-arg shape as ``ask_wiki_answer``.

    Returns:
        Prose that templates a ``ground`` tool call and the
        ``[[collection/slug]] (Title): "<snippet>"`` render form.
    """
    import shlex

    argv = shlex.split(text) if text.strip() else []
    if not argv:
        return _filter_parse_error_prompt(text, ValueError("empty input"))

    try:
        parsed_question, include_ast, exclude_ast, _ = parse_query_argv(argv)
    except (TagExprParseError, TagExprEmpty) as exc:
        # Workaround for Claude Code's slash dispatcher bug: when
        # the slash input begins with ``+`` the dispatcher drops
        # everything past the first whitespace, so the prompt only
        # sees the filter token. ``parse_query_argv`` raises
        # ``"filter present but no question"`` for this shape;
        # the include / exclude ASTs were never assigned, so we
        # re-parse the filter portion directly via
        # :func:`_parse_filter_only` (which tolerates a missing
        # question) and substitute a default question so the
        # search still runs. Other parse errors (e.g. ``+a&``
        # with a dangling operator) surface verbatim so the
        # operator can fix the typo.
        if str(exc) != "filter present but no question":
            return _filter_parse_error_prompt(text, exc)
        include_ast, exclude_ast = _parse_filter_only(argv)
        parsed_question = _DEFAULT_CITE_QUESTION

    tag_expr = _render_include(include_ast) if include_ast is not None else None
    exclude_tags = [_render_include(exclude_ast)] if exclude_ast is not None else []
    top_k = 3

    return _render_cite_prompt_body(
        question=parsed_question,
        tag_expr=tag_expr,
        exclude_tags=exclude_tags,
        top_k=top_k,
    )


_DEFAULT_CITE_QUESTION = "Summarize the most relevant snippets in the matched corpus."


def _parse_filter_only(argv: list[str]) -> tuple[Any, Any]:
    """Parse ``argv`` for the include / exclude filter ASTs only.

    Tolerates a missing question by re-parsing the argv with a
    synthetic trailing question token. Used by the ``/cite`` prompt
    to recover the filter ASTs when ``parse_query_argv`` rejects the
    truncated dispatcher shape (``filter present but no question``).

    The synthetic token is a plain word that no filter chain could
    consume (``parse_tokens`` stops at the first non-filter token),
    so the parser leaves it in the question slot without disturbing
    the chain.

    Returns ``(include_ast, exclude_ast)``. Both default to
    ``None`` when the input has no filter.
    """
    from lies.query.tag_expr import TagExprParseError

    sentinel_argv = [*argv, "__cite_default_question__"]
    try:
        _, include_ast, exclude_ast, _ = parse_query_argv(sentinel_argv)
    except TagExprParseError:
        # Filter chain itself is malformed (e.g. ``+a&``); fall
        # back to None ASTs so the caller surfaces the parse error
        # rather than emitting a half-routed prompt.
        return None, None
    return include_ast, exclude_ast


def _render_cite_prompt_body(
    *,
    question: str,
    tag_expr: str | None,
    exclude_tags: list[str],
    top_k: int,
) -> str:
    """Render the prompt body for the parsed args.

    No fillable slots for ``tag_expr`` / ``exclude_tags`` / ``top_k``:
    the slash prompt parses them out of the ``question`` argument
    before the calling LLM sees the body. The LLM only has to forward
    the rendered kwargs verbatim to the ``ground`` tool.
    """
    return (
        f"Call the `ground` MCP tool to ground the following fragment:\n"
        f"  question: {question}\n"
        f"  tag_expr: {tag_expr!r}\n"
        f"  exclude_tags: {exclude_tags!r}\n"
        f"  top_k: {top_k!r}\n\n"
        f"Render the returned `ArchivistDigest.citations` as one line "
        f"per citation in this exact form:\n\n"
        f'  [[collection/slug]] (Title): "<verbatim CitationSnippet.snippet>"\n\n'
        f"LIBRARY citations (source_kind='library') render as above. "
        f"WIKI-ONLY citations (source_kind='wiki') prefix the "
        f"line with `[secondary] ` to flag that the snippet is not "
        f"grounded in a primary source:\n\n"
        f'  [secondary] [[wiki/slug]] (Title): "<snippet>"\n\n'
        f"Do NOT fabricate citations. On `ArchivistCoverageError`, "
        f"surface the error message verbatim in your reply and stop. "
        f"If `no_coverage` is true on the digest, say so explicitly and "
        f"render any still-relevant slugs with empty snippets."
    )


@mcp.prompt(name="file-back")
def file_back(wiki: str) -> str:
    """Return the F3 file-back walkthrough prose.

    ``wiki`` is captured for MCP introspection only; the reference
    prose is static and does not substitute this value.
    """
    return load_prompt("file-back").format(version=__version__, wiki=wiki)
