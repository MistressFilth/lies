"""Top-level orchestrator that dispatches user commands to sub-agents."""
from __future__ import annotations

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
    shell,
)
from lies.config import get_model
from lies.qmd import QmdMcpClient
from lies.schema import load_schema
from lies.wiki.layout import WikiLayout

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
                memory(),
                planning(),
                dynamic_workflow(agents=named_agents, max_agent_calls=20),
                file_system(wiki_root=self.layout.root),
                shell(allowlist=["qmd", "git"]),
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
