"""Top-level orchestrator that dispatches user commands to sub-agents."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.indexer import indexer_agent
from lies.agents.linter import LintFinding, LintReport, linter_agent
from lies.agents.page_writer import (
    PageDiff,
    PageWriterDeps,
    page_writer_agent,
)
from lies.agents.query_synthesizer import QueryAnswer, QueryDeps, query_synthesizer_agent
from lies.agents.repair import RepairAgentDeps, repair_agent
from lies.agents.repair_models import RepairPlan, RepairReceipt
from lies.agents.repair_validation import ValidatedRepairPlan, validate_plan
from lies.agents.source_reader import SourceExtraction, source_reader_agent
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
    IngestQuarantined,
    IngestSourceUnreachable,
    MemoryPlan,
    MemoryReceipt,
    WikiCommitFailed,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.retry import EnrichmentQueue
from lies.memory.service import WikiMemoryService, build_synthesis_plan
from lies.memory.tools import WikiMemoryDeps, register_read_tools
from lies.qmd import QmdCapability
from lies.query import (
    PageRead,
    SynthesizedAnswer,
    build_answer_from_pages,
    retrieve_pages,
    synthesize_answer,
)
from lies.schema import load_schema
from lies.wiki.git import CommitError, atomic_commit
from lies.wiki.wiki import Wiki
from lies.wikilinks import WikiLinkResolver
from lies.wikilinks import extract_wikilinks as _extract_wikilinks


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
    if wiki.wiki_dir.exists():
        for path in wiki.wiki_dir.rglob("*.md"):
            rel = path.relative_to(wiki.wiki_dir).as_posix()
            if rel in {"index.md", "log.md", "lint-report.md", "overview.md"}:
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

    # Synthesis-page mechanical checks: synthesis_missing_evidence and
    # dangling_derived_from. A synthesis page's contract (see
    # ``src/lies/schema/default_schema.md`` and ``build_synthesis_plan``)
    # is: frontmatter ``type: synthesis`` + ``derived_from: list[str]``
    # of wiki-relative slugs, plus a body ``## Evidence`` section. The
    # spec'd repairs are mechanical: ``synthesis_missing_evidence``
    # appends a ``## Evidence`` block listing every ``derived_from``
    # slug (empty section if the list is empty); ``dangling_derived_from``
    # removes the dangling slug from the frontmatter list. Both flip
    # ``safe_to_fix=True`` so the repair agent can auto-close them.
    #
    # Read each synthesis page once: a single ``read_text`` per page
    # feeds the type check, the body ``## Evidence`` check, and the
    # ``derived_from`` slug resolution — the previous 3-pass loop
    # opened and closed each file three times, which adds up on large
    # wikis without any semantic gain.
    for page in pages:
        try:
            text = (wiki.wiki_dir / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _extract_frontmatter_type(text) != "synthesis":
            continue
        if "## Evidence" not in _strip_frontmatter(text):
            findings.append(
                LintFinding(
                    severity=LintSeverity.MEDIUM,
                    category="synthesis_missing_evidence",
                    pages=[page],
                    message=f"synthesis page {page} lacks ## Evidence section",
                    safe_to_fix=True,
                )
            )
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


# Module-level constants for the F2 helpers (_list_existing_pages and
# _materialize_source). These are deterministic pure functions, not
# agent-shaped, so they live as module-level helpers alongside the
# other host-side helpers (lint shell, snapshot, etc.) rather than on
# the WikiMemoryService.
#
# _EXCLUDED_TOP_LEVEL_DIRS: any directory segment in this set is
# skipped by the page walker. Keeps ``.lies/`` (runtime sidecars),
# ``.git/`` (git metadata), and ``node_modules/`` (tooling artifacts)
# out of the agent's existing-pages list.
_EXCLUDED_TOP_LEVEL_DIRS = frozenset({".lies", ".git", "node_modules"})
# _FRONTMATTER_SUMMARY_RE: matches a ``summary: <value>`` line inside a
# YAML frontmatter block. The block is parsed by checking
# ``text.startswith("---")`` and finding the closing ``\n---``; only
# then is the regex applied to the block contents.
_FRONTMATTER_SUMMARY_RE = re.compile(r"^summary:\s*(.+)$", re.MULTILINE)


def _summarize_page(path: Path) -> str:
    """Return the page's frontmatter ``summary:`` value, else a
    deterministic fallback built from the first H1 + first body line.

    Pure function; no I/O beyond reading the file. Test-only / agent-input
    utility — does not need to live on the WikiMemoryService. The body
    line is taken from lines AFTER any YAML frontmatter block so the
    ``title:`` (or other frontmatter fields) don't get reported as the
    first body line.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    body_text = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_match = _FRONTMATTER_SUMMARY_RE.search(text[3:end])
            if fm_match:
                return fm_match.group(1).strip().strip('"').strip("'")
            # Skip the frontmatter block when building the fallback so
            # the ``title:`` line doesn't get reported as the first body.
            body_text = text[end + 4 :].lstrip("\n")
    lines = body_text.splitlines()
    h1 = next((line for line in lines if line.startswith("# ")), "")
    body = next(
        (line.strip() for line in lines if line.strip() and not line.startswith("#")),
        "",
    )
    return f"{h1.removeprefix('# ').strip()} {body}".strip()


def _url_basename(url: str) -> str:
    """Stable filename for a fetched URL. Falls back to ``fetched.md``.

    The basename is the URL's last path segment; URLs whose path has
    no useful tail (e.g. ``https://example.com/``) get the literal
    fallback so the materialize step always produces a real file.
    """
    from urllib.parse import urlparse

    name = Path(urlparse(url).path).name
    return name or "fetched.md"


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
        "source_reader",
        source_reader_agent,
        (
            "Read a raw source and return a structured extraction "
            "(claims, entities, concepts, comparisons, summary)."
        ),
    ),
    (
        "page_writer",
        page_writer_agent,
        (
            "Create or update wiki pages from extracted material; "
            "return `PageDiff` operations; never touches index.md or log.md."
        ),
    ),
    (
        "indexer",
        indexer_agent,
        (
            "Maintain wiki/index.md (the catalog) and wiki/log.md "
            "(the append-only log) from a list of `PageDiff` operations."
        ),
    ),
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


class Orchestrator:
    """The top-level agent that maintains a LIES wiki.

    The orchestrator is the only entrypoint exposed to the CLI. It composes
    five sub-agents (source-reader, page-writer, indexer, linter,
    query-synthesizer) via harness's `SubAgents` capability, plus file system,
    shell, qmd MCP, CodeMode, Memory, Planning, and DynamicWorkflow.

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

        Inline 3-attempt retry on transient persistence errors. Never
        raises — the synthesized answer is always returned to the
        operator regardless of outcome.
        """

        def exists(rel: str) -> bool:
            return (self.wiki.wiki_dir / rel).exists()

        def sha_lookup(rel: str) -> str:
            return self._memory_service.current_state(rel)[0]

        question = getattr(answer, "question", "")

        try:
            plan = build_synthesis_plan(
                question=question,
                answer=answer.answer,
                pages_read=answer.pages_read,
                collection=collection,
                sha_lookup=sha_lookup,
                exists=exists,
            )
        except WikiPlanInvalid as exc:
            return MemoryReceipt(
                changed_pages=[],
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[f"plan_invalid: {exc}"],
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

    def run_ingest(self, source: str, *, no_llm: bool = False) -> str:
        """Ingest a single source through the LLM round-trip (default)
        or via ``sync_collection`` (when ``no_llm=True``).

        F2 default (``no_llm=False``):
          1. snapshot working tree (``_snapshot_working_tree``)
          2. materialize ``source`` to ``raw/<collection>/<basename>``
          3. ``source_reader_agent`` → ``SourceExtraction``
          4. ``_list_existing_pages`` (deterministic)
          5. ``page_writer_agent`` → ``list[PageDiff]``
          6. ``translate_page_diffs_to_plan`` → ``MemoryPlan(tag="ingest")``
          7. ``WikiMemoryService.apply_plan`` (flock + atomic commit +
             sidecar + log + qmd update + rebuild_index + rollback)
          8. discard snapshot

        Agent failures at steps 3 or 5 call
        ``etl.quarantine.quarantine`` and raise ``IngestQuarantined``.
        Infra failures rollback and propagate the typed error.

        ``IngestSourceUnreachable`` (raised at step 2 before any agent
        work) — and any raw ``OSError`` from step 2's disk I/O — takes
        the same ``discard snapshot`` path as :class:`IngestQuarantined`.
        The snapshot was taken, but no wiki writes happened, so the
        stash entry can be dropped rather than restored. Without this
        branch the stash would leak until the next ``git stash clear``.
        """
        from lies.etl.sync_helper import sync_collection
        from lies.memory.service import (
            _hash_text,
            _read_page,
            translate_page_diffs_to_plan,
        )

        collection_name = Path(source).stem
        if no_llm:
            sync_collection(self.wiki, collection_name, force=False)
            return f"ingested {source}"

        def _sha_lookup(rel: str) -> str:
            """Return the SHA-256 of an existing wiki page, or "" if missing.

            The page-writer agent emits UPDATE ops with a fresh
            ``new_content``; the adapter sets ``expected_sha256`` to the
            current on-disk hash so :class:`WikiWriteConflict` catches
            drift. Brand-new pages don't go through UPDATE, but we still
            return "" uniformly for non-existent paths.

            The page-writer emits paths with the ``wiki/`` prefix per
            the schema convention. ``_read_page`` joins onto
            ``wiki.wiki_dir`` (= ``<data_root>/wiki``), so it expects a
            path WITHOUT the leading ``wiki/`` — exactly like
            ``_apply_operations`` passes to ``validate_page_path``.
            Without the strip, ``_sha_lookup`` reads the doubled-prefix
            location (``<data_root>/wiki/wiki/<rest>``) and returns
            ``""`` even when the real on-disk file exists, breaking
            the validate/apply agreement that
            ``expected_sha256`` relies on.
            """
            body = _read_page(self.wiki, rel.removeprefix("wiki/"))
            return "" if body is None else _hash_text(body)

        repo = self.wiki.data_root
        # Snapshot first (captures pre-existing dirty state in the wiki),
        # then materialize. The user's source is expected to live
        # OUTSIDE the wiki — materialize copies it in AFTER the snapshot,
        # so the materialized file is NOT part of the stash and quarantine
        # can still find it on the failure path. The snapshot still
        # stashes any agent-written untracked files (new wiki pages)
        # that ``WikiMemoryService.apply_plan`` will overwrite on
        # success; on failure the service's own snapshot/restore rolls
        # those back too.
        snapshot_ref = Orchestrator._snapshot_working_tree(repo)
        try:
            raw_path = self._materialize_source(source, collection=collection_name)
        except (IngestSourceUnreachable, OSError):
            # Step 2 failed before any agent work — no wiki writes
            # happened, so the snapshot can be discarded rather than
            # restored. ``_materialize_source`` raises
            # :class:`IngestSourceUnreachable` for typed source
            # failures, but raw ``OSError`` (e.g. ``PermissionError``
            # from ``mkdir`` / ``write_text``) can also leak out of
            # the disk I/O branches. Both error classes trigger the
            # same discard-snapshot path here because no wiki writes
            # occurred. Without this branch the stash entry would
            # survive the raise and accumulate until ``git stash
            # clear`` or the next ``run_ingest`` overwrites it.
            Orchestrator._discard_snapshot(repo, snapshot_ref)
            raise
        source_relpath = raw_path.relative_to(repo).as_posix()
        try:
            extraction = self._call_source_reader(
                raw_path,
                collection=collection_name,
                source_relpath=source_relpath,
            )
            existing_pages = self._list_existing_pages(collection_name)
            schema_text = (
                self.wiki.schema_path.read_text(encoding="utf-8")
                if self.wiki.schema_path and self.wiki.schema_path.exists()
                else ""
            )
            diffs = self._call_page_writer(
                extraction=extraction,
                existing_pages=existing_pages,
                schema_text=schema_text,
                collection=collection_name,
                source_relpath=source_relpath,
            )
            plan = translate_page_diffs_to_plan(
                diffs=diffs,
                collection=collection_name,
                source_path=source_relpath,
                sha_lookup=_sha_lookup,
            )
            svc = WikiMemoryService(self.wiki)
            svc.register_evidence({source_relpath, *plan.evidence})
            svc.apply_plan(plan)
        except IngestQuarantined:
            # The agent wrapper already quarantined the source. No wiki
            # writes happened, so the snapshot can be discarded (the
            # underlying ``git stash push --include-untracked`` is
            # purely a safety net; nothing was staged into HEAD).
            Orchestrator._discard_snapshot(repo, snapshot_ref)
            raise
        except BaseException:
            # Any other failure (WikiPlanInvalid, WikiWriteConflict,
            # WikiCommitFailed, …) means the agent or the service
            # envelope blew up after the snapshot was taken. Restore
            # the working tree so a follow-up retry sees a clean slate.
            Orchestrator._restore_working_tree(repo, snapshot_ref)
            raise
        Orchestrator._discard_snapshot(repo, snapshot_ref)
        return f"ingested {source} into {collection_name}"

    def run_query(
        self,
        question: str,
        *,
        collection: str | None = None,
        file: bool = True,
        force_file: bool = False,
    ) -> SynthesizedAnswer:
        """Answer ``question`` using the wiki, synthesized by the LLM.

        Retrieval runs once via :func:`retrieve_pages` (qmd, falling
        back to ``wiki/index.md``), then ``query_synthesizer_agent``
        writes the answer from the full text of those pages. If the
        agent fails for any reason, the deterministic extractive
        synthesizer produces the answer instead and ``synthesis_used``
        is False.

        The two provenance axes are independent: ``fallback_used``
        reports retrieval, ``synthesis_used`` reports synthesis.

        File-back (F3): when ``file`` is True and the agent marked the
        answer ``should_file`` (or ``force_file`` flips it on), a wiki
        page is materialized via :meth:`file_back_synthesis` and the
        resulting :class:`MemoryReceipt` is attached as
        ``ans.file_receipt``. ``collection`` identifies which
        subdirectory the page lands in; without it, the answer is
        returned unfilled and a note is appended to ``synthesis_reason``
        rather than silently dropping the filing intent.
        """
        if not question or not question.strip():
            return synthesize_answer(question, self.wiki)

        pages, fallback_reason = retrieve_pages(question, self.wiki)

        # Nothing to synthesize: don't spend a model call on an empty wiki.
        # ``synthesis_reason="no pages retrieved"`` surfaces the bypass to
        # the CLI/MCP so the user sees the same "LLM synthesis unavailable"
        # note they'd see on an agent failure.
        if not pages:
            extractive = build_answer_from_pages(question, pages, fallback_reason)
            return replace(
                extractive,
                synthesis_used=False,
                synthesis_reason="no pages retrieved",
            )

        output, synthesis_reason = self._call_query_synthesizer(question, pages)
        if output is None:
            extractive = build_answer_from_pages(question, pages, fallback_reason)
            return replace(
                extractive,
                synthesis_used=False,
                synthesis_reason=synthesis_reason,
            )

        retrieved = {page.rel_path for page in pages}
        kept = [c for c in output.citations if c in retrieved]
        dropped = [c for c in output.citations if c not in retrieved]
        if dropped:
            synthesis_reason = (
                f"dropped {len(dropped)} unretrieved citation(s): {', '.join(dropped)}"
            )

        ans = SynthesizedAnswer(
            question=question,
            answer=output.answer,
            citations=kept,
            pages_read=[page.rel_path for page in pages],
            fallback_used=bool(fallback_reason),
            fallback_reason=fallback_reason,
            page_links=[f"[{page.title}]({page.rel_path})" for page in pages],
            synthesis_used=True,
            synthesis_reason=synthesis_reason,
            should_file=output.should_file,
        )

        # File-back decision (F3). ``should_file`` is the agent's own
        # verdict on whether this answer earns a wiki page; ``force_file``
        # overrides it for callers who always want one (e.g. an
        # integration test). ``file`` lets callers opt out entirely
        # (``file=False``) without losing the rest of the synthesis
        # envelope. ``collection`` is required to know where the page
        # lives — when the caller wants a filing but didn't supply one,
        # raise ``WikiPlanInvalid`` so the CLI can exit 2 and the MCP
        # tool can re-raise as ``ToolError``.
        should_file = ans.should_file or force_file
        if should_file and file and collection is None:
            raise WikiPlanInvalid("collection required to file synthesis")
        if should_file and file and collection is not None:
            # Register the read pages so the synthesis plan's
            # ``evidence=pages_read`` survives ``validate_operation_evidence``;
            # otherwise ``apply_plan`` rejects the plan with
            # ``WikiEvidenceMissing`` before any disk write happens. Mirrors
            # the ``register_evidence`` call in ``_run_enrichment`` and
            # ``run_ingest``.
            self._memory_service.register_evidence(set(ans.pages_read))
            ans = replace(ans, file_receipt=self.file_back_synthesis(ans, collection))

        return ans

    def _call_query_synthesizer(
        self, question: str, pages: list[PageRead]
    ) -> tuple[QueryAnswer | None, str]:
        """Invoke the query-synthesizer sub-agent over ``pages``.

        Reads each retrieved page's FULL body — not the 400-char
        excerpt on ``PageRead`` — because the agent's prompt requires
        verbatim quotation and disagreement-surfacing, neither of which
        survives truncation.

        ``rel_path`` is ``data_root``-relative (it carries the ``wiki/``
        prefix), so it joins onto ``self.wiki.data_root``. Joining onto
        ``wiki_dir`` would silently produce ``wiki/wiki/...`` and read
        nothing.

        Returns ``(output, "")`` on success and ``(None, reason)`` on
        any failure, where ``reason`` is ``"<ExcType>: <msg>"``. One
        attempt, no retry: the extractive path is the safety net and a
        query is cheap for the user to re-run. Mirrors
        :meth:`_call_linter`.
        """
        import logging

        page_texts: dict[str, str] = {}
        for page in pages:
            try:
                page_texts[page.rel_path] = (self.wiki.data_root / page.rel_path).read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeDecodeError):
                continue

        deps = QueryDeps(question=question, page_texts=page_texts)
        try:
            result = self._query_synthesizer_agent.run_sync(question, deps=deps)
        except Exception as exc:  # noqa: BLE001 - broad catch; extractive is the safety net
            logging.getLogger(__name__).warning(
                "query_synthesizer_agent failed; falling back to extractive: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None, f"{type(exc).__name__}: {exc}"
        return result.output, ""

    # -- F2 single-source ingest wrappers --------------------------------------
    #
    # These two wrappers back the ingest-source flow
    # (``lies ingest-source <path> --collection <name>``). Both follow the
    # existing fail-soft shape (``except Exception``) but, unlike the
    # lint / query-synthesizer wrappers that degrade silently, they
    # quarantine the offending source and re-raise as
    # :class:`IngestQuarantined` so the caller surfaces the failure
    # rather than papering over it. ``source_relpath`` is the path the
    # caller is operating on (e.g. ``raw/foo/incoming.md``);
    # ``quarantine`` wants just the basename relative to
    # ``raw/<collection>/``, so the wrappers strip the prefix before
    # delegating.

    def _call_source_reader(
        self,
        raw_path: Path,
        *,
        collection: str = "",
        source_relpath: str = "",
    ) -> SourceExtraction:
        """Call ``source_reader_agent`` on the materialized raw file.

        On any agent exception, quarantine the source and raise
        :class:`IngestQuarantined`. ``collection`` and ``source_relpath``
        are required for the quarantine sidecar; both default to
        empty strings so the success-path unit tests don't need to
        thread them through. Real callers (the F2 ingest flow) always
        supply both.
        """
        try:
            reader = source_reader_agent(model=self.models["source_reader"])
            extraction: SourceExtraction = reader.run_sync(  # type: ignore[assignment]
                f"Read {raw_path} and emit a SourceExtraction."
            ).output
            return extraction
        except Exception as exc:
            from lies.etl.quarantine import quarantine

            quarantine(
                self.wiki,
                collection=collection,
                path=source_relpath.removeprefix("raw/" + collection + "/"),
                reason=f"source_reader_agent raised {type(exc).__name__}: {exc}",
            )
            raise IngestQuarantined(
                source=source_relpath,
                collection=collection,
                reason=f"source_reader_agent raised {type(exc).__name__}: {exc}",
            ) from exc

    def _call_page_writer(
        self,
        *,
        extraction: SourceExtraction,
        existing_pages: list[tuple[str, str]],
        schema_text: str,
        collection: str = "",
        source_relpath: str = "",
    ) -> list[PageDiff]:
        """Call ``page_writer_agent`` with deps, returning ``list[PageDiff]``.

        Quarantine + raise on agent failure (mirrors
        :meth:`_call_source_reader`). ``collection`` and
        ``source_relpath`` are required for the quarantine sidecar;
        real callers always supply both, but both default to empty
        strings so the success-path unit tests don't need to thread
        them through.
        """
        try:
            writer = page_writer_agent(model=self.models["page_writer"])
            deps = PageWriterDeps(
                question=f"Ingest {source_relpath} into {collection}",
                schema_text=schema_text,
                existing_pages=existing_pages,
            )
            diffs: list[PageDiff] = writer.run_sync(deps=deps).output  # type: ignore[assignment]
            return diffs
        except Exception as exc:
            from lies.etl.quarantine import quarantine

            quarantine(
                self.wiki,
                collection=collection,
                path=source_relpath.removeprefix("raw/" + collection + "/"),
                reason=f"page_writer_agent raised {type(exc).__name__}: {exc}",
            )
            raise IngestQuarantined(
                source=source_relpath,
                collection=collection,
                reason=f"page_writer_agent raised {type(exc).__name__}: {exc}",
            ) from exc

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
        self._append_log_entry(
            f"## [{datetime.now(tz=UTC).date().isoformat()}] lint | "
            f"{final_md.count(chr(10))} findings"
        )
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
            op.kind.value  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
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
        record its run without disturbing the indexer's contract.
        """
        log_path = self.wiki.wiki_dir / "log.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")

    @staticmethod
    def _commit_ingest(repo: Path, source: str) -> str:
        """Commit the agent's ingest output as one atomic commit.

        Unlike the bare ``atomic_commit(repo, message)`` default (which
        only stages tracked modifications), an ingest may add brand-new
        wiki pages. This helper enumerates every dirty path
        -- untracked, modified, and deleted -- and passes them to
        ``atomic_commit`` so the commit is all-or-nothing.

        Returns:
            The new commit SHA.

        Raises:
            CommitError: If there is nothing to commit, or the commit
                itself fails. (Atomicity is preserved: the index is reset
                to its pre-call state on any failure.)
        """
        dirty_paths = _list_working_tree_changes(repo)
        if not dirty_paths:
            raise CommitError("nothing to commit (ingest produced no changes)")
        sha = atomic_commit(repo, f"ingest: {source}", files=dirty_paths)
        if sha is None:
            # atomic_commit detected the staged diff was empty (e.g. the
            # ingest produced no actual content changes). Treat as a
            # real failure: the caller asked for a commit, not a no-op.
            raise CommitError("nothing to commit (ingest produced no changes)")
        return sha

    # -- host-side snapshot / rollback -----------------------------------------
    #
    # The wiki is expected to be clean between invocations. The snapshot
    # machinery uses ``git stash`` so the working tree is empty while
    # the agent runs (a clean tree makes file writes by sub-agents easy to
    # inspect and roll back). If the wiki is dirty at entry we still record
    # the state so we can restore it on failure.

    @staticmethod
    def _snapshot_working_tree(repo: Path) -> str:
        """Stash any working-tree changes; return a stash ref.

        If the working tree is clean, returns the sentinel ``"<clean>"``
        so the restore path knows there's nothing to put back.
        """
        # Stash includes untracked files so any new files the agent creates
        # can also be rolled back.
        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "pre-ingest"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to snapshot working tree: {result.stderr.strip()}")
        # ``git stash push`` is silent when there is nothing to stash. Detect
        # that case and return the clean-tree sentinel.
        if "No local changes to save" in result.stdout:
            return "<clean>"
        return "stash@{0}"

    @staticmethod
    def _restore_working_tree(repo: Path, snapshot_ref: str) -> None:
        """Restore the working tree from a snapshot, wiping any agent changes.

        Used on the failure path of ``run_ingest`` to put the wiki back to
        the pre-ingest state.
        """
        if snapshot_ref == "<clean>":
            # The tree was clean before the agent ran; just wipe whatever
            # the agent left behind. ``git checkout -- .`` covers tracked
            # files, ``git clean -fd`` covers untracked files and dirs.
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            return
        # The pre-ingest state was stashed. Drop the agent's changes and
        # restore the stash. ``git checkout`` + ``git clean`` discards the
        # agent's edits; ``git stash pop`` re-applies the pre-ingest state.
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        pop = subprocess.run(
            ["git", "stash", "pop"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if pop.returncode != 0:
            # The stash pop conflicted (e.g. the agent's changes touched
            # the same files the user had dirty). Drop the stash and
            # surface a clear error -- the user's pre-existing changes
            # are still preserved in the stash list, but we couldn't
            # safely merge them.
            subprocess.run(
                ["git", "stash", "drop"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            raise RuntimeError(
                "could not restore pre-ingest working tree: stash pop "
                "conflicted. Original state is preserved in the stash list."
            )

    @staticmethod
    def _discard_snapshot(repo: Path, snapshot_ref: str) -> None:
        """Drop the stash entry without applying it.

        Called on the success path of ``run_ingest`` -- the agent's changes
        are kept and the snapshot is no longer needed.
        """
        if snapshot_ref == "<clean>":
            return
        subprocess.run(
            ["git", "stash", "drop", snapshot_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    # -- F2 single-source ingest helpers --------------------------------------
    #
    # These two methods back the ingest-source flow (``lies ingest-source
    # <path> --collection <name>``). ``_materialize_source`` ensures the
    # source is on disk under ``raw/<collection>/<basename>``; the page
    # writer's deps then call ``_list_existing_pages`` so the agent sees
    # the existing wiki corpus before proposing PageDiff operations. Both
    # are pure deterministic host-side helpers (no LLM call), so they live
    # on the Orchestrator rather than on WikiMemoryService.

    def _list_existing_pages(self, collection: str) -> list[tuple[str, str]]:
        """Walk ``wiki/<collection>/`` returning
        ``(data-root-relative path, summary)`` pairs.

        The path is ``data_root``-relative and keeps the ``wiki/`` prefix
        (e.g., ``wiki/foo/concepts/alpha.md``) so the agent's
        existing-pages list maps 1-to-1 onto paths it can also write to.
        The summary is the frontmatter ``summary:`` field if present,
        else the first H1 plus the first non-empty line of body text.
        Excludes ``index.md``, ``log.md``, and anything under ``.lies/``
        or ``.git/``. Pure deterministic; no LLM call. Returns ``[]``
        when the collection directory does not exist.
        """
        out: list[tuple[str, str]] = []
        collection_dir = self.wiki.wiki_dir / collection
        if not collection_dir.exists():
            return out
        for path in sorted(collection_dir.rglob("*.md")):
            rel = path.relative_to(self.wiki.data_root).as_posix()
            parts = rel.split("/")
            if any(part in _EXCLUDED_TOP_LEVEL_DIRS for part in parts):
                continue
            if parts[-1] in {"index.md", "log.md"}:
                continue
            out.append((rel, _summarize_page(path)))
        return out

    def _materialize_source(self, source: str, collection: str) -> Path:
        """Ensure ``source`` is on disk under
        ``wiki.data_root/raw/<collection>/<basename>``.

        Branches:
        - URL (http/https): fetch via ``WebScraper.fetch`` and write.
        - local path: must exist; copy if outside ``raw/``, else pass-through.
        - ``'-'`` (stdin): read all of stdin, write to a stable basename.

        Raises :class:`IngestSourceUnreachable` on source-resolution
        failures (unreachable URL, missing local path, stdin read
        errors). Raw ``OSError`` (e.g. ``PermissionError`` from the
        ``mkdir`` / ``write_text`` / ``write_bytes`` disk I/O) is NOT
        wrapped — the caller (``run_ingest``) catches it alongside
        :class:`IngestSourceUnreachable` and discards the snapshot.
        """
        import shutil

        raw_root = self.wiki.raw_dir / collection
        raw_root.mkdir(parents=True, exist_ok=True)

        # Stdin branch: the source arrives over stdin; we need a real file
        # on disk for the agent pipeline. Read all of stdin and write to
        # ``raw/<collection>/stdin.md`` so the basename is stable.
        if source.strip() == "-":
            try:
                sys.stdin.seek(0)
                body = sys.stdin.read()
            except Exception as exc:
                raise IngestSourceUnreachable(source="stdin", reason=str(exc)) from exc
            target = raw_root / "stdin.md"
            target.write_text(body, encoding="utf-8")
            return target

        # URL branch: fetch via the project's WebScraper. The fetcher
        # already handles llms.txt / llms-full.txt walking and rejects
        # HTML / redirect-to-marketing responses; we just persist its
        # bytes under a stable basename.
        if source.startswith(("http://", "https://")):
            from lies.scrapers.web import WebScraper

            try:
                body = WebScraper().fetch(source)
            except Exception as exc:
                raise IngestSourceUnreachable(source=source, reason=str(exc)) from exc
            basename = _url_basename(source)
            target = raw_root / basename
            target.write_bytes(body)
            return target

        # Local-path branch: must exist on disk. Pass through when the
        # caller already pointed at the destination (avoids a redundant
        # copy that would otherwise wipe the file's mtime).
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise IngestSourceUnreachable(source=source, reason="local path missing")
        basename = path.name
        target = raw_root / basename
        if path != target:
            shutil.copy2(path, target)
        return target
