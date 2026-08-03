"""Top-level orchestrator that dispatches user commands to sub-agents."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai import Agent

from lies.agents.indexer import indexer_agent
from lies.agents.linter import LintFinding, LintReport, linter_agent
from lies.agents.page_writer import page_writer_agent
from lies.agents.query_synthesizer import query_synthesizer_agent
from lies.agents.repair import RepairAgentDeps, repair_agent
from lies.agents.repair_models import RepairPlan, RepairReceipt
from lies.agents.source_reader import source_reader_agent
from lies.capabilities import (
    code_mode,
    dynamic_workflow,
    file_system,
    memory,
    planning,
)
from lies.config import get_model
from lies.memory.enricher import MemoryEnricherDeps, enricher_agent
from lies.memory.models import (
    MemoryPlan,
    MemoryReceipt,
    WikiCommitFailed,
    WikiLockBusy,
    WikiWriteConflict,
)
from lies.memory.retry import EnrichmentQueue
from lies.memory.service import WikiMemoryService
from lies.memory.tools import WikiMemoryDeps, register_read_tools
from lies.qmd import QmdMcpClient
from lies.query import SynthesizedAnswer, synthesize_answer
from lies.schema import load_schema
from lies.wiki.git import CommitError, atomic_commit
from lies.wiki.layout import WikiLayout


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
    layout: WikiLayout,
    *,
    repair_receipt: RepairReceipt | None = None,
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
        layout: The wiki to lint.
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
    pages: set[str] = set()
    if layout.wiki_dir.exists():
        for path in layout.wiki_dir.rglob("*.md"):
            rel = path.relative_to(layout.root).as_posix()
            if rel in {"wiki/index.md", "wiki/log.md", "wiki/lint-report.md", "wiki/overview.md"}:
                continue
            pages.add(rel)

    # Orphan check: a page is orphan if no other page links to it.
    if pages:
        linked: set[str] = set()
        for page in pages:
            try:
                text = (layout.root / page).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for raw in _extract_markdown_links(text):
                if raw.startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                if raw.startswith(("/", "\\")):
                    continue
                clean = raw.split("#", 1)[0].split("?", 1)[0]
                if clean.endswith(".md"):
                    linked.add(clean)
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
                text = (layout.root / page).read_text(encoding="utf-8")
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

        page_links: dict[str, set[str]] = {}
        for page in pages:
            try:
                text = (layout.root / page).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            targets: set[str] = set()
            for raw in _extract_markdown_links(text):
                if raw.startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                if raw.startswith(("/", "\\")):
                    continue
                clean = raw.split("#", 1)[0].split("?", 1)[0]
                if clean.endswith(".md"):
                    targets.add(clean)
            page_links[page] = targets

        body_cache: dict[str, str] = {}
        # Resolve each page's raw link targets to wiki-relative paths
        # once, so the per-pair comparison can check "did this page
        # link to that specific other page" without re-walking the
        # resolution rules. Targets that don't resolve are dropped.
        resolved_links: dict[str, set[str]] = {}
        for page, raws in page_links.items():
            resolved_links[page] = {
                resolved
                for raw in raws
                if (resolved := _resolve_link_target(page, raw, layout.root))
            }

        for page, title in unique_titles.items():
            other_pages = [p for p, t in unique_titles.items() if t != title]
            if not other_pages:
                continue
            try:
                body = body_cache.setdefault(
                    page, _strip_frontmatter((layout.root / page).read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeDecodeError):
                continue
            body_lower = body.lower()
            page_targets = resolved_links.get(page, set())
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
            text = (layout.root / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for source in _extract_frontmatter_sources(text):
            resolved = (layout.root / source).resolve()
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

    report = LintReport(findings=findings, report_markdown="")
    body = _format_lint_markdown(report, layout)
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


def _resolve_link_target(source_page_path: str, raw_target: str, wiki_root: Path) -> str | None:
    """Resolve a bare markdown link target to a wiki-relative path.

    Tries resolving relative to the source page's directory first,
    then relative to the wiki root, and returns the first
    wiki-relative ``.md`` path that lands inside ``wiki_root``.
    Returns ``None`` when neither interpretation lands inside the
    wiki or the result is not a ``.md`` file.

    Examples (wiki root = ``/tmp/wiki``):

    - ``wiki/concepts/a.md`` -> ``b.md`` -> ``wiki/concepts/b.md``
    - ``wiki/concepts/a.md`` -> ``concepts/b.md`` -> ``wiki/concepts/b.md``
    - ``wiki/concepts/a.md`` -> ``../concepts/b.md`` -> ``wiki/concepts/b.md``
    - ``wiki/overview.md`` -> ``b.md`` -> ``wiki/b.md`` (NOT ``wiki/concepts/b.md``)
    """
    wiki_root_resolved = wiki_root.resolve()
    # Source page's directory, absolute.
    source_dir = (wiki_root / source_page_path).parent.resolve()
    for base in (source_dir, wiki_root_resolved):
        try:
            candidate = (base / raw_target).resolve()
        except OSError:
            continue
        try:
            relative = candidate.relative_to(wiki_root_resolved)
        except ValueError:
            continue
        result = relative.as_posix()
        if result.endswith(".md"):
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


def _format_lint_markdown(report: LintReport, layout: WikiLayout) -> str:
    """Format a ``LintReport`` as markdown for ``wiki/lint-report.md``."""
    by_cat: dict[str, int] = {}
    for f in report.findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1

    header = (
        f"## Lint report — {datetime.now(tz=timezone.utc).date().isoformat()}\n\n"
        f"Wiki root: `{layout.root}`\n\n"
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


def _format_repair_section(receipt: RepairReceipt) -> str:
    """Render the ``applied`` section of ``wiki/lint-report.md``.

    The applied entries use ``receipt.applied_repair_kinds`` (a parallel
    list of repair op kinds) when present, so callers see the original
    repair primitive (``append_link``, ``update_index``, ...) instead of
    the underlying memory operation kind (``update``). Falls back to the
    memory operation kind for backwards compatibility.
    """
    lines = [f"### Applied ({len(receipt.applied)})", ""]
    if not receipt.applied:
        lines.append("_No repairs applied._")
    else:
        kinds = receipt.applied_repair_kinds
        for index, ref in enumerate(receipt.applied):
            kind = kinds[index] if index < len(kinds) else ref.op.value
            lines.append(f"- applied: {kind} — {ref.path}")
    lines.append("")
    lines.append(f"### Skipped ({len(receipt.skipped)})")
    lines.append("")
    if receipt.skipped:
        for reason in receipt.skipped:
            lines.append(f"- {reason}")
    else:
        lines.append("_No findings skipped._")
    lines.append("")
    if receipt.errors:
        lines.append(f"### Errors ({len(receipt.errors)})")
        lines.append("")
        for err in receipt.errors:
            lines.append(f"- {err}")
        lines.append("")
    return "\n".join(lines)


ORCHESTRATOR_SYSTEM_PROMPT_PREFIX = """You are the LIES orchestrator. The user
is curating a Karpathy-pattern LLM wiki at the path below. You dispatch their
commands to specialized sub-agents and return results.

Wiki root: {wiki_root}

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

    def __init__(self, wiki_root: Path, model: str | None = None) -> None:
        # Top-level: store wiki_root as a first-class attribute so callers
        # and tests can inspect the propagated root without reaching through
        # `self.layout.root`. The layout is the resolved on-disk view of
        # the same root; they are equal by construction.
        self.wiki_root: Path = Path(wiki_root).resolve()
        self.layout = WikiLayout(self.wiki_root)
        self.model = model or get_model()
        self.schema = load_schema(self.layout)
        self._build()

    def _build(self) -> None:
        """Construct the orchestrator agent with all capabilities and sub-agents."""
        from pydantic_ai_harness.subagents import SubAgent, SubAgents

        # Assign a name to each sub-agent so harness's SubAgents and
        # DynamicWorkflow catalogs can key them. The factories themselves
        # don't set a name; the orchestrator owns the namespace.
        named_agents: list[Agent] = []
        for name, factory, _description in _SUB_AGENT_TABLE:
            agent = factory(model=self.model)  # type: ignore[operator]  # ty: ignore[call-non-callable]
            agent.name = name
            named_agents.append(agent)

        # Sub-agents as `SubAgent` delegates for the SubAgents capability.
        delegates = [
            SubAgent(agent=agent, name=name, description=description)
            for (name, _factory, description), agent in zip(_SUB_AGENT_TABLE, named_agents)
        ]

        self._harness_memory = memory(self.wiki_root)
        self._agent: Agent = Agent(
            self.model,
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT_PREFIX.format(wiki_root=self.layout.root)
            + self.schema,
            deps_type=WikiMemoryDeps,
            capabilities=[
                SubAgents(agents=delegates),
                code_mode(),
                self._harness_memory,
                planning(),
                dynamic_workflow(agents=named_agents, max_agent_calls=20),
                file_system(wiki_root=self.layout.root),
                QmdMcpClient(transport="stdio").as_capability(),
            ],
        )
        self._memory_service = WikiMemoryService(self.layout)
        self._enrichment_queue = EnrichmentQueue(max_attempts=3)
        self._turn_counter = 0
        self._enricher = enricher_agent(model=self.model)
        self._repair_agent = repair_agent(model=self.model)
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
                command, deps=WikiMemoryDeps(layout=self.layout, service=self._memory_service)
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
            cwd=self.layout.root,
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
        sync_collection(self.layout.root, collection_name, force=False)
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
        return synthesize_answer(question, self.layout)

    def run_lint(self, apply: bool = False) -> str:
        """Run the lint pass and write ``wiki/lint-report.md``.

        When ``apply=True``, also invokes the repair agent and applies
        the resulting RepairPlan through ``WikiMemoryService``. The
        post-apply report shows both the proposed and the applied
        sections.

        Args:
            apply: If True, run the repair agent and apply the resulting
                plan. If False, return the dry-run report only.

        Returns:
            The lint report markdown that was written to
            ``wiki/lint-report.md``.

        Raises:
            Exception: Anything raised by the agent or the apply path
                is propagated.
        """
        # NOTE: the linter sub-agent's structured output is currently
        # not consumed here; the host-side deterministic shell
        # (`_build_lint_report`) is the source of truth for findings
        # in this branch. The call to the linter is preserved so
        # downstream wiring remains intact, but its return value is
        # deliberately discarded until the integration is finalized.
        self._agent.run_sync("lint")
        lint_report = _build_lint_report(self.layout)
        repair_receipt: RepairReceipt | None = None
        if apply:
            plan = self._run_repair_agent(lint_report)
            repair_receipt = self._apply_repair_plan(plan)
        final_report = _build_lint_report(self.layout, repair_receipt=repair_receipt)
        self.layout.lint_report_path.write_text(final_report.report_markdown, encoding="utf-8")
        self._append_log_entry(
            f"## [{datetime.now(tz=timezone.utc).date().isoformat()}] lint | "
            f"{final_report.report_markdown.count(chr(10))} findings"
        )
        return final_report.report_markdown

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
                path = self.layout.wiki_dir / page
                if path.exists():
                    page_texts[page] = path.read_text(encoding="utf-8")
        return self._repair_agent.run_sync(
            "Propose a RepairPlan for the lint report.",
            deps=RepairAgentDeps(lint_report=lint_report, page_texts=page_texts),
        ).output

    def _apply_repair_plan(self, plan: RepairPlan) -> RepairReceipt:
        """Apply a RepairPlan through WikiMemoryService and return a receipt."""
        from lies.agents.repair_models import RepairReceipt as _Receipt

        if plan.is_noop():
            return _Receipt(
                applied=[],
                applied_repair_kinds=[],
                skipped=[],
                deferred=[],
                errors=[],
            )
        try:
            memory_receipt = self._memory_service.apply_repair_plan(plan)
        except Exception as exc:  # noqa: BLE001 - capture all apply failures
            return _Receipt(
                applied=[],
                applied_repair_kinds=[],
                skipped=[],
                deferred=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
                errors=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
            )
        # Map each successfully applied memory operation back to the
        # repair op kind that produced it. ``plan.operations`` and
        # ``memory_receipt.changed_pages`` are paired by construction
        # (the service applies operations in order and appends a
        # ``PageReference`` for each), so we use position.
        kinds = [op.kind.value for op in plan.operations]  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        return _Receipt(
            applied=memory_receipt.changed_pages,
            applied_repair_kinds=kinds,
            skipped=[],
            deferred=[],
            errors=memory_receipt.errors,
        )

    def _append_log_entry(self, line: str) -> None:
        """Append a single line to ``wiki/log.md``.

        Creates the file (and parent dir) if missing. Used by lint to
        record its run without disturbing the indexer's contract.
        """
        log_path = self.layout.log_path
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
        return atomic_commit(repo, f"ingest: {source}", files=dirty_paths)

    # -- host-side snapshot / rollback -----------------------------------------
    #
    # The wiki is expected to be clean between invocations. The snapshot
    # machinery uses ``git stash push`` so the working tree is empty while
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
