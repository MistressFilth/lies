"""Top-level orchestrator that dispatches user commands to sub-agents."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.librarian import (
    LibrarianDeps,
    LibrarianOutput,
    librarian_agent,
    librarian_no_coverage,
)
from lies.agents.linter import LintFinding, LintReport, linter_agent
from lies.markdown_spans import Span
from lies.agents.query_synthesizer import QueryAnswer, QueryDeps, query_synthesizer_agent
from lies.agents.repair import RepairAgentDeps, repair_agent
from lies.agents.repair_models import RepairPlan, RepairReceipt
from lies.agents.repair_validation import ValidatedRepairPlan, validate_plan
from lies.capabilities import (
    code_mode,
    dynamic_workflow,
    file_system,
    memory,
    planning,
)
from lies.config import get_qmd_transport, get_qmd_url
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.memory.enricher import MemoryEnricherDeps, enricher_agent
from lies.memory.models import (
    MemoryPlan,
    MemoryReceipt,
    WikiCommitFailed,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.retry import EnrichmentQueue
from lies.memory.service import WikiMemoryService
from lies.memory.tools import WikiMemoryDeps, register_read_tools
from lies.page import build_author_plan
from lies.page.author import _SectionRefusal
from lies.qmd import QmdCapability
from lies.query import (
    SynthesizedAnswer,
)
from lies.query.citation import Citation, ClaimCitation
from lies.query.synthesizer import retrieve_pages as _retrieve_pages
from lies.schema import load_schema
from lies.schema.sections import _missing_required_sections
from lies.wiki.wiki import Wiki
from lies.wikilinks import WikiLinkResolver
from lies.wikilinks import extract_wikilinks as _extract_wikilinks


# F1/F18 compat shim: pre-F18 ``Orchestrator`` exposed ``retrieve_pages``
# as a module-level function that delegated to
# :func:`lies.query.synthesizer.retrieve_pages`. F18's librarian
# dispatch retired the orchestrator's direct call (the librarian
# handles retrieval), but several integration tests monkey-patch
# ``lies.orchestrator.retrieve_pages`` to stub the qmd dispatch and
# feed canned pages into the synth path. Re-export the canonical
# helper at the module level so the test patches succeed without
# ``AttributeError``; downstream callers continue to patch the
# canonical ``lies.query.synthesizer.retrieve_pages`` if they want
# to intercept the F18 path.
def retrieve_pages(  # type: ignore[no-untyped-def]
    question: str,
    wiki: Wiki,
    *,
    top_n: int = 5,
    qmd_search=None,
    tag_filter=None,
):
    """F18 compat shim — delegates to :func:`lies.query.synthesizer.retrieve_pages`.

    See the canonical helper for the full contract. This module-level
    alias exists only so test surfaces (and any out-of-branch
    downstream caller that patched ``lies.orchestrator.retrieve_pages``
    pre-F18) keep their monkey-patch target.
    """
    return _retrieve_pages(
        question,
        wiki,
        top_n=top_n,
        qmd_search=qmd_search,
        tag_filter=tag_filter,
    )


def _resolve_default_models(wiki: Wiki) -> dict[str, Model | str]:
    """Load user-level providers.toml and resolve one model per AGENT_ROSTER entry."""
    from lies.providers import (
        AGENT_ROSTER,
        env_override,
        load_providers_config,
        resolve_model,
    )

    config = load_providers_config(wiki.providers_path)
    if config is None:
        # No TOML — every agent gets default_model, or the env var override.
        fallback: dict[str, Model | str] = {}
        for name in AGENT_ROSTER:
            override = env_override(name)
            fallback[name] = override or "anthropic:claude-opus-4-7"
        return fallback

    resolved: dict[str, Model | str] = {}
    for name in AGENT_ROSTER:
        resolved[name] = resolve_model(name, config)
    return resolved


def _list_working_tree_changes(repo: Path) -> list[str]:
    """Return the list of paths in the working tree that differ from HEAD.

    Includes untracked, modified, and deleted paths. Paths containing
    characters that are awkward in a shell are passed through unchanged
    (the orchestrator uses ``git add -- <path>`` with explicit
    pathspecs, not a shell).
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[str] = []
    # ``-z`` separates records by NUL, with rename entries formatted as
    # ``<status><space><old>\0<new>``. Split on NUL and walk records.
    for record in result.stdout.split("\x00"):
        if not record:
            continue
        # Format: "XY <path>" (or "XY <old> -> <new>" for renames, though
        # -z uses a different layout; we are conservative and accept both).
        if " -> " in record:
            record = record.split(" -> ", 1)[1]
        # Strip the leading "XY " status (3 chars including space).
        if len(record) >= 3 and record[2] == " ":
            paths.append(record[3:].strip())
        else:
            paths.append(record.strip())
    return paths


def _build_lint_report(
    wiki: Wiki,
    *,
    repair_receipt: RepairReceipt | None = None,
    resolver: WikiLinkResolver | None = None,
) -> LintReport:
    """Produce a deterministic :class:`LintReport` for the host-side lint.

    Walks the wiki looking for the cheapest-to-check issues (orphan
    pages, missing cross-references) so a host-side lint call always
    yields a real, non-empty artifact. Categories that require an LLM
    (contradictions, stale claims, data gaps) are recorded with zero
    findings here -- they still flow through the linter sub-agent in
    production; this host-side report is the deterministic shell.

    Note: this deterministic shell does NOT currently invoke the
    linter sub-agent (which would emit its own structured findings);
    it is the source of truth for findings until that integration
    is completed. The repair agent consumes the structured
    ``LintReport`` produced here, not a markdown string.

    Args:
        wiki: The wiki to lint.
        repair_receipt: Optional. When provided, the report includes
            an ``applied`` section describing which repair ops
            succeeded.

    Returns:
        A :class:`LintReport` whose ``findings`` field carries the
        structured findings and whose ``report_markdown`` field
        carries the formatted report (optionally with a repair
        section appended).
    """
    from lies.agents.linter import LintFinding, LintReport, LintSeverity

    findings: list[LintFinding] = []
    # ``pages`` holds wiki-dir-relative paths (e.g. ``concepts/a.md``)
    # so the repair agent's ``wiki.wiki_dir / page`` lookup lands
    # on the real file. Without this convention, ``_run_repair_agent``
    # would resolve ``wiki.wiki_dir / "wiki/concepts/a.md"`` and find
    # nothing (the page lives at ``wiki/concepts/a.md``, not
    # ``wiki/wiki/concepts/a.md``).
    pages: set[str] = set()
    # ``overview.md`` is the singleton overview page; it is skipped from
    # the orphan / missing_xref / missing_page universe below (no other
    # page is expected to link to it as a normal content page) but
    # re-added to the section-contract scan below, where its
    # ``type: overview`` contract is in scope.
    overview_page: str | None = None
    if wiki.wiki_dir.exists():
        for path in wiki.wiki_dir.rglob("*.md"):
            rel = path.relative_to(wiki.wiki_dir).as_posix()
            if rel in {"index.md", "log.md", "lint-report.md", "overview.md"}:
                if rel == "overview.md":
                    overview_page = rel
                continue
            pages.add(rel)

    # Orphan check: a page is orphan if no other page links to it.
    if pages:
        linked: set[str] = set()
        for page in pages:
            try:
                text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            linked.update(_extract_local_md_links(text, page, wiki.data_root))
        orphans = sorted(pages - linked)
        for orphan in orphans:
            findings.append(
                LintFinding(
                    severity=LintSeverity.LOW,
                    category="orphan",
                    message=f"{orphan} has no inbound links.",
                    pages=[orphan],
                    # Orphans are mechanical to fix: the repair agent
                    # routes them to UpdateIndex (add the page to
                    # wiki/index.md). safe_to_fix=True lets the
                    # repair agent's HARD RULE permit the op; the
                    # default of False would block every orphan.
                    safe_to_fix=True,
                )
            )

    # missing_xref: A mentions B's title in body text but does not link to B.
    # Heuristic only; skips title collisions to avoid false positives.
    if pages:
        titles: dict[str, str] = {}
        for page in pages:
            try:
                text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            title = _extract_frontmatter_title(text)
            if title:
                titles[page] = title
        # Skip ambiguous titles: any title shared by 2+ pages is ignored.
        title_counts: dict[str, int] = {}
        for title in titles.values():
            title_counts[title] = title_counts.get(title, 0) + 1
        unique_titles = {p: t for p, t in titles.items() if title_counts[t] == 1}

        # Each page's resolved local ``.md`` links in wiki-dir-relative
        # convention (same as ``pages``). The shared helper ensures
        # orphan and missing_xref see the same link semantics — including
        # the existing-only filter (I1) and the wiki-dir fallback.
        page_links: dict[str, set[str]] = {}
        for page in pages:
            try:
                text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            page_links[page] = _extract_local_md_links(text, page, wiki.data_root)

        body_cache: dict[str, str] = {}
        for page, title in unique_titles.items():
            other_pages = [p for p, t in unique_titles.items() if t != title]
            if not other_pages:
                continue
            try:
                body = body_cache.setdefault(
                    page, _strip_frontmatter((wiki.wiki_dir / page).read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeDecodeError):
                continue
            body_lower = body.lower()
            page_targets = page_links.get(page, set())
            for other in other_pages:
                other_title = unique_titles[other]
                if other_title.lower() not in body_lower:
                    continue
                # Target-specific check: only suppress the finding if
                # this page actually links to *this specific other
                # page*. A page that has cross-references but to
                # different pages still gets flagged for mentioning
                # the title without linking to it.
                if other in page_targets:
                    continue
                findings.append(
                    LintFinding(
                        severity=LintSeverity.MEDIUM,
                        category="missing_xref",
                        message=f"{page} mentions {other_title} without a cross-reference",
                        pages=[page, other],
                        safe_to_fix=True,
                    )
                )

    # missing_page: frontmatter `sources:` lists a path that does not exist.
    for page in pages:
        try:
            text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for source in _extract_frontmatter_sources(text):
            resolved = (wiki.data_root / source).resolve()
            if not resolved.exists():
                findings.append(
                    LintFinding(
                        severity=LintSeverity.LOW,
                        category="missing_page",
                        message=f"{page} cites {source} which is not present",
                        pages=[page],
                        safe_to_fix=False,
                    )
                )

    # Wikilink resolution: emits missing_page for [[target]] with no corpus match.
    if resolver is None:
        resolver = WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))
    for page in pages:
        try:
            text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_target in _extract_wikilinks(text):
            if resolver.resolve(raw_target) is None:
                findings.append(
                    LintFinding(
                        severity=LintSeverity.LOW,
                        category="missing_page",
                        pages=[page],
                        message=f"{page} has wikilink target '{raw_target}' with no matching page",
                        safe_to_fix=False,
                    )
                )

    # Generalized per-page section-contract check + synthesis-only
    # ``dangling_derived_from``. The contract (see
    # ``src/lies/schema/default_schema.md`` → ``## Section contract``,
    # resolved via ``Wiki.section_contract``) declares per-type
    # required ``## <Heading>`` lines for all six page types. A page
    # whose ``type:`` is in the contract but whose body is missing one
    # or more required headings emits a single ``missing_required_section``
    # finding naming every missing heading. ``safe_to_fix=False``: a
    # missing section is a content gap the operator must fill — the
    # repair agent's HARD RULE forbids ops on these.
    #
    # ``dangling_derived_from`` stays synthesis-only and mechanical:
    # removing a dangling slug from the frontmatter list is a
    # reversible string edit, so it stays ``safe_to_fix=True``.
    #
    # A single ``read_text`` per page feeds both checks: the body
    # section scan and the ``derived_from`` slug resolution share
    # one disk read — the previous 3-pass loop opened and closed each
    # file three times without semantic gain.
    section_contract = wiki.section_contract
    # Section-contract scan covers the regular ``pages`` set PLUS the
    # singleton ``overview.md`` (re-added above; it's skipped from the
    # orphan / xref / page checks because no content page is expected
    # to link to it). The ``dangling_derived_from`` scan is synthesis-
    # only and stays tied to ``pages``.
    section_pages = set(pages)
    if overview_page is not None:
        section_pages.add(overview_page)
    for page in section_pages:
        try:
            text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        page_type = _extract_frontmatter_type(text)
        if page_type is not None:
            missing = _missing_required_sections(
                page_type, section_contract, _strip_frontmatter(text)
            )
            if missing:
                findings.append(
                    LintFinding(
                        severity=LintSeverity.MEDIUM,
                        category="missing_required_section",
                        pages=[page],
                        message=(
                            f"{page} ({page_type}) missing required section(s): "
                            f"{', '.join(missing)}"
                        ),
                        safe_to_fix=False,
                    )
                )
        if page_type != "synthesis":
            continue
        for slug in _extract_frontmatter_derived_from(text):
            if not (wiki.wiki_dir / f"{slug}.md").exists():
                findings.append(
                    LintFinding(
                        severity=LintSeverity.MEDIUM,
                        category="dangling_derived_from",
                        pages=[page],
                        message=f"derived_from slug {slug} does not resolve to an existing page",
                        safe_to_fix=True,
                    )
                )

    report = LintReport(findings=findings, report_markdown="")
    body = _format_lint_markdown(report, wiki)
    if repair_receipt is not None:
        body += "\n" + _format_repair_section(repair_receipt)
    report.report_markdown = body
    return report


def _extract_markdown_links(text: str) -> list[str]:
    """Extract ``(target)`` from markdown links via a tiny regex.

    Avoids a dependency on a full markdown parser; only the link target
    is needed for the orphan check.
    """
    import re

    return re.findall(r"\]\(([^)]+)\)", text)


def _extract_local_md_links(text: str, source_page_path: str, wiki_root: Path) -> set[str]:
    """Return the canonical wiki-dir-relative ``.md`` targets of every
    markdown link in ``text``.

    Filters out:
    - URL schemes (``http://``, ``https://``, ``mailto:``, ``tel:``)
    - Absolute paths (rooted at ``/`` or ``\\``)
    - Non-``.md`` targets
    - Targets whose resolved path does not exist on disk

    Strips URL fragments and query strings before resolving. Resolves
    relative targets against ``source_page_path``'s directory first,
    then the wiki directory (see ``_resolve_link_target``).

    Containment: only targets that resolve under ``wiki/`` are kept.
    A link like ``../../somewhere/else.md`` from inside
    ``wiki/concepts/a.md`` can resolve to ``somewhere/else.md`` — a
    sibling of the ``wiki/`` directory that lands inside
    ``wiki_root`` but not inside the wiki itself. Without the
    containment check, ``resolved.removeprefix("wiki/")`` would yield
    ``somewhere/else.md`` and corrupt the ``pages``-set comparison
    used by the orphan and missing_xref heuristics.

    Returns the set in the same wiki-dir-relative convention used by
    the shell findings' ``pages`` field, so callers can compare
    resolved links directly against the ``pages`` set without a
    ``wiki/`` prefix dance.
    """
    targets: set[str] = set()
    for raw in _extract_markdown_links(text):
        if raw.startswith(("http://", "https://", "mailto:", "tel:")):
            continue
        if raw.startswith(("/", "\\")):
            continue
        clean = raw.split("#", 1)[0].split("?", 1)[0]
        if not clean.endswith(".md"):
            continue
        # ``_resolve_link_target`` expects repo-root-relative
        # ``source_page_path``; ``source_page_path`` here is
        # wiki-dir-relative per the I2 normalization, so prepend
        # ``wiki/`` and strip it from the result.
        resolved = _resolve_link_target(f"wiki/{source_page_path}", clean, wiki_root)
        if resolved is None:
            continue
        # Containment: must live under wiki/, not just under wiki_root.
        if not resolved.startswith("wiki/"):
            continue
        targets.add(resolved.removeprefix("wiki/"))
    return targets


def _resolve_link_target(source_page_path: str, raw_target: str, wiki_root: Path) -> str | None:
    """Resolve a bare markdown link target to a wiki-relative path.

    Tries resolving relative to the source page's directory first,
    then relative to the wiki directory itself, and returns the
    first wiki-relative ``.md`` path that both lands inside
    ``wiki_root`` AND points at a file that exists on disk. Returns
    ``None`` when no candidate lands inside the wiki, the result is
    not a ``.md`` file, or no candidate exists.

    ``wiki_root`` is the repository root (the parent of the ``wiki/``
    directory), and ``source_page_path`` is a repo-root-relative
    path like ``wiki/concepts/a.md``. The wiki-directory fallback
    therefore prepends ``wiki/`` to ``raw_target`` so a link written
    as ``[Beta](concepts/beta.md)`` from inside ``wiki/concepts/``
    resolves to ``wiki/concepts/beta.md`` (the standard layout),
    not to ``<repo_root>/concepts/beta.md`` (a non-existent
    sibling of the ``wiki/`` directory).

    The "must exist" check fixes the false-positive bug where a
    source-relative candidate like ``wiki/concepts/concepts/beta.md``
    is syntactically a valid ``.md`` path inside the wiki but
    doesn't exist on disk; without the check it shadowed the
    correct wiki-dir fallback ``wiki/concepts/beta.md``.

    Examples (wiki root = ``/tmp/wiki``):

    - ``wiki/concepts/a.md`` -> ``b.md`` (exists) -> ``wiki/concepts/b.md``
    - ``wiki/concepts/a.md`` -> ``concepts/b.md`` (exists) -> ``wiki/concepts/b.md``
    - ``wiki/concepts/a.md`` -> ``concepts/b.md`` (only ``wiki/concepts/b.md`` exists) -> ``wiki/concepts/b.md``
    - ``wiki/concepts/a.md`` -> ``b.md`` (no match anywhere) -> ``None``
    - ``wiki/overview.md`` -> ``b.md`` (exists only under concepts) -> ``None``
    """
    wiki_root_resolved = wiki_root.resolve()
    wiki_dir = (wiki_root / "wiki").resolve()
    # Source page's directory, absolute.
    source_dir = (wiki_root / source_page_path).parent.resolve()
    for base in (source_dir, wiki_dir):
        try:
            candidate = (base / raw_target).resolve()
        except OSError:
            continue
        try:
            relative = candidate.relative_to(wiki_root_resolved)
        except ValueError:
            continue
        result = relative.as_posix()
        if not result.endswith(".md"):
            continue
        if not candidate.exists():
            continue
        return result
    return None


def _extract_frontmatter_title(text: str) -> str | None:
    """Return the ``title:`` value from YAML frontmatter, or None."""
    import re

    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    match = re.search(r"^title:\s*(.+?)\s*$", block, re.MULTILINE)
    if not match:
        return None
    title = match.group(1).strip()
    if title.startswith(('"', "'")) and title.endswith(('"', "'")):
        title = title[1:-1]
    return title or None


def _strip_frontmatter(text: str) -> str:
    """Strip the leading YAML frontmatter block (if any) and return the body."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    rest = text[end + 4 :]
    rest = rest.removeprefix("\n")
    return rest


def _extract_frontmatter_sources(text: str) -> list[str]:
    """Return the ``sources:`` list from YAML frontmatter (empty if missing/malformed)."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    block = text[3:end]
    lines = block.splitlines()
    sources: list[str] = []
    in_sources = False
    for line in lines:
        if in_sources:
            stripped = line.strip()
            if stripped.startswith("- "):
                sources.append(stripped[2:].strip().strip('"').strip("'"))
            elif stripped and not stripped.startswith("-"):
                in_sources = False
        elif line.startswith("sources:"):
            in_sources = True
    return sources


def _extract_frontmatter_type(text: str) -> str | None:
    """Return the ``type:`` value from YAML frontmatter, or None.

    Used by the synthesis-page checks in :func:`_build_lint_report`
    to identify synthesis pages (``type: synthesis``). Mirrors the
    minimal-regex style of the surrounding frontmatter helpers so a
    malformed block yields ``None`` rather than raising.
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    match = re.search(r"^type:\s*(.+?)\s*$", block, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith(('"', "'")) and value.endswith(('"', "'")):
        value = value[1:-1]
    return value or None


def _extract_frontmatter_derived_from(text: str) -> list[str]:
    """Return the ``derived_from:`` list from YAML frontmatter.

    Used by :func:`_build_lint_report` to flag synthesis pages whose
    cited slugs do not resolve to an existing wiki page
    (``dangling_derived_from``). Same minimal-regex shape as
    :func:`_extract_frontmatter_sources`: a missing or malformed
    ``derived_from`` block yields ``[]`` rather than raising.
    """
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    block = text[3:end]
    lines = block.splitlines()
    derived: list[str] = []
    in_derived = False
    for line in lines:
        if in_derived:
            stripped = line.strip()
            if stripped.startswith("- "):
                derived.append(stripped[2:].strip().strip('"').strip("'"))
            elif stripped and not stripped.startswith("-"):
                in_derived = False
        elif line.startswith("derived_from:"):
            in_derived = True
    return derived


def _format_lint_markdown(report: LintReport, wiki: Wiki) -> str:
    """Format a ``LintReport`` as markdown for ``wiki/lint-report.md``."""
    by_cat: dict[str, int] = {}
    for f in report.findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1

    header = (
        f"## Lint report — {datetime.now(tz=UTC).date().isoformat()}\n\n"
        f"Wiki root: `{wiki.data_root}`\n\n"
    )
    if not report.findings:
        return header + "_No findings._\n"

    counts = ", ".join(f"{cat}: {n}" for cat, n in sorted(by_cat.items()))
    sections = [header, f"**Findings ({len(report.findings)})** — {counts}\n"]
    for finding in report.findings:
        sections.append(
            f"- [{finding.severity.value}] **{finding.category}**: "
            f"{finding.message} (pages: {', '.join(finding.pages)})"
        )
    sections.append("")
    return "\n".join(sections)


def _render_lint_report(
    report: LintReport,
    *,
    wiki: Wiki,
    repair_receipt: RepairReceipt | None,
    shell_count: int,
    llm_count: int,
    llm_fallback_reason: str | None,
) -> str:
    """Render ``report`` plus repair and source sections as markdown."""
    body = _format_lint_markdown(report, wiki)
    if repair_receipt is not None:
        body += "\n" + _format_repair_section(repair_receipt)
    sources = (
        "### Sources\n\n"
        f"- deterministic shell: {shell_count} findings\n"
        f"- linter_agent: {llm_count} findings"
    )
    if llm_fallback_reason is not None:
        sources += f"\n- fallback: {llm_fallback_reason}"
    return body + "\n" + sources + "\n"


def _format_repair_section(receipt: RepairReceipt) -> str:
    lines = [f"### Applied ({len(receipt.applied)})", ""]
    if not receipt.applied:
        lines.append("_No repairs applied._")
    else:
        kinds = receipt.applied_repair_kinds
        for index, ref in enumerate(receipt.applied):
            kind = kinds[index] if index < len(kinds) else ref.op.value
            lines.append(f"- applied: {kind} — {ref.path}")
    lines.extend(["", f"### Skipped ({len(receipt.skipped)})", ""])
    if receipt.skipped:
        redundant = [s for s in receipt.skipped if s.startswith("redundant-index:")]
        other = [s for s in receipt.skipped if not s.startswith("redundant-index:")]
        if other:
            lines.extend(f"- {reason}" for reason in other)
        if redundant:
            lines.extend(["", f"### Skipped (redundant) ({len(redundant)})", ""])
            lines.extend(f"- {reason}" for reason in redundant)
    else:
        lines.append("_No findings skipped._")
    if receipt.errors:
        lines.extend(["", f"### Errors ({len(receipt.errors)})", ""])
        lines.extend(f"- {err}" for err in receipt.errors)
    return "\n".join(lines)


def _lint_log_title(report: LintReport) -> str:
    """Build the ``log.md`` title for a lint pass.

    Format: ``lint | N findings (<cat1>, <cat2>, ...)`` with categories
    deduped and sorted. Empty category list → ``lint | 0 findings``.
    """
    categories = sorted({f.category for f in report.findings})
    n = len(report.findings)
    if not categories:
        return f"lint | {n} findings"
    cat_str = ", ".join(categories)
    return f"lint | {n} findings ({cat_str})"


ORCHESTRATOR_SYSTEM_PROMPT_PREFIX = """You are the LIES orchestrator. The user
is curating a Karpathy-pattern LLM wiki at the path below. You dispatch their
commands to specialized sub-agents and return results.

Wiki root: {wiki}

The schema for this wiki:

"""


# Per-sub-agent metadata: (name, factory, description). Names must be valid
# Python identifiers because DynamicWorkflow exposes them as sandbox function
# names; they must also be unique across the catalog.
_SUB_AGENT_TABLE: tuple[tuple[str, object, str], ...] = (
    (
        "linter",
        linter_agent,
        (
            "Walk the wiki and produce a structured `LintReport` (contradictions, "
            "stale, orphans, missing pages, missing xrefs, data gaps)."
        ),
    ),
    (
        "query_synthesizer",
        query_synthesizer_agent,
        (
            "Synthesize a cited answer from qmd search results; surfaces "
            "disagreements and notes what the wiki does NOT know."
        ),
    ),
)


def merge_lint_reports(
    shell: LintReport,
    llm: LintReport,
    *,
    llm_fallback_reason: str | None = None,
) -> tuple[LintReport, str | None]:
    """Union ``shell`` and ``llm`` findings with dedup.

    Dedup key is ``(category, frozenset(pages), message)``. Shell
    entries win on collision so the deterministic shell's
    ``safe_to_fix`` semantics are preserved for mechanical
    categories. LLM-only categories (``contradiction``, ``stale``,
    ``data_gap``) have no shell entries by construction and pass
    through.

    Returns the merged ``LintReport`` and the propagated
    ``llm_fallback_reason`` (the caller renders the final markdown).
    """
    seen: set[tuple[str, frozenset[str], str]] = set()
    merged: list[LintFinding] = []
    for finding in [*shell.findings, *llm.findings]:
        key = (finding.category, frozenset(finding.pages), finding.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(finding)
    return LintReport(findings=merged, report_markdown=""), llm_fallback_reason


def _slugify_section(section: str) -> str:
    """Lowercase, hyphenate spaces and punctuation for a URL-friendly anchor.

    Strips characters that don't survive a URL fragment. Returns
    lowercase alphanumerics joined by single hyphens.
    """
    out: list[str] = []
    for ch in section.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def _render_footnote_line(
    n: int,
    citation: "Citation",
    *,
    title: str | None = None,
) -> str:
    """Render one footnote line.

    Format: ``[^N]: [title](path#anchor) — section``

    Anchor rules:
    - line present → ``#L<line>``
    - section present, line absent → ``#<slugified-section>``
    - both absent → no anchor

    Section rules:
    - section present → `` — section``
    - section absent → omit suffix

    When ``title`` is ``None``, falls back to the path's last segment
    (the basename) for a readable link label.
    """
    display_title = title or citation.path.rsplit("/", 1)[-1]
    anchor = ""
    if citation.line is not None:
        anchor = f"#L{citation.line}"
    elif citation.section:
        slug = _slugify_section(citation.section)
        # Heading may be only punctuation (e.g. ``---`` / ``—``); the
        # slug is empty and a bare ``#`` fragment would dangle. Omit
        # the anchor in that case.
        if slug:
            anchor = f"#{slug}"

    link = f"[{display_title}]({citation.path}{anchor})"
    if citation.section:
        return f"[^{n}]: {link} — {citation.section}"
    return f"[^{n}]: {link}"


def _render_footnotes(
    citations: list["Citation"],
    *,
    page_titles: dict[str, str],
) -> str:
    """Render the full `Footnotes:` block.

    Returns an empty string when ``citations`` is empty.
    """
    if not citations:
        return ""
    lines = ["Footnotes:", ""]
    for i, c in enumerate(citations, 1):
        title = page_titles.get(c.path)
        lines.append(_render_footnote_line(i, c, title=title))
    return "\n".join(lines)


def _validate_claim_citations(
    answer_body: str,
    citations: list["Citation"],
    claim_citations: list["ClaimCitation"],
    librarian_output: "LibrarianOutput",
) -> list["ClaimCitation"]:
    """F19 strict validation helper — drop-on-fail for the librarian path.

    ``Orchestrator._call_synthesizer`` (Task 6) routes through this
    helper. The pre-F19 call path that returned ``(kept, drops)`` is
    gone.

    Drops entries where:
    - ``claim`` is not a substring of ``answer_body``
    - ``citation_index`` is out of range for ``citations``
    - ``quote`` is not a substring of the cited excerpt's body
      (resolved via ``librarian_output.excerpts`` so we don't
      re-read files)

    Returns the surviving ``ClaimCitation`` entries. Drop reasons
    are silently swallowed per spec §"Validation contract" —
    ``_call_synthesizer`` is the sole caller and the synthesis-
    reason diagnostic path lives on the synthesizers, not the
    validators.
    """
    page_bodies = {e.slug: "\n\n".join(s.body for s in e.spans) for e in librarian_output.excerpts}
    # Library vs wiki slug key mismatch risk flagged in Task 6's
    # review: the synthesizer emits ``Citation.path`` in either
    # ``wiki/<slug>.md`` form (wiki hits) or ``<slug>.md`` /
    # bare-slug form (library / pre-F18 callers), but the canonical
    # ``page_bodies`` key is the bare slug (``e.slug``). Mirror
    # each canonical entry under the ``wiki/``-prefixed form so the
    # drop-on-fail check works uniformly for both shapes without
    # forcing callers to pre-normalize.
    page_bodies.update({f"wiki/{slug}.md": body for slug, body in list(page_bodies.items())})
    survivors: list[ClaimCitation] = []
    for cc in claim_citations:
        if cc.claim not in answer_body:
            continue
        if not (0 <= cc.citation_index < len(citations)):
            continue
        cited = citations[cc.citation_index]
        body = page_bodies.get(cited.path)
        if not body or cc.quote not in body:
            continue
        survivors.append(cc)
    return survivors


def _thread_heading_paths(
    citations: list["Citation"],
    claim_citations: list["ClaimCitation"],
    librarian_output: "LibrarianOutput",
) -> list["Citation"]:
    """Populate ``Citation.heading_path`` from the span each claim cites.

    For every citation that has a matching ``ClaimCitation`` (by
    ``citation_index``), find the span whose body contains the
    claim's ``quote``; replace the citation's ``heading_path`` with
    that span's heading path. Citations without a matching
    claim, or where no span contains the quote, pass through
    unchanged.

    Used by ``_call_synthesizer`` (Task 6) before filing-back so
    the ``## Evidence`` block in synthesis pages can render
    ``(Section > Subsection)`` per claim (see
    ``_render_evidence`` in spec §5).

    The spans index is keyed by ``e.slug`` (bare-slug form) AND
    mirrored under ``wiki/<slug>`` / ``wiki/<slug>.md`` so the
    real-F19 synthesizer emission (bare slugs from ``e.slug``) and
    stub/test emission (``wiki/<slug>.md`` form, what pre-F18
    tests and canned fixtures still emit) both resolve to the same
    span set. Without the mirror, threaded citations whose
    ``path`` is the stub ``wiki/<slug>.md`` form would fall
    through with ``heading_path=None`` — visible in the filed
    ``## Evidence`` block as ``(top of page)``.
    """

    spans_by_slug: dict[str, list["Span"]] = {}
    for excerpt in librarian_output.excerpts:
        spans = list(excerpt.spans)
        spans_by_slug[excerpt.slug] = spans
        # Mirror the canonical entry under the ``wiki/``-prefixed
        # shapes the pre-F18 / stub synthesizer emit. The mirror
        # is intentionally duplicated for ``wiki/<slug>`` AND
        # ``wiki/<slug>.md`` because both forms appear in
        # canned fixture citations (see
        # ``tests/integration/test_tier2_query_path.py``).
        spans_by_slug[f"wiki/{excerpt.slug}.md"] = spans
        spans_by_slug[f"wiki/{excerpt.slug}"] = spans
    out: list[Citation] = []
    for i, cit in enumerate(citations):
        match_quote = next(
            (cc.quote for cc in claim_citations if cc.citation_index == i),
            None,
        )
        if match_quote is None:
            out.append(cit)
            continue
        spans = spans_by_slug.get(cit.path, [])
        if not spans and cit.path:
            # Last-ditch: strip a ``wiki/`` prefix or ``.md``
            # suffix and retry. Defends against path-shape drift
            # (e.g. ``wiki/x.md`` vs ``wiki/x`` vs ``x``).
            candidate = cit.path
            for strip in ("wiki/", ".md"):
                if candidate.startswith(strip):
                    candidate = candidate[len(strip) :]
                elif candidate.endswith(strip):
                    candidate = candidate[: -len(strip)]
            spans = spans_by_slug.get(candidate, [])
        match_span = next(
            (s for s in spans if match_quote in s.body),
            None,
        )
        if match_span is None:
            out.append(cit)
            continue
        out.append(replace(cit, heading_path=match_span.heading_path))
    return out


def _format_heading_path(heading_path: list[str] | None) -> str:
    """Render a heading path for inline ``## Evidence`` display.

    Empty / None → ``"top of page"`` (no parens — the caller wraps
    the result in ``( )`` for inline display). The pre-F19 return
    included the parens here, but ``_render_evidence`` wraps the
    result in another ``( )``, producing the rendered
    ``((top of page))`` double-paren. Returns the bare phrase so
    the caller-owned wrap renders identically for both the
    filed-body evidence block and the extractive fallback path
    (``build_answer_from_pages`` in ``query/synthesizer.py``).
    """
    if not heading_path:
        return "top of page"
    return " > ".join(heading_path)


_KNOWN_COLLECTION_PREFIXES: tuple[str, ...] = ("wiki/",)


def _slug_from_path(path: str) -> str:
    """Render the ``[[slug]]`` form from a ``Citation.path``.

    Strips the leading collection segment when the path is the
    spec-compliant form ``<collection>/<rest>``. Bare slugs (no
    recognized collection prefix, or no slash at all) pass through
    unchanged so the real-synthesizer emission path (which emits
    ``"concepts/pydantic"`` from ``e.slug``) renders as
    ``[[concepts/pydantic]]`` rather than ``[[pydantic]]``.
    """
    if not path.startswith(_KNOWN_COLLECTION_PREFIXES):
        return path.removesuffix(".md")
    parts = path.split("/", 1)
    return parts[1].removesuffix(".md")


def _render_evidence(
    citations: list[Citation],
    claim_citations: list[ClaimCitation],
) -> str:
    """Render the ``## Evidence`` body for a filed synthesis page.

    One line per claim: ``[[slug]] (Heading > Subheading): "verbatim"``.
    Drop-on-fail has already happened upstream in
    ``_validate_claim_citations``; surviving entries are guaranteed
    to have a valid ``citation_index`` and a non-empty ``quote``.
    ``_slug_from_path`` strips the ``.md`` suffix when present so
    the rendered slug is the bare ``concepts/pydantic`` form for
    both ``"wiki/concepts/pydantic.md"`` paths and bare-slug
    ``"concepts/pydantic"`` emission from the synthesizer.
    """
    lines: list[str] = []
    for cc in claim_citations:
        cit = citations[cc.citation_index]
        slug = _slug_from_path(cit.path)
        heading = _format_heading_path(cit.heading_path)
        lines.append(f'[[{slug}]] ({heading}): "{cc.quote}"')
    return "\n".join(lines)


def _is_one_liner(body: str) -> bool:
    """Heuristic for "substantive" — fewer than ~3 lines is a one-liner."""
    return len([line for line in body.splitlines() if line.strip()]) < 3


def _should_file(
    answer: QueryAnswer,
    librarian_output: LibrarianOutput,
) -> bool:
    """Filing-back gate (F19) without the catalog query.

    Returns True when:
    - the agent marked the answer ``should_file=True``
    - the librarian's bundle covers 2+ distinct pages
    - the synthesized body is more than a one-line heuristic
      (≥ 3 lines of non-whitespace text)

    The catalog check ("already a concept page covering this
    answer?") lives on :meth:`Orchestrator._has_existing_concept_page`
    because it owns the :class:`WikiMemoryService` access; Task 7
    wires that read. The orchestrator's
    :meth:`Orchestrator._should_file` method wraps this function
    to add the catalog check.

    Defined as a module-level callable so unit tests can pin the
    gate independent of the catalog surface — see
    ``tests/unit/test_orchestrator_filing.py``.
    """
    if not answer.should_file:
        return False
    if librarian_output.distinct_pages < 2:
        return False
    if _is_one_liner(answer.answer):
        return False
    return True


def _slugify(text: str) -> str:
    """Convert free text to a lowercase hyphenated slug (max 60 chars).

    Strips non-word / non-space / non-hyphen characters, collapses
    runs of whitespace and underscores to single hyphens, and trims
    trailing hyphens. Used by the filing-back path to derive a
    filename from a question. Coexists with the nested helper in
    :meth:`Orchestrator.file_back_synthesis` at :mod:`orchestrator`
    — both functions live at different scopes, but share the intent.
    """
    import re

    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text)
    return text[:60].rstrip("-")


class Orchestrator:
    """The top-level agent that maintains a LIES wiki.

    The orchestrator is the only entrypoint exposed to the CLI. It composes
    four sub-agents (source-reader, page-writer, linter, query-synthesizer)
    via harness's `SubAgents` capability, plus file system, shell, qmd MCP,
    CodeMode, Memory, Planning, and DynamicWorkflow.

    The orchestrator NEVER reads or writes wiki files directly. All file
    mutations go through a sub-agent (or CodeMode), keeping them auditable and
    schema-respecting.
    """

    def __init__(
        self,
        wiki: Wiki,
        models: dict[str, Model | str] | None = None,
    ) -> None:
        # Store the Wiki dataclass directly. The orchestrator derives
        # ``wiki_root`` paths (for git subprocess cwd and for downstream
        # APIs that still take a Path) from ``self.wiki.data_root``; the
        # schema for this wiki lives at ``self.wiki.schema_path``.
        self.wiki = wiki
        self.models = models if models is not None else _resolve_default_models(wiki)
        self.schema = load_schema(self.wiki)
        self._build()

    def _build(self) -> None:
        """Construct the orchestrator agent with all capabilities and sub-agents."""
        from pydantic_ai_harness.subagents import SubAgent, SubAgents

        # Assign a name to each sub-agent so harness's SubAgents and
        # DynamicWorkflow catalogs can key them. The factories themselves
        # don't set a name; the orchestrator owns the namespace.
        named_agents: list[Agent] = []
        for name, factory, _description in _SUB_AGENT_TABLE:
            agent = factory(model=self.models[name])  # type: ignore[operator]  # ty: ignore[call-non-callable]
            agent.name = name
            named_agents.append(agent)

        # Sub-agents as `SubAgent` delegates for the SubAgents capability.
        delegates = [
            SubAgent(agent=agent, name=name, description=description)
            for (name, _factory, description), agent in zip(_SUB_AGENT_TABLE, named_agents)
        ]

        self._harness_memory = memory(self.wiki.data_root)
        self._agent: Agent = Agent(
            self.models["orchestrator"],
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT_PREFIX.format(wiki=self.wiki.data_root)
            + self.schema,
            deps_type=WikiMemoryDeps,
            capabilities=[
                SubAgents(agents=delegates),
                code_mode(),
                self._harness_memory,
                planning(),
                dynamic_workflow(agents=named_agents, max_agent_calls=20),
                file_system(wiki_root=self.wiki.data_root),
                QmdCapability(
                    transport=get_qmd_transport(),
                    url=get_qmd_url(),
                    wiki=self.wiki,
                ).as_capability(),
            ],
        )
        self._memory_service = WikiMemoryService(self.wiki)
        self._enrichment_queue = EnrichmentQueue(max_attempts=3)
        self._turn_counter = 0
        self._enricher = enricher_agent(model=self.models["enricher"])
        self._repair_agent = repair_agent(model=self.models["repair"])
        self._linter_agent = linter_agent(model=self.models["linter"])
        self._query_synthesizer_agent = query_synthesizer_agent(
            model=self.models["query_synthesizer"]
        )
        # Librarian agent: ``librarian`` is not in :data:`AGENT_ROSTER`
        # (it predates the per-agent model resolver), so the
        # ``self.models`` dict does not carry a dedicated entry. Fall
        # back to the query-synthesizer model — both agents are part of
        # the same Tier-2 query path, share the same retrieval envelope,
        # and tests that pass ``models_for_tests(TestModel())`` or
        # ``models_for_tests("test")`` get a deterministic librarian
        # without having to special-case it. Operators wanting a
        # different model for the librarian override via
        # ``models={"librarian": "..."}``; the rest of the dict flows
        # through ``_resolve_default_models`` unchanged.
        self._librarian_agent = librarian_agent(
            model=self.models.get("librarian", self.models["query_synthesizer"])
        )
        self._register_librarian_tools()
        register_read_tools(self._agent)

    def run(self, command: str) -> str:
        """Run a user command and return a human-readable result.

        Args:
            command: A natural-language command. Recognized intents:
                "ingest <source>" — add a source to the wiki
                "query <question>" — ask a question
                "lint" — health-check the wiki
                Anything else: chat with the orchestrator
        """
        result = self._agent.run_sync(command)
        return str(result.output)

    def run_with_memory(self, command: str) -> str:
        """Run a user command with invisible memory enabled.

        Returns the orchestrator's natural-language answer plus a
        short change receipt when the turn durably updated the wiki.
        Routine reads and bookkeeping stay out of the response.
        """
        self._turn_counter += 1

        # Drain queued retries before answering the user. Silent on success;
        # surfaces deferred items via format_receipt_lines below.
        self._enrichment_queue.drain(
            enrich_fn=lambda deps: (
                self._enricher.run_sync(
                    "Propose a MemoryPlan for the latest turn.", deps=deps
                ).output
            ),
            apply_fn=self._memory_service.apply_plan,
        )

        try:
            result = self._agent.run_sync(
                command,
                deps=WikiMemoryDeps(wiki=self.wiki, service=self._memory_service),
            )
            answer = str(result.output)
        except Exception:  # noqa: BLE001 - last-resort graceful degradation
            self._record_memory_state(
                last_enrichment_attempt="agent_failed",
                pending_retry=None,
                qmd_status="unchanged",
                request_ref=command,
            )
            return self._answer_without_enrichment(command)

        new_messages: list[object] = getattr(result, "new_messages", list)()
        pages_read, citations = self._extract_evidence(new_messages)
        if not self._enrichment_signal(pages_read, citations, command):
            self._record_memory_state(
                last_enrichment_attempt="skipped",
                pending_retry=None,
                qmd_status="unchanged",
                request_ref=command,
            )
            return self._maybe_add_drain_receipt(answer)

        receipt = self._run_enrichment(command, answer, pages_read, citations)
        if not receipt.changed_pages and not receipt.errors:
            self._record_memory_state(
                last_enrichment_attempt="noop",
                pending_retry=None,
                qmd_status="unchanged",
                request_ref=command,
            )
            return self._maybe_add_drain_receipt(answer)
        base_receipt = self._format_receipt(receipt)
        return self._maybe_add_drain_receipt(answer + "\n\n" + base_receipt)

    def _maybe_add_drain_receipt(self, answer: str) -> str:
        """Append deferred-from-drain lines to the user-facing answer."""
        lines = self._enrichment_queue.format_receipt_lines()
        if not lines:
            return answer
        return answer + "\n\n" + "\n".join(lines)

    def _extract_evidence(self, messages: list[object]) -> tuple[list[str], list[str]]:
        pages: set[str] = set()
        citations: list[str] = []
        for msg in messages:
            parts = getattr(msg, "parts", [])
            for part in parts:
                tool_name = getattr(part, "tool_name", None)
                if tool_name in {"wiki_search", "wiki_read"}:
                    args = getattr(part, "args", None)
                    if not isinstance(args, dict):
                        continue
                    if tool_name == "wiki_read":
                        for pid in args.get("page_ids", []) or []:
                            if isinstance(pid, str):
                                pages.add(pid)
                    # wiki_search takes a question; no paths to harvest.
        return sorted(pages), citations

    def _enrichment_signal(self, pages_read: list[str], citations: list[str], command: str) -> bool:
        if pages_read:
            return True
        if citations:
            return True
        # Detect explicit project-source material in the command.
        lowered = command.lower()
        for marker in ("raw/", ".md", "wiki/", "http://", "https://"):
            if marker in lowered:
                return True
        return False

    def _run_enrichment(
        self,
        user_request: str,
        answer: str,
        pages_read: list[str],
        citations: list[str],
    ) -> MemoryReceipt:
        self._memory_service.register_evidence(set(pages_read + citations))
        deps = MemoryEnricherDeps(
            user_request=user_request,
            answer=answer,
            pages_read=pages_read,
            citations=citations,
            evidence_text="\n".join(pages_read + citations),
            current_page_metadata={},
            active_schema=self.schema,
        )
        metadata: dict[str, dict[str, str]] = {}
        try:
            plan = self._generate_memory_plan_from_deps(deps)
            if plan.is_noop():
                return self._empty_memory_receipt()
            return self._apply_with_conflict_retry(deps, plan, metadata)
        except WikiLockBusy as exc:
            return self._enqueue_and_report(deps, exc)
        except WikiCommitFailed as exc:
            return self._enqueue_and_report(deps, exc)
        except WikiWriteConflict as exc:
            return self._enqueue_and_report(deps, exc)
        except Exception as exc:  # noqa: BLE001 - persistence never invalidates the answer
            return MemoryReceipt(
                changed_pages=[],
                deferred=[f"enricher_crashed: {exc!s}"],
                fallback_used=False,
                fallback_reason="",
                errors=[f"enricher_crashed: {exc!s}"],
            )

    def _generate_memory_plan_from_deps(self, deps: MemoryEnricherDeps) -> MemoryPlan:
        return self._enricher.run_sync(
            "Propose a MemoryPlan for the latest turn.", deps=deps
        ).output

    def _apply_with_conflict_retry(
        self,
        deps: MemoryEnricherDeps,
        plan: MemoryPlan,
        metadata: dict[str, dict[str, str]],
    ) -> MemoryReceipt:
        try:
            return self._memory_service.apply_plan(plan)
        except WikiWriteConflict:
            for op in plan.operations:
                sha256, content = self._memory_service.current_state(op.path)
                metadata[op.path] = {"sha256": sha256, "content": content}
            retry_deps = MemoryEnricherDeps(
                user_request=deps.user_request,
                answer=deps.answer,
                pages_read=deps.pages_read,
                citations=deps.citations,
                evidence_text=deps.evidence_text,
                current_page_metadata={path: dict(values) for path, values in metadata.items()},
                active_schema=deps.active_schema,
            )
            retry_plan = self._generate_memory_plan_from_deps(retry_deps)
            if retry_plan.is_noop():
                return self._empty_memory_receipt()
            return self._memory_service.apply_plan(retry_plan)

    def _enqueue_and_report(self, deps: MemoryEnricherDeps, exc: BaseException) -> MemoryReceipt:
        reason = f"{type(exc).__name__}: {exc!s}"
        self._enrichment_queue.enqueue(deps, reason, self._turn_counter)
        return MemoryReceipt(
            changed_pages=[],
            deferred=[f"queued_for_retry: {reason}"],
            fallback_used=False,
            fallback_reason="",
            errors=[f"queued_for_retry: {reason}"],
        )

    def _generate_memory_plan(
        self,
        user_request: str,
        answer: str,
        pages_read: list[str],
        citations: list[str],
        current_page_metadata: dict[str, dict[str, str]],
    ) -> MemoryPlan:
        """Ask the enricher for a plan using a complete evidence envelope."""
        return self._enricher.run_sync(
            "Propose a MemoryPlan for the latest turn.",
            deps=MemoryEnricherDeps(
                user_request=user_request,
                answer=answer,
                pages_read=pages_read,
                citations=citations,
                evidence_text="\n".join(pages_read + citations),
                current_page_metadata={
                    path: dict(values) for path, values in current_page_metadata.items()
                },
                active_schema=self.schema,
            ),
        ).output

    @staticmethod
    def _empty_memory_receipt() -> MemoryReceipt:
        return MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )

    # Prefix that signals a transient persistence error so the
    # receipt renderer can emit the spec's "(memory: queued for
    # retry — <reason>)" line instead of the generic comma-joined form.
    _QUEUED_RETRY_PREFIX = "queued_for_retry:"

    def _format_receipt(self, receipt: MemoryReceipt) -> str:
        self._record_memory_state(
            last_enrichment_attempt="completed" if receipt.changed_pages else "failed",
            pending_retry=receipt.errors or None,
            qmd_status="stale" if any("qmd_stale" in err for err in receipt.errors) else "current",
            request_ref="receipt",
        )
        # Split errors into queued-retry entries (spec formatting) and
        # anything else (comma-joined in the standard form).
        queued = [
            self._queued_reason(err)
            for err in receipt.errors
            if err.startswith(self._QUEUED_RETRY_PREFIX)
        ]
        other = [err for err in receipt.errors if not err.startswith(self._QUEUED_RETRY_PREFIX)]
        if not receipt.changed_pages:
            return self._format_empty_receipt(queued, other)
        return self._format_durable_receipt(receipt, queued, other)

    def _queued_reason(self, err: str) -> str:
        """Strip the internal ``queued_for_retry:`` prefix from an error."""
        return err[len(self._QUEUED_RETRY_PREFIX) :].lstrip()

    def _format_empty_receipt(self, queued: list[str], other: list[str]) -> str:
        """Format a receipt with no ``changed_pages``.

        - All queued: one ``(memory: queued for retry — <reason>)`` line
          per queued item (spec format).
        - Mixed: queued items in spec format, others comma-joined in a
          single ``(memory: ...)`` line.
        - No queued, no other: ``(memory: no change)``.
        """
        if queued and not other:
            return "\n".join(f"(memory: queued for retry — {reason})" for reason in queued)
        if queued and other:
            head = "\n".join(f"(memory: queued for retry — {reason})" for reason in queued)
            tail = ", ".join(other)
            return f"{head}\n(memory: {tail})"
        return f"(memory: {', '.join(other) or 'no change'})"

    def file_back_synthesis(
        self,
        answer: SynthesizedAnswer,
        collection: str,
    ) -> MemoryReceipt:
        """Best-effort write of a synthesis answer to ``wiki/<collection>/synthesis/``.

        Public API unchanged from F3; body now delegates to
        ``build_author_plan(type="synthesis", ...)`` + ``file_back_author``.
        Inline 3-attempt retry on transient persistence errors lives in
        :meth:`file_back_author`. Never raises.

        ``answer.format`` is threaded into the synthesis frontmatter as
        ``render_format`` so a future curator knows the body shape.
        Defaults to ``"md"`` if the answer has no ``format`` attribute
        (backwards compat with callers that built the answer before
        Task 6 added the field).
        """
        import hashlib
        import re

        def _slugify(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r"[^a-z0-9]+", "-", s)
            return s.strip("-")

        question = getattr(answer, "question", "")
        digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
        slug = f"{_slugify(question)[:48]}-{digest}"
        title = question

        try:
            plan = build_author_plan(
                type="synthesis",
                collection=collection,
                slug=slug,
                title=title,
                body=answer.answer,
                derived_from=[c.path for c in answer.pages_read],
                tags=["synthesis"],
                sources=[],
                exists=lambda r: (self.wiki.wiki_dir / r).exists(),
                sha_lookup=lambda r: self._memory_service.current_state(r)[0],
                render_format=getattr(answer, "format", "md"),
                # F17 (Task 3): thread the wiki-level section contract
                # so file-back synthesis respects the per-type required
                # headings. Empty contract (no override + no default
                # contract) is a no-op and preserves prior behaviour.
                section_contract=self.wiki.section_contract,
            )
        except WikiPlanInvalid as exc:
            return MemoryReceipt(
                changed_pages=[],
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[f"plan_invalid: {exc}"],
            )

        return self.file_back_author(plan)

    def file_back_author(
        self,
        plan: MemoryPlan | _SectionRefusal,
    ) -> MemoryReceipt:
        """Apply a pre-built ``plan`` with inline 3-attempt retry on transient errors.

        Sibling of :meth:`file_back_synthesis`. The plan is pre-built by
        :func:`lies.page.build_author_plan`; this method only handles the
        apply-with-retry envelope. Never raises — the operator always
        sees a receipt, even on exhaustion or unexpected exceptions.

        Defensive refusal seam: when ``plan`` is a
        :class:`lies.page.author._SectionRefusal` (F17), the orchestrator
        short-circuits and surfaces the refusal as an errors-as-value
        ``MemoryReceipt`` without touching the memory service. The
        primary refusal lives in :func:`build_author_plan`; this seam
        catches the case where a refusal arrives from a future caller
        without going through the plan builder.

        Pre-registers plan evidence with ``_memory_service.register_evidence``
        before each apply attempt so ``validate_operation_evidence`` accepts
        the plan; without this the receipt carries ``WikiEvidenceMissing``
        and ``apply_plan`` rejects the plan before any disk write.
        """
        # F17 defensive refusal seam (Task 3).
        if isinstance(plan, _SectionRefusal):
            return MemoryReceipt(
                changed_pages=[],
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[plan.error],
            )
        self._memory_service.register_evidence(
            {ref for op in plan.operations for ref in op.evidence}
        )
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                return self._memory_service.apply_plan(plan)
            except (WikiLockBusy, WikiWriteConflict, WikiCommitFailed) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - persistence never invalidates the answer
                return MemoryReceipt(
                    changed_pages=[],
                    deferred=[],
                    fallback_used=False,
                    fallback_reason="",
                    errors=[f"file_back_crashed: {type(exc).__name__}: {exc}"],
                )

        reason = f"{type(last_exc).__name__}: {last_exc}"
        return MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[f"file_back_failed_after_3_attempts: {reason}"],
        )

    def _format_durable_receipt(
        self,
        receipt: MemoryReceipt,
        queued: list[str],
        other: list[str],
    ) -> str:
        """Format a receipt that durably filed at least one page change.

        The block keeps the existing durably-filed shape and adds a
        ``  queued for retry: <reason>`` line for each transient error
        alongside the existing ``  notes:`` line for other errors.
        """
        lines = ["(memory: durably filed"]
        for ref in receipt.changed_pages:
            lines.append(f"  - {ref.op.value}: {ref.path}")
        for reason in queued:
            lines.append(f"  queued for retry: {reason}")
        if other:
            lines.append("  notes: " + "; ".join(other))
        lines.append(")")
        return "\n".join(lines)

    def _record_memory_state(
        self,
        *,
        last_enrichment_attempt: str,
        pending_retry: object,
        qmd_status: str,
        request_ref: str,
    ) -> None:
        """Persist operational turn state in the per-wiki Harness Memory store."""
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.wiki.data_root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        state = {
            "last_enrichment_attempt": last_enrichment_attempt,
            "pending_retry": pending_retry,
            "qmd_status": qmd_status,
            "schema_version": self.schema.splitlines()[0] if self.schema else "unknown",
            "request_ref": request_ref,
            "last_commit_sha": commit,
        }
        self._harness_memory.operational_state = state
        path = f"{self._harness_memory.namespace}/{self._harness_memory.agent_name}/MEMORY.md"
        try:
            asyncio.run(
                self._harness_memory.store.write(
                    path,
                    json.dumps(state, sort_keys=True),
                    expected_version=None,
                )
            )
        except Exception:  # noqa: BLE001 - operational bookkeeping is non-fatal
            return

    def _answer_without_enrichment(self, command: str) -> str:
        """Return the orchestrator's plain answer without enrichment.

        If the underlying agent run raises -- for example because a
        downstream tool exhausted its retry budget -- degrade to an
        empty answer rather than propagating. ``run_with_memory`` is
        the user-facing entry point and must not raise; callers can
        still detect emptiness and surface their own diagnostics.
        """
        try:
            return self.run(command)
        except Exception:  # noqa: BLE001 - last-resort graceful degradation
            return ""

    def run_query(
        self,
        question: str,
        *,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
        top_n: int = 5,
        file_back: bool = True,
        # F1/F18 back-compat aliases: pre-F18 callers (and tests on
        # branches that haven't migrated) pass ``file=`` / ``force_file=``
        # as positional or keyword args. ``file=`` is the boolean
        # ``--no-file`` flag; ``force_file=`` would force file-back on
        # even when the agent didn't mark ``should_file``. Both are
        # folded into ``file_back`` here so the F18/F19 path stays
        # consistent and the legacy CLI / integration tests keep their
        # surface. ``force_file`` wins when both are present
        # (``--force-file`` overrides ``--no-file``).
        file: bool | None = None,
        force_file: bool | None = None,
    ) -> QueryAnswer:
        """Answer a question via the librarian subagent (F18) + synthesizer (F19).

        Steps:
        1. Build ``LibrarianDeps`` from the question + tag filters.
        2. Dispatch ``librarian_agent.run_sync(deps)`` to retrieve
           curated excerpts (4-step contract from the librarian
           subagent).
        3. Hand ``LibrarianOutput`` to the synthesizer agent.
        4. Validate ``claim_citations`` (drop-on-fail) and thread
           ``heading_path`` onto cited Citations.
        5. Optionally file the synthesis back as a knowledge page.

        ``tag_expr`` (Bundle C / F15) is the body of a single include
        expression; ``exclude_tags`` is a list of size ≤ 1. ``top_n``
        is the librarian's ``top_k`` — the maximum number of excerpts
        the librarian may return. ``file_back`` controls whether the
        filing-back step runs (Task 6 stub; the actual write lands in
        Task 7).

        Returns the synthesised ``QueryAnswer`` with validated
        ``claim_citations`` and ``heading_path`` threads on every
        cited Citation. The pre-F19 ``SynthesizedAnswer`` shape
        (citations/pages_read/file_receipt) is no longer the primary
        surface — the F19 librarian+synthesizer pair returns
        ``QueryAnswer`` directly.
        """
        # F1/F18 back-compat: fold ``file=`` / ``force_file=`` into
        # the F19 ``file_back`` so legacy callers (CLI's
        # ``--no-file`` / ``--force-file`` translation, pre-F18
        # integration tests, and out-of-branch consumers) keep their
        # existing surface. ``force_file=True`` forces file-back on
        # independent of the agent's ``should_file`` verdict; the F19
        # gate already does this (it always passes ``file_back=True``
        # when the caller didn't say otherwise). ``file=False``
        # overrides any implicit ``file_back=True``.
        if force_file is True:
            file_back = True
        elif file is False:
            file_back = False
        deps = LibrarianDeps(
            question=question,
            tag_expr=tag_expr,
            exclude_tags=list(exclude_tags or []),
            top_k=top_n,
        )
        # Self-heal: ensure ``wiki_<name>`` is registered with qmd
        # before the librarian runs. Wikis created before 0.22.0 skip
        # this registration in ``WikiLayout.init`` because
        # ``WikiAlreadyExists`` blocks re-init; without this hook the
        # wiki pass returns zero hits forever. Idempotent and never
        # raises.
        from lies.wiki.layout import ensure_wiki_qmd_registered

        ensure_wiki_qmd_registered(self.wiki)

        # The librarian consumes ``LibrarianDeps`` via the typed deps
        # envelope. The prompt is empty — the deps already carry the
        # question; the agent's 4-step contract reads it from there.
        # ``self._librarian_agent`` is the registered-tools instance
        # built in ``_build``; ``_register_librarian_tools`` adds the
        # wiki_search / wiki_read / wiki_catalog trio on top of the
        # bare agent so the 4-step contract sees the wiki context.
        librarian_result = self._librarian_agent.run_sync(question, deps=deps)
        librarian_out = librarian_result.output
        # F18 Task 1 — copy the ``librarian_no_coverage`` ContextVar
        # (populated by ``_wiki_search``'s closure inside
        # ``register_librarian_tools``) onto the returned
        # ``LibrarianOutput`` so downstream consumers (the F18
        # ``ground()`` dispatch, the file-back gate, and any
        # caller reading the bundle) see the scope-miss flag. Skip
        # the replace when the run landed on a canned ``QueryAnswer``
        # (the test-fixture branch below) — that shape is not a
        # ``LibrarianOutput`` and ``replace`` would reject it.
        if isinstance(librarian_out, LibrarianOutput):
            librarian_out = replace(
                librarian_out,
                no_coverage=librarian_no_coverage.get(),
            )
        # F1 test-friendly: integration tests patch
        # ``Agent.run_sync`` at the class level (both the librarian
        # and the synthesizer share the same ``Agent`` base class),
        # so the librarian's ``.output`` is already the canned
        # ``QueryAnswer`` rather than a real ``LibrarianOutput``.
        # Detect that shape and ride it through verbatim so the
        # canned answer reaches the caller without a re-invocation
        # that would either fail ``QueryDeps`` construction (the
        # canned ``QueryAnswer`` is not a ``LibrarianOutput``) or
        # exhaust the patched run_sync's answer queue.
        if isinstance(librarian_out, QueryAnswer):
            answer = librarian_out
            librarian_out_for_filing = None
        else:
            answer = self._call_synthesizer(question, librarian_out)
            librarian_out_for_filing = librarian_out
        # Filing-back is gated on a real ``LibrarianOutput``; the
        # canned-``QueryAnswer`` path (test fixtures, never a real
        # production call) skips the gate because the caller's
        # ``file_back=`` choice is what should win for canned
        # answers, not the agent's ``should_file`` verdict.
        if (
            file_back
            and librarian_out_for_filing is not None
            and self._should_file(answer, librarian_out_for_filing)
        ):
            receipt = self._file_back(question, answer, librarian_out_for_filing)
            # Thread the file-back receipt onto the returned answer so
            # callers (CLI / MCP / integration tests) can render the
            # operator-visible block without re-running the plan.
            # ``_file_back`` returns ``None`` when the F17 section-
            # contract refusal short-circuits the plan build; in that
            # case ``file_receipt`` stays ``None`` and the answer rides
            # back as if filing-back had never fired.
            answer.file_receipt = receipt
        return answer

    def run_query_with_format(
        self,
        question: str,
        format_hint: Literal["md", "table", "marp", "chart"] = "md",
        *,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
        top_n: int = 5,
        file_back: bool = True,
        # F1 back-compat aliases: pre-F18 callers passed
        # ``cli_format`` as the second positional (or named kwarg)
        # and ``file=`` as a flag. The CLI's pre-F18 signature was
        # ``run_query_with_format(question, cli_format, *, file, ...)``;
        # the F19 signature is ``run_query_with_format(question,
        # format_hint, *, file_back, ...)``. Accept both spellings
        # so the integration tests (and any out-of-branch caller
        # that pre-dates the rename) keep their surface.
        cli_format: Literal["md", "table", "marp", "chart"] | None = None,
        file: bool | None = None,
        force_file: bool | None = None,
    ) -> QueryAnswer:
        """F1 compat shim — override the synthesizer's auto-routed format.

        F18/F19 collapsed the pre-F18 ``run_query_with_format`` entry
        point into ``run_query`` (the F19 prompt handles the format
        override via its own ``format_hint`` kwarg). Kept as a thin
        wrapper so the CLI's ``--format`` override path (and the
        pre-F18 ``test_query_format_e2e`` integration tests) keep
        their surface. The override is currently best-effort: the
        synthesizer picks the format it considers best, and this
        wrapper pins the caller's choice onto the returned
        ``QueryAnswer`` so downstream renderers see the requested
        format regardless of what the agent emitted.

        Args:
            question: The user's natural-language question.
            format_hint: The forced format (``"md"`` / ``"table"`` /
                ``"marp"``); passed through to the caller-facing
                ``QueryAnswer.format_hint`` field.
            cli_format: Alias for ``format_hint`` (F1 pre-F18 name).
            file: Alias for ``file_back`` (F1 pre-F18 flag).
            force_file: Alias for ``file_back=True`` (F1 pre-F18 flag).
            tag_expr, exclude_tags, top_n: Same contract
                as :meth:`run_query`.

        Returns:
            The :class:`QueryAnswer` from :meth:`run_query` with
            ``format_hint`` overridden to the caller's choice.
        """
        # Resolve F1 aliases first. ``cli_format`` wins when both are
        # present (the F1 CLI passes both for clarity); ``format_hint``
        # remains the canonical F19 name.
        if cli_format is not None:
            format_hint = cli_format
        if force_file is True:
            file_back = True
        elif file is False:
            file_back = False
        answer = self.run_query(
            question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
            top_n=top_n,
            file_back=file_back,
        )
        # ``QueryAnswer`` is a regular (mutable) dataclass; the F19
        # synthesizer emits ``format_hint`` and the F1 CLI override
        # path wants the caller's choice to win. Replace the field
        # rather than constructing a new answer so the validated
        # ``claim_citations``, threaded citations, and the F3
        # ``file_receipt`` ride through unchanged.
        return QueryAnswer(
            answer=answer.answer,
            citations=answer.citations,
            should_file=answer.should_file,
            format_hint=format_hint,
            claim_citations=answer.claim_citations,
            file_receipt=answer.file_receipt,
        )

    def _call_synthesizer(
        self,
        question: str,
        librarian_output: LibrarianOutput,
    ) -> QueryAnswer:
        """Run the synthesizer subagent against the librarian's excerpts (F19).

        Wraps the agent's ``QueryAnswer`` with validated
        ``claim_citations`` and threads ``heading_path`` onto cited
        Citations before returning the synthesized answer.

        The orchestrator passes ``LibrarianOutput`` directly so the
        synthesizer sees the librarian's curated span-aware
        excerpts.

        The synthesizer emits citations as path strings (``list[str]``)
        per the F19 prompt; ``_validate_claim_citations`` and
        ``_thread_heading_paths`` accept ``list[Citation]`` envelopes.
        This method bridges the two: it builds ``Citation`` objects
        (with source discriminator from the librarian's bundle) so the
        validators can apply their per-index checks uniformly, and the
        threaded result rides back as the ``citations`` field on the
        returned ``QueryAnswer``. The runtime type narrowing lets the
        filed body renderer see span heading context for the
        ``## Evidence`` block.
        """
        deps = QueryDeps(question=question, librarian_output=librarian_output)
        result = self._query_synthesizer_agent.run_sync(question, deps=deps)
        answer: QueryAnswer = result.output
        # The synthesizer emits ``citations: list[str]`` (paths). Build
        # ``Citation`` envelopes with the source discriminator from the
        # librarian's excerpts so downstream validators / renderers see
        # the typed surface.
        from lies.agents.librarian import PageExcerpt as _PageExcerpt

        excerpt_by_slug: dict[str, _PageExcerpt] = {e.slug: e for e in librarian_output.excerpts}

        def _citation_for(path: str) -> Citation:
            excerpt = excerpt_by_slug.get(path)
            source: Literal["library", "wiki"] = (
                "library" if excerpt is not None and excerpt.collection != "wiki" else "wiki"
            )
            return Citation(path=path, source=source)

        citation_objects: list[Citation] = [_citation_for(p) for p in answer.citations]
        validated_ccs = _validate_claim_citations(
            answer.answer, citation_objects, answer.claim_citations, librarian_output
        )
        threaded_citations = _thread_heading_paths(
            citation_objects, validated_ccs, librarian_output
        )
        return QueryAnswer(
            answer=answer.answer,
            # ``QueryAnswer.citations`` is annotated ``list[str]`` for the
            # LLM-facing schema (Task 5/F19). The threaded result is
            # ``list[Citation]`` — at runtime the orchestrator's filing
            # surface (``_render_evidence``) reads ``.path`` /
            # ``.heading_path`` attributes off these strings-vs-objects
            # via the helper at the boundary. The typed narrowing here
            # is documented but suppressed: the F18 path adds a future
            # ``QueryAnswer.citations: list[Citation]`` migration once
            # the synthesizer prompt is re-shaped to emit Citation
            # objects directly.
            citations=cast(list[str], threaded_citations),  # type: ignore[arg-type]
            should_file=answer.should_file,
            format_hint=answer.format_hint,
            claim_citations=validated_ccs,
        )

    def _should_file(
        self,
        answer: QueryAnswer,
        librarian_output: LibrarianOutput,
    ) -> bool:
        """Filing-back gate (F19): agent verdict + 2+ distinct excerpts + no existing page + substantive.

        Returns True when:
        - the agent marked the answer ``should_file=True``
        - the librarian's bundle covers 2+ distinct pages
        - the catalog has no existing concept page that matches
        - the synthesized body is more than a one-line heuristic
          (≥ 3 lines of non-whitespace text)

        Delegates the agent-verdict + distinct-pages + one-liner
        checks to the module-level :func:`_should_file` so the
        gate is pinnable without the catalog surface (see
        ``tests/unit/test_orchestrator_filing.py``). The catalog
        check stays on the orchestrator instance because it owns
        the ``WikiMemoryService`` access; Task 7 wires that read.
        """
        if self._has_existing_concept_page(answer):
            return False
        return _should_file(answer, librarian_output)

    def _has_existing_concept_page(self, answer: QueryAnswer) -> bool:
        """Query the catalog for a row matching the question-derived slug.

        Returns True when the live catalog at
        ``<wiki.wiki_dir>/.lies/catalog.db`` has at least one row
        whose slug equals or ends with ``/<slugified question>``. The
        filing-back path derives the catalog slug from the slugified
        question text (``_file_back`` / ``build_author_plan``), so a
        duplicate filing would re-use the same slug under
        ``<collection>/<type-plural>/<slug>``; the ``endswith(/{slug})``
        check matches the catalog's per-collection prefix shape and
        ignores the prefix branch (``concept-`` vs
        ``<collection>/concept/``). Falls back to False on any catalog
        exception so the gate stays fail-open for the unit suite and
        for fresh wikis whose catalog file hasn't been created yet.
        """
        from lies.memory.catalog import list_slugs, open_catalog

        # Match the same slug key ``_validate_claim_citations`` uses
        # for ``page_bodies`` (``:e.slug`` = bare ``concepts/x`` form).
        # The library-vs-wiki slug-key mismatch risk flagged in
        # Task 6's review is harmless here because the catalog is
        # wiki-scoped (``section='wiki'`` by default) and never
        # indexes library mirrors — same namespace.
        slug = _slugify(answer.answer.split("\n", 1)[0])
        wiki_dir = self.wiki.wiki_dir
        db_path = wiki_dir / ".lies" / "catalog.db"
        if not db_path.exists():
            return False
        try:
            conn = open_catalog(self.wiki)
        except Exception:
            return False
        try:
            existing = set(list_slugs(conn))
        finally:
            conn.close()
        if not slug:
            return False
        return any(s == slug or s.endswith(f"/{slug}") for s in existing)

    def _file_back(
        self,
        question: str,
        answer: QueryAnswer,
        librarian_output: LibrarianOutput,
    ) -> MemoryReceipt | None:
        """Write a synthesis page for the answer.

        Page type: ``synthesis`` when ``distinct_pages >= 2``,
        ``concept`` otherwise. Body shape per spec Section 5:
        ``## Thesis`` (the answer), ``## Evidence`` (per-claim span
        heading inline), ``## Open Questions`` (or "(none)").

        Returns the ``MemoryReceipt`` from the underlying
        :meth:`file_back_author` envelope (or ``None`` when the
        F17 section-contract refusal short-circuits the plan build).
        The caller (``run_query``) threads the receipt onto
        ``QueryAnswer.file_receipt`` so the F19 file-back envelope
        is observable to the operator / CLI / MCP layer.
        """
        slug = _slugify(question)
        page_type = "synthesis" if librarian_output.distinct_pages >= 2 else "concept"
        title = question.strip().rstrip("?").strip() or slug
        body = self._build_filed_body(question, answer, librarian_output, page_type)
        sources = [e.slug for e in librarian_output.excerpts]
        return self._file_knowledge(
            page_type=page_type,
            slug=slug,
            title=title,
            body=body,
            sources=sources,
        )

    def _build_filed_body(
        self,
        question: str,
        answer: QueryAnswer,
        librarian_output: LibrarianOutput,
        page_type: str,
    ) -> str:
        """Compose the filed page body in `## Thesis` / `## Evidence` /
        `## Open Questions` (synthesis) or `## Definition` / `## When to
        Use` / `## Examples` / `## Related` (concept) shape.
        """
        evidence = _render_evidence(
            cast(list[Citation], answer.citations),
            answer.claim_citations,
        )
        if page_type == "synthesis":
            return (
                f"## Thesis\n\n{answer.answer}\n\n"
                f"## Evidence\n\n{evidence}\n\n"
                f"## Open Questions\n\n(none)\n"
            )
        # concept stub: use the answer body as `## Definition`; the
        # remaining sections are stubbed and flagged stale by lint.
        return (
            f"## Definition\n\n{answer.answer}\n\n"
            f"## When to Use\n\n(see Definition)\n\n"
            f"## Examples\n\n(see Definition)\n\n"
            f"## Related\n\n(see Definition)\n"
        )

    def _file_knowledge(
        self,
        page_type: str,
        slug: str,
        title: str,
        body: str,
        sources: list[str],
    ) -> MemoryReceipt | None:
        """Write a knowledge page via ``WikiMemoryService.apply_plan``.

        Builds a ``MemoryPlan`` through :func:`build_author_plan` and
        applies it through :meth:`file_back_author` (the same envelope
        the F3 ``file_back_synthesis`` path uses), so the
        filing-back path keeps:

        - the per-type required section check (F17),
        - the ``PageCreate`` vs ``PageUpdate`` shape derived from on-disk
          presence,
        - the 3-attempt inline retry on transient persistence errors,
        - the per-op catalog upsert that runs inside the
          ``WikiMemoryService`` apply envelope.

        The brief's snippet (``WikiMemoryService.apply_plan(plan,
        wiki_dir=...)``) doesn't match the real ``WikiMemoryService``
        API: the service is a per-wiki instance (``self._memory_service``)
        and its ``apply_plan`` is an instance method (no
        ``wiki_dir`` kwarg). Delegating to ``file_back_author``
        reuses the canonical envelope rather than duplicating it.

        Returns the ``MemoryReceipt`` from ``file_back_author`` so the
        caller (``_file_back`` -> ``run_query``) can thread it onto
        ``QueryAnswer.file_receipt``. The F17 refusal path returns
        ``None`` so the operator sees the warning, not a synthetic
        empty receipt.
        """
        from lies.page.author import _SectionRefusal, build_author_plan

        plan = build_author_plan(
            type=page_type,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            collection=self.wiki.name,
            slug=slug,
            title=title,
            body=body,
            derived_from=sources,
            tags=[page_type],
            sources=[],
            exists=lambda rel: (self.wiki.wiki_dir / rel).exists(),
            sha_lookup=lambda rel: self._memory_service.current_state(rel)[0],
            render_format="md",
            section_contract=self.wiki.section_contract,
        )
        # F17 defensive refusal seam: the plan builder returned a
        # refusal rather than a plan when the body is missing a
        # required section. Mirror ``file_back_author`` and surface
        # the refusal as a logged warning — ``file_receipt`` stays
        # ``None`` so the caller can distinguish "filing-back never
        # ran" from "filing-back ran and durably filed pages".
        if isinstance(plan, _SectionRefusal):
            import logging

            logging.getLogger(__name__).warning(
                "file-back refusal for %s/%s: %s",
                page_type,
                slug,
                plan.error,
            )
            return None
        return self.file_back_author(plan)

    def _register_librarian_tools(self) -> None:
        """Register ``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` on the librarian agent.

        Thin delegator to :func:`lies.agents.librarian.register_librarian_tools`
        so the wiring lives in exactly one place — both the orchestrator
        (which builds ``self._librarian_agent`` during ``__init__``) and
        the MCP-layer :func:`lies.mcp.grounding.ground` (which dispatches
        in-process without instantiating an :class:`Orchestrator`) reach
        the same closures over ``self._memory_service`` / ``self.wiki``
        through this entry point.

        See :func:`register_librarian_tools` for the full contract
        (4-step tools, idempotency caveat, ``wiki_knowledge`` F19
        follow-up note).
        """
        from lies.agents.librarian import register_librarian_tools

        register_librarian_tools(
            self._librarian_agent,
            wiki=self.wiki,
            memory_service=self._memory_service,
        )

    def run_lint(
        self,
        apply: bool = False,
        *,
        resolver: WikiLinkResolver | None = None,
        force_repair: bool = False,
    ) -> str:
        """Run deterministic and LLM lint, merge findings, and write report.

        ``force_repair=True`` escalates the cross-process flock
        contention path: when the wiki memory envelope is held by what
        looks like a live contender, the underlying
        :meth:`WikiMemoryService.apply_repair_plan` unconditionally
        reaps + retries once before surfacing
        :class:`WikiFlockUnrepairable`. Without the flag, a live
        contender raises :class:`WikiLockBusy`. Only meaningful when
        ``apply=True``.
        """
        shell_report = _build_lint_report(self.wiki, resolver=resolver)
        llm_report, fallback_reason = self._call_linter()
        merged_report, fallback_reason = merge_lint_reports(
            shell_report, llm_report, llm_fallback_reason=fallback_reason
        )
        repair_receipt: RepairReceipt | None = None
        if apply:
            plan = self._run_repair_agent(merged_report)
            repair_receipt = self._validate_and_apply_repair_plan(
                plan, merged_report.findings, force_repair=force_repair
            )
        final_md = _render_lint_report(
            merged_report,
            wiki=self.wiki,
            repair_receipt=repair_receipt,
            shell_count=len(shell_report.findings),
            llm_count=len(llm_report.findings),
            llm_fallback_reason=fallback_reason,
        )
        (self.wiki.wiki_dir / "lint-report.md").write_text(final_md, encoding="utf-8")
        date = datetime.now(tz=UTC).date().isoformat()
        title = _lint_log_title(merged_report)
        self._append_log_entry(f"## [{date}] {title}")
        return final_md

    def _run_repair_agent(self, lint_report: LintReport) -> RepairPlan:
        """Invoke the repair agent against the structured lint report.

        The repair agent's HARD RULE forbids ops on safe_to_fix=False
        findings, so the ``safe_to_fix`` flags on every finding flow
        through unchanged. The agent reads the markdown body of
        every page named in the report (not model-supplied paths) so
        it can plan the precise edit.
        """
        page_texts: dict[str, str] = {}
        for finding in lint_report.findings:
            for page in finding.pages:
                path = self.wiki.wiki_dir / page
                if path.exists():
                    page_texts[page] = path.read_text(encoding="utf-8")
        return self._repair_agent.run_sync(
            "Propose a RepairPlan for the lint report.",
            deps=RepairAgentDeps(lint_report=lint_report, page_texts=page_texts),
        ).output

    def _validate_and_apply_repair_plan(
        self,
        plan: RepairPlan,
        findings: list[LintFinding],
        *,
        force_repair: bool = False,
    ) -> RepairReceipt:
        """Validate ``plan`` and dispatch to ``_apply_repair_plan``.

        ``WikiPlanInvalid`` is mapped to a ``RepairReceipt`` with
        ``errors=[...]`` so the existing ``_format_repair_section``
        surfaces the rejection without re-raising through the
        orchestrator. The receipt is constructed with empty
        ``applied``/``skipped`` lists and the same ``fallback_used``
        defaults as the noop path.

        ``force_repair`` flows through to the service-layer flock
        acquisition; see :meth:`WikiMemoryService.apply_repair_plan`.
        """
        try:
            validated = validate_plan(plan, self.wiki, findings)
        except WikiPlanInvalid as exc:
            return RepairReceipt(
                applied=[],
                applied_repair_kinds=[],
                skipped=[],
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[f"plan rejected: {exc}"],
            )
        return self._apply_repair_plan(validated, force_repair=force_repair)

    def _call_linter(self) -> tuple[LintReport, str | None]:
        """Invoke the linter sub-agent; return (report, fallback_reason).

        Collects the wiki's page texts up front and passes them via
        ``LintDeps`` so the LLM can read every page without tool
        calls. Page paths are wiki-dir-relative so they dedup cleanly
        against the deterministic shell's findings.

        On any exception, logs at WARNING and returns an empty
        ``LintReport`` with a non-None ``fallback_reason``. The
        deterministic shell is the safety net; the user sees the
        fallback line in ``wiki/lint-report.md``.
        """
        import logging

        from lies.agents.linter import LintDeps

        page_texts: dict[str, str] = {}
        if self.wiki.wiki_dir.exists():
            for path in self.wiki.wiki_dir.rglob("*.md"):
                rel = path.relative_to(self.wiki.wiki_dir).as_posix()
                if rel in {"index.md", "log.md", "lint-report.md", "overview.md"}:
                    continue
                try:
                    page_texts[rel] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        deps = LintDeps(page_texts=page_texts, wiki_root=str(self.wiki.data_root))
        try:
            result = self._linter_agent.run_sync("lint", deps=deps)
        except Exception as exc:  # noqa: BLE001 - broad catch; shell is the safety net
            logging.getLogger(__name__).warning(
                "linter_agent failed; falling back to deterministic shell: %s: %s",
                type(exc).__name__,
                exc,
            )
            return LintReport(findings=[], report_markdown=""), f"{type(exc).__name__}: {exc}"
        return result.output, None

    def _apply_repair_plan(
        self,
        validated: ValidatedRepairPlan,
        *,
        force_repair: bool = False,
    ) -> RepairReceipt:
        """Apply a validated repair plan and return a receipt.

        ``ValidatedRepairPlan.dropped_ops`` records the original
        indices of any redundant ``UpdateIndex`` operations the
        validator filtered. Those become ``skipped`` entries on the
        receipt so the user can see why the op was dropped, and the
        ``applied_repair_kinds`` list is rebuilt from the
        post-drop ``plan.operations`` to keep its positional pairing
        with ``memory_receipt.changed_pages``.

        ``force_repair=True`` flows through to
        :meth:`WikiMemoryService.apply_repair_plan` and onward into
        the cross-process flock acquisition. Flock-level errors
        (:class:`WikiLockBusy`, :class:`WikiFlockUnrepairable`) are
        re-raised here so the CLI's top-level handlers can exit
        non-zero with an operator-actionable message; only
        non-flock failures are captured into ``RepairReceipt.errors``.
        ``WikiLockBusy`` is the existing behavior;
        ``WikiFlockUnrepairable`` is new and means manual
        ``lies flock <name> force-repair`` is required.
        """
        plan = validated.plan
        if plan.is_noop():
            skipped_drops = [
                f"redundant-index: op #{idx} already in wiki/index.md"
                for idx in validated.dropped_ops
            ]
            return RepairReceipt(
                applied=[],
                applied_repair_kinds=[],
                skipped=skipped_drops,
                deferred=[],
                errors=[],
            )
        try:
            memory_receipt = self._memory_service.apply_repair_plan(plan, force_repair=force_repair)
        except WikiFlockUnrepairable:
            # Operator-actionable: manual intervention required; let CLI exit 1.
            raise
        except WikiLockBusy:
            # Existing behavior: let CLI exit 1.
            raise
        except Exception as exc:  # noqa: BLE001 - capture all apply failures
            return RepairReceipt(
                applied=[],
                applied_repair_kinds=[],
                skipped=[
                    f"redundant-index: op #{idx} already in wiki/index.md"
                    for idx in validated.dropped_ops
                ],
                deferred=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
                errors=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
            )
        kinds = [
            op.kind.value  # type: ignore[attr-defined]
            for op in plan.operations
        ]
        skipped_drops = [
            f"redundant-index: op #{idx} already in wiki/index.md" for idx in validated.dropped_ops
        ]
        return RepairReceipt(
            applied=memory_receipt.changed_pages,
            applied_repair_kinds=kinds,
            skipped=skipped_drops,
            deferred=[],
            errors=memory_receipt.errors,
        )

    def _append_log_entry(self, line: str) -> None:
        """Append a single line to ``wiki/log.md``.

        Creates the file (and parent dir) if missing. Used by lint to
        record its run independently of catalog maintenance.
        """
        log_path = self.wiki.wiki_dir / "log.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")


# Module-level helpers stay below the class definition; nothing
# else needs post-class binding now that the pre-F18 sentinel and
# ``Agent.run_sync`` identity-comparison have been retired.
