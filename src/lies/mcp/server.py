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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from pydantic import Field

try:
    from fastmcp import Context
except ImportError:  # FastMCP < 3.4.5 with Context.elicit
    Context: type | None = None  # type: ignore[assignment,misc]

from lies import __version__, xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.errors import WikiAlreadyExists
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.mcp.instructions_loader import load_instructions, load_prompt
from lies.mcp.resolution import resolve_wiki
from lies.memory.models import WikiPlanInvalid
from lies.orchestrator import Orchestrator
from lies.query.citation import Citation, ClaimCitation
from lies.query.tag_expr import (
    TagExprEmpty,
    TagExprParseError,
    TagExprUnknown,
    _render_include,
    check_qualifier,
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


class SynthesizedMcpAnswer(BaseModel):
    """Structured answer returned by the ``query`` tool.

    A 1:1 slice of :class:`lies.query.models.SynthesizedAnswer` for
    FastMCP serialization — only ``page_links`` is dropped (it is
    redundant with ``citations`` plus the answer body's own links;
    raw wiki reads are still available via the ``wiki://`` resources
    if the LLM wants them). ``should_file`` and ``file_receipt`` are
    F3 file-back fields: ``should_file`` is the agent's verdict on
    whether the answer earns a wiki page; ``file_receipt`` is the
    structured outcome of the file-back attempt (or ``None`` when
    filing was skipped / failed-soft).

    ``citations`` and ``pages_read`` mirror the underlying
    :class:`SynthesizedAnswer` shape (``list[Citation]``) — each
    carries the source discriminator (``"library"`` / ``"wiki"``)
    so downstream consumers can apply the library-wins-on-conflict
    rule without re-deriving the source from the path.
    """

    answer: str
    fallback_used: bool
    fallback_reason: str | None  # None when qmd served the query
    citations: list[Citation]
    pages_read: list[Citation]
    claim_citations: list[ClaimCitation] = []
    changed_pages: list[str]
    synthesis_used: bool = False
    synthesis_reason: str | None = None  # None when the agent answered cleanly
    should_file: bool = False  # F3: agent verdict on whether this earns a page
    file_receipt: dict | None = None  # F3: serialized MemoryReceipt or None
    searched_scope: list[str] = Field(
        default_factory=list
    )  # Bundle C (F15): sorted, unique collection names searched
    format: Literal["md", "table", "marp", "chart"] = (
        "md"  # F1: validated output format (auto-route resolves to one of these)
    )


# Re-export the page-author slice for FastMCP serialization.
from lies.page import WriteKnowledgeResult  # noqa: E402,F401


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
        exclude_tags: NOT tags without leading sigil. The F15 grammar
            permits at most one; more is forwarded to the librarian
            unchanged.
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
    try:
        digest = ground(
            question=question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
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
# wiki_search / wiki_read — direct memory retrieval
# ---------------------------------------------------------------------------


@mcp.tool
def wiki_search(
    question: str,
    collection_ids: list[str] | None = None,
    limit: int = 5,
    name: str | None = None,
) -> dict[str, object]:
    """Search the wiki identified by ``name`` for project knowledge."""
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    result = WikiMemoryService(wiki).search(
        question,
        collection_ids=collection_ids,
        limit=limit,
    )
    return cast(dict[str, object], result.model_dump())


@mcp.tool
def wiki_read(
    page_ids: list[str],
    name: str | None = None,
) -> dict[str, str]:
    """Read full wiki pages by ID for the wiki identified by ``name``."""
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    return WikiMemoryService(wiki).read(page_ids)


# ---------------------------------------------------------------------------
# file_knowledge — write one markdown page (collision + force gate)
# ---------------------------------------------------------------------------

from lies.page import build_author_plan  # noqa: E402
from lies.page.author import _SectionRefusal  # noqa: E402,F401

_TYPE_PLURAL_MCP: dict[str, str] = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "source": "sources",
    "synthesis": "synthesis",
}


@dataclass
class _CollisionVerdict:
    """Response model for the file_knowledge collision elicit."""

    action: Literal["overwrite", "rename", "cancel"]
    new_slug: str | None = None


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


@mcp.tool(
    description=(
        "Write one markdown page to the wiki. type/slug/title/body required. "
        "Slugs already on disk elicit overwrite/rename/cancel via ctx.elicit. "
        "Returns the written page path + receipt on success; raises ToolError "
        "on plan-invalid input."
    ),
)
async def file_knowledge(
    page_type: str,
    collection: str,
    slug: str,
    title: str,
    body: str,
    *,
    derived_from: list[str] | None = None,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    force: bool = False,
    name: str | None = None,
    ctx: Context | None = None,  # type: ignore[valid-type]
) -> dict[str, object]:
    wiki = resolve_wiki(name)
    rel_path = (
        "wiki/overview.md"
        if page_type == "overview"
        else f"{collection}/{_TYPE_PLURAL_MCP[page_type]}/{slug}.md"
    )

    # Collision gate.
    if (wiki.wiki_dir / rel_path).exists() and not force:
        if ctx is None:
            raise ToolError(f"page exists at {rel_path}; pass force=True to overwrite")
        verdict = await ctx.elicit(
            f"page already exists at {rel_path}; overwrite, rename, or cancel?",
            response_type=_CollisionVerdict,
        )
        # FastMCP wraps the response: AcceptedElicitation has .action == "accept"
        # and .data == <_CollisionVerdict>; DeclinedElicitation / CancelledElicitation
        # carry no user payload. Branch on the wrapper action first; only on "accept"
        # read the user's choice from .data.
        if verdict.action == "cancel":
            return WriteKnowledgeResult(
                page_path=None,
                page_type=page_type,
                slug=slug,
                collection=collection,
                op="none",
                receipt={
                    "changed_pages": [],
                    "deferred": [],
                    "fallback_used": False,
                    "fallback_reason": "",
                    "errors": ["cancelled by operator"],
                },
            ).model_dump()
        if verdict.action == "decline":
            # Treat decline the same as cancel: no write, return a cancelled receipt.
            return WriteKnowledgeResult(
                page_path=None,
                page_type=page_type,
                slug=slug,
                collection=collection,
                op="none",
                receipt={
                    "changed_pages": [],
                    "deferred": [],
                    "fallback_used": False,
                    "fallback_reason": "",
                    "errors": ["cancelled by operator"],
                },
            ).model_dump()
        if verdict.action == "accept":
            user_action = verdict.data.action
            if user_action == "rename":
                new_slug = verdict.data.new_slug
                if not new_slug:
                    raise ToolError("rename requires new_slug")
                slug = new_slug
                rel_path = (
                    "wiki/overview.md"
                    if page_type == "overview"
                    else f"{collection}/{_TYPE_PLURAL_MCP[page_type]}/{new_slug}.md"
                )
            elif user_action == "cancel":
                return WriteKnowledgeResult(
                    page_path=None,
                    page_type=page_type,
                    slug=slug,
                    collection=collection,
                    op="none",
                    receipt={
                        "changed_pages": [],
                        "deferred": [],
                        "fallback_used": False,
                        "fallback_reason": "",
                        "errors": ["cancelled by operator"],
                    },
                ).model_dump()
            # user_action == "overwrite" falls through; proceed to build_author_plan
        else:  # pragma: no cover  # unknown wrapper action
            raise ToolError(f"unexpected elicit verdict action: {verdict.action}")

    orch = Orchestrator(wiki=wiki)
    try:
        plan = build_author_plan(
            type=page_type,  # type: ignore
            collection=collection,
            slug=slug,
            title=title,
            body=body,
            derived_from=derived_from or [],
            tags=tags or [],
            sources=sources or [],
            exists=lambda r: (wiki.wiki_dir / r).exists(),
            sha_lookup=lambda r: orch._memory_service.current_state(r)[0],
            # F17 (Task 4): thread the wiki's resolved section contract
            # into the plan builder. ``Wiki.section_contract`` is the
            # per-wiki resolved contract (override → default → empty);
            # production wikis see enforcement. The default contract
            # for an unresolved wiki yields an empty SectionContract
            # that the helper short-circuits to ``[]`` — no refusal
            # fires.
            section_contract=wiki.section_contract,
        )
    except WikiPlanInvalid as exc:
        raise ToolError(f"plan_invalid: {exc}") from exc

    # F17 (Task 4) refusal surface. ``build_author_plan`` returns a
    # ``_SectionRefusal`` (an errors-as-value sentinel) when the body
    # omits a heading required by the wiki's section contract. We
    # short-circuit before reaching ``Orchestrator.file_back_author``
    # and translate the refusal into a refusal-shaped
    # ``WriteKnowledgeResult`` (``op="none"``, ``page_path=None``,
    # error preserved verbatim in ``receipt["errors"]``). The shape
    # mirrors the cancel/decline elicit branches above; LLM callers
    # already pattern-match on these fields, so we keep the surface
    # uniform. ``Orchestrator.file_back_author`` also has a defensive
    # ``isinstance(plan, _SectionRefusal)`` seam, but that one writes
    # a misleading ``page_path=rel_path`` / ``op="create"`` envelope —
    # the MCP layer must own this translation.
    if isinstance(plan, _SectionRefusal):
        return WriteKnowledgeResult(
            page_path=None,
            page_type=plan.page_type,
            slug=plan.slug,
            collection=collection,
            op="none",
            receipt={
                "changed_pages": [],
                "deferred": [],
                "fallback_used": False,
                "fallback_reason": "",
                "errors": [plan.error],
            },
        ).model_dump()

    receipt = orch.file_back_author(plan)
    op_kind = "update" if any(p.op.name == "UPDATE" for p in receipt.changed_pages) else "create"
    return WriteKnowledgeResult(
        page_path=rel_path,
        page_type=page_type,
        slug=slug,
        collection=collection,
        op=op_kind,
        receipt=receipt.model_dump(),
    ).model_dump()


# ---------------------------------------------------------------------------
# query — synthesized answer with structured retrieval + synthesis metadata
# ---------------------------------------------------------------------------


@mcp.tool
def query(
    question: str,
    name: str | None = None,
    collection: str | None = None,
    file: bool = True,
    force_file: bool = False,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
) -> SynthesizedMcpAnswer:
    """Answer ``question`` from the wiki identified by ``name``.

    Synthesizes through ``query_synthesizer_agent`` over qmd-retrieved
    pages. ``fallback_used`` / ``fallback_reason`` report retrieval;
    ``synthesis_used`` / ``synthesis_reason`` report whether the LLM or
    the extractive fallback wrote the body.

    F3 file-back: when ``file`` is True and the synthesized answer
    marks itself ``should_file`` (or ``force_file`` flips it on), the
    answer is filed under ``wiki/<collection>/synthesis/`` and a
    structured ``file_receipt`` is returned. ``collection`` is required
    to know where the page lives; without it the orchestrator raises
    :class:`WikiPlanInvalid` and the tool re-raises that as a
    ``ToolError`` so the LLM caller can react.

    Bundle C (F15) tag filter: ``tag_expr`` is the body of a single
    include expression (no leading ``+``); ``exclude_tags`` is a list of
    size ≤ 1. Either may be set independently; together they build one
    :class:`ResolvedTagFilter` passed to the orchestrator. Each atom
    (inside ``tag_expr`` and as an ``exclude_tags`` element) may carry
    a ``t:`` / ``c:`` qualifier prefix. An unknown include atom raises
    a ``ToolError`` with the verbatim spelling; a too-long exclude
    list raises ``ToolError`` at the boundary. Both kwargs are
    additive — existing callers (no ``tag_expr``) get the unfiltered
    behavior.
    """
    if exclude_tags is not None and len(exclude_tags) > 1:
        raise ToolError(
            f"exclude_tags accepts at most one tag; got {len(exclude_tags)} ({exclude_tags!r})"
        )

    wiki = resolve_wiki(name)

    # F19 (Task 6): pre-flight validation of ``tag_expr`` /
    # ``exclude_tags`` against the registered collection set surfaces
    # unknown-tag errors and parser errors at the boundary, then the
    # raw kwargs flow into the F18 ``librarian_agent`` path. The
    # synthesized ``ResolvedTagFilter`` envelope is no longer built
    # here — the F18 librarian owns its own tag-expression semantics.
    try:
        if tag_expr is not None:
            include_ast = parse(tag_expr)
            resolve(include_ast, available=_collect_available_tags_mcp(wiki))
        if exclude_tags:
            check_qualifier(exclude_tags[0], position=0)
    except TagExprParseError as exc:
        raise ToolError(f"invalid tag expression: {exc}") from exc
    except TagExprEmpty as exc:
        raise ToolError(f"empty tag expression: {exc}") from exc
    except TagExprUnknown as exc:
        raise ToolError(format_unknown_tag_error(exc)) from exc

    orch = Orchestrator(wiki=wiki)
    # F18/F19 (Task 6): ``run_query`` now takes the librarian-threading
    # kwargs (``tag_expr`` + ``exclude_tags`` + ``file_back``) and
    # returns a ``QueryAnswer`` rather than a ``SynthesizedAnswer``.
    # The pre-F18 ``tag_filter=ResolvedTagFilter(...)`` envelope was
    # retired; tag-filter plumbing flows through the orchestrator's
    # ``LibrarianDeps`` → ``librarian_agent`` path. The MCP boundary
    # passes the F19 kwargs through directly: ``tag_expr`` and
    # ``exclude_tags`` are already strings / list[str] on the wire,
    # not the synthesized AST that the legacy path consumed.
    try:
        ans = orch.run_query(
            question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
            file_back=file,
        )
    except Exception as exc:  # noqa: BLE001 - orchestration surfaces upstream
        raise ToolError(f"orchestrator failure: {type(exc).__name__}: {exc}") from exc

    # Build ``Citation`` envelopes from the synthesizer's emitted
    # citation paths. The new ``QueryAnswer.citations`` carries the
    # threaded heading context (Task 6) so the citation surface
    # preserves the F19 ``[[slug]]: "verbatim"`` shape.
    page_read_for_path: dict[str, object] = {}
    # ``QueryAnswer.citations`` is ``list[str]`` (F19 paths). Pre-F18
    # ``SynthesizedAnswer.citations`` is ``list[Citation]``. The MCP
    # wire shape is the latter; coerce path strings into Citation
    # envelopes and pass-through ``Citation`` objects unchanged.
    raw_citations = ans.citations
    citations: list[Citation] = []
    for entry in raw_citations:
        if isinstance(entry, Citation):
            citations.append(entry)
        else:
            path = str(entry)
            citations.append(
                Citation(
                    path=path,
                    source=cast(
                        Literal["library", "wiki"], _source_for_path(path, page_read_for_path)
                    ),
                )
            )
    # Pre-F18 mocks/tests pass a full ``SynthesizedAnswer`` envelope.
    # The new F19 path returns ``QueryAnswer`` only. Surface whichever
    # provenance fields the answer carries; defaults preserve the F19
    # shape (the F18 librarian does not emit ``fallback_used`` or
    # ``pages_read`` / ``synthesis_reason``).
    fallback_used = bool(getattr(ans, "fallback_used", False))
    fallback_reason = getattr(ans, "fallback_reason", None) or None
    synthesis_used = bool(getattr(ans, "synthesis_used", True))
    synthesis_reason = getattr(ans, "synthesis_reason", None) or None
    raw_pages_read = getattr(ans, "pages_read", None)
    if (
        isinstance(raw_pages_read, list)
        and raw_pages_read
        and isinstance(raw_pages_read[0], Citation)
    ):
        pages_read: list[Citation] = [cast(Citation, p) for p in raw_pages_read]
    else:
        pages_read = []
    return SynthesizedMcpAnswer(
        answer=ans.answer,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        citations=citations,
        pages_read=pages_read,
        claim_citations=list(ans.claim_citations),
        changed_pages=list(getattr(ans, "changed_pages", [])),
        synthesis_used=synthesis_used,
        synthesis_reason=synthesis_reason,
        should_file=ans.should_file,
        file_receipt=getattr(ans, "file_receipt", None),
        searched_scope=list(getattr(ans, "searched_scope", [])),
        # The new ``QueryAnswer`` carries ``format_hint`` (F19); the
        # pre-F18 surface used ``format``. Tolerate either so legacy
        # mocks / SynthesizedAnswer stubs continue to work without
        # the MCP boundary knowing about every field renumber.
        format=getattr(ans, "format_hint", None) or getattr(ans, "format", "md"),
    )


def _source_for_path(path: str, _page_read_for_path: dict[str, object]) -> str:
    """Discriminate ``"library"`` vs ``"wiki"`` from the citation path.

    Library pages surface as ``<coll>/...``; wiki pages as
    ``wiki/...``. The F18 librarian's evidence bundle carries the
    authoritative discriminator; the MCP layer's lightweight
    derivation is good enough for the wire envelope and stays
    consistent with the source rule in
    ``SynthesizedAnswer.pages_read``.
    """
    if path.startswith("wiki/"):
        return "wiki"
    return "library"


@mcp.tool
def answer(
    question: str,
    name: str | None = None,
    collection: str | None = None,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
) -> str:
    """Answer ``question`` from the wiki as plain text.

    Surface alias for the ``query`` tool that returns ONLY the answer
    body. ``query`` returns a structured envelope (``answer`` plus
    metadata: ``citations``, ``pages_read``, ``synthesis_reason``,
    ``fallback_used``); ``answer`` returns just the ``answer`` string so
    the tool result renders as the actual response text in chat.

    Use ``answer`` when the caller wants the answer body surfaced in
    chat. Use ``query`` when the caller needs the structured envelope
    (citations to follow up on, file-receipt details, scope).

    Same tag-filter surface as ``query``. ``collection`` is only
    required if the synthesis path wants to file the answer back; by
    default this tool runs with ``file=False``.
    """
    result = query(
        question=question,
        name=name,
        collection=collection,
        file=False,
        force_file=False,
        tag_expr=tag_expr,
        exclude_tags=exclude_tags,
    )
    return result.answer


def _collect_available_tags_mcp(wiki: Wiki) -> set[str]:
    """Return every addressable tag in the library (MCP surface).

    Thin shim over :func:`lies.library.registry.library_collection_names`
    — kept so the MCP ``query`` / ``answer`` boundary has the same
    helper name as its CLI counterpart. ``wiki`` is accepted for
    signature uniformity with the legacy per-wiki resolution but is
    intentionally ignored: collections live in the library, not in
    any wiki.

    Each collection name is added with the ``c:`` qualifier prefix so
    the F15 tag-expression validator recognizes ``c:<name>`` atoms as
    addressable on the MCP ``query`` / ``answer`` path (Fix 3 / Task 3
    brief). Each ``LibraryCollectionConfig.tags`` entry is added with
    the ``t:`` qualifier prefix so ``t:<tag>`` filters against a
    library-collection tag do not raise ``TagExprUnknown`` (Fix 6 /
    Task 8 brief). Both lookups are wrapped in ``try/except`` so an
    uninitialized library — or any other registry failure — does not
    break the validator; the function still returns a set, just one
    that does not include library tags of the failed surface.
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
            tags.add(f"t:{tag}")
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
    wiki = resolve_wiki(name)
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
    """Raw contents of ``wiki/index.md`` (empty string if absent).

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
    wiki = resolve_wiki(name)
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
    """Raw contents of ``wiki/lint-report.md`` (empty string if absent).

    Direct Python entry point — accepts an explicit ``name`` kwarg so
    tests and REPL callers don't have to mutate ``LIES_WIKI_NAME``.
    Mirrors the ``query`` / ``answer`` / ``init_wiki`` tool surface.
    The FastMCP wire protocol calls the registered zero-arg handler
    above; this function is the parity surface for direct callers.
    """
    return _wiki_lint_report_impl(name)


def _safe_page_path(wiki: Wiki, path: str) -> Path:
    """Resolve ``path`` under ``wiki.wiki_dir`` and reject escapes.

    ``path`` is wiki-dir-relative. Absolute paths, ``..`` traversal,
    and any path that resolves outside ``wiki.wiki_dir`` are rejected
    with :class:`WikiPlanInvalid`. Missing files are not an error at
    this layer — callers decide what to do with the returned path.
    """
    if not path:
        raise WikiPlanInvalid("page path is empty")
    candidate = Path(path)
    if candidate.is_absolute():
        raise WikiPlanInvalid(f"page path must be relative: {path}")
    if any(part == ".." for part in candidate.parts):
        raise WikiPlanInvalid(f"page path contains '..': {path}")
    resolved = (wiki.wiki_dir / candidate).resolve()
    try:
        resolved.relative_to(wiki.wiki_dir.resolve())
    except ValueError as exc:
        raise WikiPlanInvalid(f"page path escapes wiki/: {path}") from exc
    return resolved


def _wiki_page_impl(path: str, name: str | None = None) -> str:
    """Return the raw markdown of any page under ``wiki/``.

    ``path`` is relative to ``<wiki.data_root>/wiki/``. Absolute paths,
    ``..`` traversal, and any path that resolves outside the wiki are
    rejected with :class:`WikiPlanInvalid`. Missing files return ``""``
    (the resource exists; the page just hasn't been written yet).
    """
    wiki = resolve_wiki(name)
    resolved = _safe_page_path(wiki, path)
    if not resolved.exists():
        return ""
    return resolved.read_text(encoding="utf-8")


@mcp.resource("wiki://page/{path}")
def wiki_page(path: str, name: str | None = None) -> str:
    """Raw markdown of any page under ``wiki/`` (relative ``path``).

    Template-resource handler — unlike the static resources above,
    FastMCP passes ``path`` directly so the forwarder forwards it to
    :func:`_wiki_page_impl`. The optional ``name`` kwarg keeps the
    Python-callable surface — and the MCP tool parity — consistent with
    ``query`` / ``answer`` / ``init_wiki``, which all accept ``name``;
    FastMCP itself never passes it, so the env-default wiki is used.
    """
    return _wiki_page_impl(path, name)


# ---------------------------------------------------------------------------
# wiki_changes — JSONL sidecar reader (tool + resource)
# ---------------------------------------------------------------------------


@mcp.tool
def wiki_changes(
    limit: int = 10,
    page: str | None = None,
    op: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Return recent ``MemoryPlan`` applications from the JSONL sidecar.

    Filters compose with AND. ``page`` is a substring match on each
    plan's pages list. ``op`` matches any op-kind in the histogram.
    Returns an empty list when the sidecar is unavailable.
    """
    from lies.memory import sidecar

    wiki = resolve_wiki()
    try:
        rows = sidecar.read_recent(wiki, limit=limit, page=page, op=op, since=since)
    except OSError:
        return []
    return [row.model_dump() for row in rows]


def _wiki_memory_changes_impl(name: str | None = None) -> str:
    """Render recent ``MemoryPlan`` applications as formatted text.

    Matches the layout of ``lies memory`` (the CLI counterpart): one
    4-line block per record (ts + SHA[:12] + rationale, pages, ops,
    evidence count). Missing-sidecar and ``OSError`` paths surface as
    text — the resource handler must never raise.
    """
    from lies.memory import sidecar

    wiki = resolve_wiki(name)
    out = "Recent MemoryPlan applications:\n"
    try:
        rows = sidecar.read_recent(wiki, limit=10)
    except OSError as exc:
        return out + f"sidecar unavailable: {exc}\n"
    if not rows:
        return out + "(no plans recorded yet)\n"
    for rec in rows:
        out += sidecar.format_record_block(rec)
    return out


@mcp.resource("wiki://memory-changes")
def wiki_memory_changes() -> str:
    """Recent invisible wiki writes (formatted text).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_memory_changes_impl`; ``name`` is resolved from the env
    there.
    """
    return _wiki_memory_changes_impl()


# ---------------------------------------------------------------------------
# wiki://catalog — sqlite catalog read-through (F4b)
# ---------------------------------------------------------------------------


def _wiki_catalog_impl(name: str | None = None) -> str:
    """Structured list of every catalog row.

    Each row is the JSON-serialized ``CatalogPage.model_dump(mode="json")``
    of one row in ``<wiki_dir>/.lies/catalog.db``. The shape mirrors
    ``lies catalog dump --json``. The empty-catalog case returns ``"[]"``
    so the JSON shape is stable for LLM callers.
    """
    import json

    from lies.memory.catalog import list_pages as _catalog_list_pages
    from lies.memory.catalog import open_catalog as _open_catalog

    wiki = resolve_wiki(name)
    conn = _open_catalog(wiki)
    try:
        pages = _catalog_list_pages(conn)
    finally:
        conn.close()
    return json.dumps([p.model_dump(mode="json") for p in pages], indent=2)


@mcp.resource("wiki://catalog")
def wiki_catalog() -> str:
    """All catalog rows (JSON-serialized ``list[dict]``).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_catalog_impl`; ``name`` is resolved from the env there.
    """
    return _wiki_catalog_impl()


def _wiki_catalog_slug_impl(slug: str, name: str | None = None) -> str:
    """Single catalog row by slug. Empty when not found.

    Returns ``""`` (not a JSON object) when the slug is absent so an LLM
    caller can distinguish "missing" from "present with empty fields"
    cheaply. ``model_dump(mode="json")`` ensures the ``PageSection``
    enum serializes as ``"wiki"`` / ``"ingested"`` rather than the enum
    repr.
    """
    import json

    from lies.memory.catalog import get_page as _catalog_get_page
    from lies.memory.catalog import open_catalog as _open_catalog

    wiki = resolve_wiki(name)
    conn = _open_catalog(wiki)
    try:
        page = _catalog_get_page(conn, slug)
    finally:
        conn.close()
    if page is None:
        return ""
    return json.dumps(page.model_dump(mode="json"), indent=2)


@mcp.resource("wiki://catalog/{slug}")
def wiki_catalog_slug(slug: str) -> str:
    """Single catalog row by slug (JSON-serialized ``dict`` or ``""``).

    Template-resource handler — FastMCP passes ``slug`` directly so the
    forwarder forwards it to :func:`_wiki_catalog_slug_impl`.
    """
    return _wiki_catalog_slug_impl(slug)


# ---------------------------------------------------------------------------
# Prompt — starter templates for the synthesizer paths
# ---------------------------------------------------------------------------


@mcp.prompt(name="answer")
def ask_wiki_answer(text: str) -> str:
    """Starter prompt that templates an ``answer`` tool invocation.

    Single-arg form: the entire slash-command input is passed verbatim
    as ``text``. Claude Code's slash-command dispatcher forwards the
    rest of the line as one string when the prompt has a single
    positional arg. The filter-syntax parser runs here so the calling
    LLM never has to fill ``tag_expr`` / ``exclude_tags`` slots.

    Chat-surface counterpart to the synthesized answer path: the LLM
    calls the ``answer`` tool (returns plain text) instead of ``query``
    (returns structured envelope). Use this when the response needs to
    render verbatim in chat rather than behind a collapsible JSON block.

    Filter syntax (parsed out of the ``text`` argument here, so the
    calling LLM never has to fill ``tag_expr`` / ``exclude_tags``
    slots — that was the live hallucination bug):

    - ``+c:<name>`` — include the named library collection
    - ``+t:<tag>`` — include pages tagged ``<tag>``
    - ``+a&b`` — AND two include atoms (no spaces)
    - ``+a|b`` — OR (lower precedence than ``&``)
    - ``-t:<tag>`` — exclude pages tagged ``<tag>``
    - ``-"airflow provider"`` — exclude with quoted tag

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

    Mirrors the CLI grammar exactly (see
    ``features/tag-filter-language/2026-09-02-tag-filter-language-design.md``
    and ``src/lies/query/tag_expr.py:parse_query_argv``).

    The ``name="answer"`` override registers the prompt as the
    ``/answer`` slash command even though the Python function is named
    ``ask_wiki_answer`` (the bare name conflicts with the ``answer``
    tool defined elsewhere in this module).
    """
    import shlex

    argv = shlex.split(text) if text.strip() else []
    if not argv:
        return _filter_parse_error_prompt(text, ValueError("empty input"))

    try:
        parsed_question, include_ast, exclude, _qualifier = parse_query_argv(argv)
    except (TagExprParseError, TagExprEmpty) as exc:
        return _filter_parse_error_prompt(text, exc)

    tag_expr = _render_include(include_ast) if include_ast is not None else None
    exclude_tags = [exclude] if exclude is not None else []

    return _render_answer_prompt_body(
        question=parsed_question,
        tag_expr=tag_expr,
        exclude_tags=exclude_tags,
        name=None,
        collection=None,
    )


def _render_answer_prompt_body(
    *,
    question: str,
    tag_expr: str | None,
    exclude_tags: list[str],
    name: str | None,
    collection: str | None,
) -> str:
    """Render the prompt body for the parsed args.

    No fillable slots for ``tag_expr`` / ``exclude_tags``: the slash
    prompt parses them out of the ``question`` argument before the
    calling LLM sees the body. The LLM only has to forward the
    rendered kwargs verbatim to the ``answer`` tool.
    """
    return (
        f"Call the `answer` MCP tool with the following args, then surface "
        f"the answer body verbatim in your reply:\n\n"
        f"  question: {question}\n"
        f"  tag_expr: {tag_expr!r}\n"
        f"  exclude_tags: {exclude_tags!r}\n"
        f"  name: {name!r}\n"
        f"  collection: {collection!r}\n\n"
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


@mcp.prompt(name="cite")
def cite(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    top_k: int = 3,
) -> str:
    """Slash that routes to the Archivist (F34 ground tool) and renders
    the digest as ``[[collection/slug]] (Title): "<snippet>"`` lines.

    Mirrors ask's ``archivist-prompt.md``: classify → search → read →
    cite. The parent LLM does the rendering; the prompt only templates
    the tool call and the render form. On ``ArchivistCoverageError``
    the prompt tells the LLM to surface verbatim — no silent retry.

    Args:
        question: Natural-language fragment to ground.
        tag_expr: Body of a single include token (no leading sigil),
            e.g. ``"airflow&postgres"``. ``None`` for untagged.
        exclude_tags: NOT tags without leading sigil. Forwarded to
            ``ground()`` unchanged.
        top_k: Maximum citations requested (default 3; ground()
            clamps to ``[1, 10]``).

    Returns:
        Prose that templates a ``ground`` tool call and the
        ``[[collection/slug]] (Title): "<snippet>"`` render form.
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
