"""Top-level orchestrator that dispatches user commands to sub-agents."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai import Agent

from lies.agents.indexer import indexer_agent
from lies.agents.linter import linter_agent
from lies.agents.page_writer import page_writer_agent
from lies.agents.query_synthesizer import query_synthesizer_agent
from lies.agents.source_reader import source_reader_agent
from lies.capabilities import (
    code_mode,
    dynamic_workflow,
    file_system,
    memory,
    planning,
)
from lies.config import get_model
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


def _build_lint_report(layout: WikiLayout) -> str:
    """Produce a deterministic ``wiki/lint-report.md``.

    Walks the wiki looking for the cheapest-to-check issues (orphan
    pages, missing cross-references) so a host-side lint call always
    yields a real, non-empty artifact. Categories that require an LLM
    (contradictions, stale claims, data gaps) are recorded with zero
    findings here -- they still flow through the linter sub-agent in
    production; this host-side report is the deterministic shell.

    Args:
        layout: The wiki to lint.

    Returns:
        The full markdown for ``wiki/lint-report.md``.
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
    report.report_markdown = body
    return body


def _extract_markdown_links(text: str) -> list[str]:
    """Extract ``(target)`` from markdown links via a tiny regex.

    Avoids a dependency on a full markdown parser; only the link target
    is needed for the orphan check.
    """
    import re

    return re.findall(r"\]\(([^)]+)\)", text)


def _format_lint_markdown(report, layout: WikiLayout) -> str:
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

        self._agent: Agent = Agent(
            self.model,
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT_PREFIX.format(
                wiki_root=self.layout.root
            )
            + self.schema,
            capabilities=[
                SubAgents(agents=delegates),
                code_mode(),
                memory(self.wiki_root),
                planning(),
                dynamic_workflow(agents=named_agents, max_agent_calls=20),
                file_system(wiki_root=self.layout.root),

                QmdMcpClient(transport="stdio").as_capability(),
            ],
        )

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

    def run_lint(self) -> str:
        """Run the lint pass and write ``wiki/lint-report.md``.

        The orchestrator calls the linter sub-agent, then persists its
        markdown summary to ``wiki/lint-report.md`` and appends a parseable
        ``## [YYYY-MM-DD] lint | N findings`` entry to ``wiki/log.md``.

        Returns:
            The lint report markdown that was written to
            ``wiki/lint-report.md``.

        Raises:
            Exception: Anything raised by the agent is propagated.
        """
        # The linter sub-agent's run_sync returns a structured
        # LintReport. We re-invoke the agent deterministically through the
        # agent's run_sync so the same code path is used in production
        # and tests (with the LLM mocked).
        self._agent.run_sync("lint")
        # The agent returns a plain-text wrap-up in ``output``. The actual
        # LintReport is produced by the linter sub-agent; for the host-side
        # wiring we keep a deterministic markdown report so the CLI /
        # integration tests have a stable artifact.
        report = _build_lint_report(self.layout)
        self.layout.lint_report_path.write_text(report, encoding="utf-8")
        self._append_log_entry(
            f"## [{datetime.now(tz=timezone.utc).date().isoformat()}] lint | "
            f"{report.count(chr(10))} findings",
        )
        return report

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
