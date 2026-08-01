"""Top-level orchestrator that dispatches user commands to sub-agents."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai import Agent

from lies.agents.indexer import indexer_agent
from lies.agents.linter import LintReport, linter_agent
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
) -> str:
    """Produce a deterministic ``wiki/lint-report.md``.

    Walks the wiki looking for the cheapest-to-check issues (orphan
    pages, missing cross-references) so a host-side lint call always
    yields a real, non-empty artifact. Categories that require an LLM
    (contradictions, stale claims, data gaps) are recorded with zero
    findings here -- they still flow through the linter sub-agent in
    production; this host-side report is the deterministic shell.

    Args:
        layout: The wiki to lint.
        repair_receipt: Optional. When provided, the report includes
            an ``applied`` section describing which repair ops
            succeeded.
    """
    from lies.agents.linter import LintFinding, LintReport, LintSeverity

    findings: list[LintFinding] = []
    pages: set[str] = set()
    if layout.wiki_dir.exists():
        for path in layout.wiki_dir.rglob("*.md"):
            rel = path.relative_to(layout.root).as_posix()
            if rel in {"wiki/index.md", "wiki/log.md", "wiki/lint-report.md",
                       "wiki/overview.md"}:
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
                    safe_to_fix=False,
                )
            )

    report = LintReport(findings=findings, report_markdown="")
    body = _format_lint_markdown(report, layout)
    if repair_receipt is not None:
        body += "\n" + _format_repair_section(repair_receipt)
    report.report_markdown = body
    return body


def _extract_markdown_links(text: str) -> list[str]:
    """Extract ``(target)`` from markdown links via a tiny regex.

    Avoids a dependency on a full markdown parser; only the link target
    is needed for the orphan check.
    """
    import re

    return re.findall(r"\]\(([^)]+)\)", text)


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
    """Render the ``applied`` section of ``wiki/lint-report.md``."""
    lines = [f"### Applied ({len(receipt.applied)})", ""]
    for ref in receipt.applied:
        lines.append(f"- applied: {ref.op.value} — {ref.path}")
    if not receipt.applied:
        lines.append("_No repairs applied._")
    lines.append("")
    if receipt.skipped:
        lines.append(f"### Skipped ({len(receipt.skipped)})")
        lines.append("")
        for reason in receipt.skipped:
            lines.append(f"- {reason}")
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
            agent = factory(model=self.model)  # type: ignore[operator]
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
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT_PREFIX.format(
                wiki_root=self.layout.root
            )
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
            enrich_fn=lambda deps: self._enricher.run_sync(
                "Propose a MemoryPlan for the latest turn.", deps=deps
            ).output,
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

    def _enrichment_signal(
        self, pages_read: list[str], citations: list[str], command: str
    ) -> bool:
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

    def _generate_memory_plan_from_deps(
        self, deps: MemoryEnricherDeps
    ) -> MemoryPlan:
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
                current_page_metadata={
                    path: dict(values) for path, values in metadata.items()
                },
                active_schema=deps.active_schema,
            )
            retry_plan = self._generate_memory_plan_from_deps(retry_deps)
            if retry_plan.is_noop():
                return self._empty_memory_receipt()
            return self._memory_service.apply_plan(retry_plan)

    def _enqueue_and_report(
        self, deps: MemoryEnricherDeps, exc: BaseException
    ) -> MemoryReceipt:
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
        queued = [self._queued_reason(err) for err in receipt.errors
                  if err.startswith(self._QUEUED_RETRY_PREFIX)]
        other = [err for err in receipt.errors
                 if not err.startswith(self._QUEUED_RETRY_PREFIX)]
        if not receipt.changed_pages:
            return self._format_empty_receipt(queued, other)
        return self._format_durable_receipt(receipt, queued, other)

    def _queued_reason(self, err: str) -> str:
        """Strip the internal ``queued_for_retry:`` prefix from an error."""
        return err[len(self._QUEUED_RETRY_PREFIX):].lstrip()

    def _format_empty_receipt(self, queued: list[str], other: list[str]) -> str:
        """Format a receipt with no ``changed_pages``.

        - All queued: one ``(memory: queued for retry — <reason>)`` line
          per queued item (spec format).
        - Mixed: queued items in spec format, others comma-joined in a
          single ``(memory: ...)`` line.
        - No queued, no other: ``(memory: no change)``.
        """
        if queued and not other:
            return "\n".join(
                f"(memory: queued for retry — {reason})" for reason in queued
            )
        if queued and other:
            head = "\n".join(
                f"(memory: queued for retry — {reason})" for reason in queued
            )
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
        """Run an ingest with host-side atomicity and rollback.

        The orchestrator snapshots the wiki's working tree before invoking
        the agent, runs the ingest, then commits the result as a single
        atomic commit. If anything goes wrong -- the agent raises, the
        commit fails, or even an interrupt arrives -- the working tree
        is restored to its pre-ingest state and the exception is re-raised.

        The wiki never appears half-ingested to the caller: either the
        ingest commits cleanly with one new commit, or the wiki is exactly
        as it was before the call.

        Args:
            source: Path, URL, or '-' for stdin. Forwarded to the agent as
                ``"ingest {source}"``.

        Returns:
            The orchestrator's natural-language report of the ingest.

        Raises:
            Exception: Anything raised by the agent is re-raised after the
                wiki is restored to its pre-ingest state. ``CommitError``
                from the host-side commit step is also re-raised after
                rollback, and the working tree is restored even if the
                commit itself fails.
        """
        repo = self.wiki_root
        snapshot_ref = self._snapshot_working_tree(repo)
        try:
            output = self._agent.run_sync(f"ingest {source}").output
            output = str(output)
        except BaseException:
            # The agent raised (or was interrupted). Roll the working tree
            # back to the pre-ingest snapshot before propagating.
            self._restore_working_tree(repo, snapshot_ref)
            raise

        # Agent succeeded. Drop the snapshot (we want to keep the agent's
        # changes) and commit them atomically. If the commit itself fails,
        # restore the pre-ingest state -- the wiki should never be left
        # with a half-applied ingest.
        self._discard_snapshot(repo, snapshot_ref)
        try:
            self._commit_ingest(repo, source)
        except BaseException:
            self._restore_working_tree(repo, snapshot_ref)
            raise
        return output

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
        self._agent.run_sync("lint")
        lint_report = _build_lint_report(self.layout)
        repair_receipt: RepairReceipt | None = None
        if apply:
            plan = self._run_repair_agent(lint_report)
            repair_receipt = self._apply_repair_plan(plan)
        report = _build_lint_report(self.layout, repair_receipt=repair_receipt)
        self.layout.lint_report_path.write_text(report, encoding="utf-8")
        self._append_log_entry(
            f"## [{datetime.now(tz=timezone.utc).date().isoformat()}] lint | "
            f"{report.count(chr(10))} findings"
        )
        return report

    def _run_repair_agent(self, lint_report: str) -> RepairPlan:
        """Invoke the repair agent against the lint report."""
        from lies.agents.linter import LintReport
        # Parse the markdown report back into a structured LintReport
        # via the existing LintReport markdown helper.
        report_obj = LintReport(findings=[], report_markdown=lint_report)
        page_texts: dict[str, str] = {}
        for finding in report_obj.findings:
            for page in finding.pages:
                path = self.layout.wiki_dir / page
                if path.exists():
                    page_texts[page] = path.read_text(encoding="utf-8")
        return self._repair_agent.run_sync(
            "Propose a RepairPlan for the lint report.",
            deps=RepairAgentDeps(lint_report=report_obj, page_texts=page_texts),
        ).output

    def _apply_repair_plan(self, plan: RepairPlan) -> RepairReceipt:
        """Apply a RepairPlan through WikiMemoryService and return a receipt."""
        from lies.agents.repair_models import RepairReceipt as _Receipt
        if plan.is_noop():
            return _Receipt(
                applied=[],
                skipped=[],
                deferred=[],
                errors=[],
            )
        try:
            memory_receipt = self._memory_service.apply_repair_plan(plan)
        except Exception as exc:  # noqa: BLE001 - capture all apply failures
            return _Receipt(
                applied=[],
                skipped=[],
                deferred=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
                errors=[f"apply_failed: {type(exc).__name__}: {exc!s}"],
            )
        return _Receipt(
            applied=memory_receipt.changed_pages,
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
            raise RuntimeError(
                f"failed to snapshot working tree: {result.stderr.strip()}"
            )
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
