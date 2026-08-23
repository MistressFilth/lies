"""Top-level orchestrator that dispatches user commands to sub-agents."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.indexer import indexer_agent
from lies.agents.linter import LintFinding, LintReport, linter_agent
from lies.agents.page_writer import page_writer_agent
from lies.agents.query_synthesizer import query_synthesizer_agent
from lies.agents.repair import RepairAgentDeps, repair_agent
from lies.agents.repair_models import RepairPlan, RepairReceipt
from lies.agents.repair_validation import ValidatedRepairPlan, validate_plan
from lies.agents.source_reader import source_reader_agent
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
from lies.qmd import QmdCapability
from lies.query import SynthesizedAnswer, synthesize_answer
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
            except OSError, UnicodeDecodeError:
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
            except OSError, UnicodeDecodeError:
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
            except OSError, UnicodeDecodeError:
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
            except OSError, UnicodeDecodeError:
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
        except OSError, UnicodeDecodeError:
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
        except OSError, UnicodeDecodeError:
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

    def run_ingest(self, source: str) -> str:
        """Backward-compatible wrapper. Delegates to SyncOrchestrator.

        To be deleted in a follow-up release once CLI and tests migrate.
        """
        from lies.etl.sync_helper import sync_collection

        collection_name = Path(source).stem
        # The orchestrator still operates against a single on-disk wiki
        # rooted at ``self.wiki.data_root``; pass that path through to
        # sync_collection, which (in Task 11+) routes XDG lookups via
        # the per-wiki ``Wiki`` dataclass. Construct a Wiki mirroring the
        # legacy layout (every role pinned to data_root) so the XDG
        # lookups that may try to mkdir on a privileged path are
        # skipped — this back-compat shim keeps honoring the legacy
        # locations for quarantine and telemetry.
        sync_collection(self.wiki, collection_name, force=False)
        return f"ingested {source}"

    def run_query(self, question: str) -> SynthesizedAnswer:
        """Answer ``question`` using the wiki with the qmd→index fallback.

        Tries ``qmd query`` first. When qmd is unavailable, returns no
        results, or fails for any reason, falls back to reading the
        top-N pages referenced by ``wiki/index.md``.

        Deterministic and extractive: no LLM round-trip. The synthesizer
        returns a :class:`SynthesizedAnswer` whose ``fallback_used`` and
        ``fallback_reason`` fields describe how the answer was built.
        """
        return synthesize_answer(question, self.wiki)

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
                except OSError, UnicodeDecodeError:
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
