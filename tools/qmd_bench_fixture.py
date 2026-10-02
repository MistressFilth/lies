"""Generate ``tests/fixtures/qmd_bench.json``.

Not part of the shipped package: this is a maintenance script, and the
fixture it writes is what ships (to the test suite, not to users).

    uv run python tools/qmd_bench_fixture.py
    uv run python tools/qmd_bench_fixture.py --baseline /tmp/bench.json

Every ``expected`` below was verified against the live index before it
was written down, not guessed. The oracle is ``qmd bench`` itself: it
reports ``matched_files`` per query per backend, so a wrong expectation
is visible as an empty match list rather than as a silently-passing
gate. Run it from the library root so it scores the real index::

    cd ~/.local/share/lies/library
    qmd bench <repo>/tests/fixtures/qmd_bench.json --json > /tmp/bench.json

Fixture shape, and why it carries two views of the same answer
----------------------------------------------------------------
``qmd bench`` reads ``queries[]`` and scores ``expected_files`` /
``expected_in_top_k``; see ``dist/bench/types.d.ts``. It reads nothing
else, and it validates nothing. ``collections`` and ``expected`` are
therefore *not* seen by the bench -- they exist for the LIES-side
retrieval gate that later tasks run against the same file:

- ``collections``  -- the collection scope a query is meant to be
  answered from. ``qmd bench`` has one global ``-c`` for the whole
  fixture and cannot express this, so it is carried as data. At least
  two queries name two collections, because the CLI's single ``-c``
  cannot answer a multi-collection question at all and a
  single-collection fixture cannot tell starvation from a routing bug.
- ``expected``     -- the single known answer, as ``qmd://<collection>/
  <path>``. Docids are forbidden as keys: ``docid`` is
  ``documents.hash[0:6]`` resolved by ``LIKE '<prefix>%' LIMIT 1`` with
  no ``ORDER BY``, and three live collisions exist among the 5987
  active documents.

``expected_files`` is kept equal to ``[expected]`` -- one known answer
per query, so ``precision_at_k`` is not diluted by a filler entry.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "qmd_bench.json"

# (id, query, expected, collections, type, description, paraphrase)
#
# `paraphrase=True` marks a query whose wording deliberately shares no
# vocabulary with the document that answers it, so the hyde /
# paraphrase-count questions downstream are answerable at all.
QUERIES: list[dict[str, Any]] = [
    {
        "id": "cc-hooks-pretooluse",
        "query": "PreToolUse hook matcher settings",
        "expected": "qmd://claude_code/hooks.md",
        "collections": ["claude_code"],
        "type": "exact",
        "description": "Hook event names and matcher regex in the hooks config.",
    },
    {
        "id": "cc-sandbox-rules",
        "query": "sandbox permission deny allow rules",
        "expected": "qmd://claude_code/sandboxing.md",
        "collections": ["claude_code"],
        "type": "exact",
        "description": "Filesystem and network allow/deny rules for the sandbox.",
    },
    {
        "id": "cc-subagents",
        "query": "subagents Task tool parallel delegation",
        "expected": "qmd://claude_code/sub-agents.md",
        "collections": ["claude_code"],
        "type": "exact",
        "description": "Running a delegated subagent and collecting its result.",
    },
    {
        "id": "cc-agent-teams",
        "query": "agent teams coordinated parallel teammates",
        "expected": "qmd://claude_code/agent-teams.md",
        "collections": ["claude_code"],
        "type": "topical",
        "description": "Multi-agent team layout, distinct from plain subagents.",
    },
    {
        "id": "cc-context-window-paraphrase",
        "query": "the conversation keeps growing until it no longer fits; what happens then?",
        "expected": "qmd://claude_code/context-window.md",
        "collections": ["claude_code"],
        "type": "semantic",
        "description": (
            "Paraphrase case: the wording shares no vocabulary with the document "
            "(no 'context', 'window', or 'compact'). Neither BM25 nor the reranked "
            "pipeline matches it; only the vector and hybrid backends do, which is "
            "what makes the hyde / paraphrase-count question measurable."
        ),
        "paraphrase": True,
    },
    {
        "id": "mm-sequence-diagram",
        "query": "sequence diagram actor activation",
        "expected": "qmd://mermaid/packages/mermaid/src/docs/syntax/sequencediagram.md",
        "collections": ["mermaid"],
        "type": "exact",
        "description": "Sequence diagram syntax: participants, messages, activation.",
    },
    {
        "id": "mm-gantt",
        "query": "gantt chart dateFormat excludes",
        "expected": "qmd://mermaid/packages/mermaid/src/docs/syntax/gantt.md",
        "collections": ["mermaid"],
        "type": "exact",
        "description": "Gantt chart sections, dateFormat, and excludes.",
    },
    {
        "id": "mm-flowchart",
        "query": "flowchart subgraph direction",
        "expected": "qmd://mermaid/packages/mermaid/src/docs/syntax/flowchart.md",
        "collections": ["mermaid"],
        "type": "exact",
        "description": "Flowchart node shapes, subgraphs, and edge direction.",
    },
    {
        "id": "mm-class-diagram",
        "query": "classDiagram class relation",
        "expected": "qmd://mermaid/packages/mermaid/src/docs/syntax/classdiagram.md",
        "collections": ["mermaid"],
        "type": "exact",
        "description": "Class diagram relations and cardinality markers.",
    },
    {
        "id": "fm-tool-output-schema",
        "query": "tool output schema",
        "expected": "qmd://fastmcp/servers/tools.md",
        "collections": ["fastmcp"],
        "type": "exact",
        "description": "Declaring tools and their structured output schema.",
    },
    {
        "id": "fm-server-context",
        "query": "auth middleware dependency injection",
        "expected": "qmd://fastmcp/servers/context.md",
        "collections": ["fastmcp"],
        "type": "topical",
        "description": "Server context, dependency injection, and middleware.",
    },
    {
        "id": "ty-parameters",
        "query": "command line parameter types",
        "expected": "qmd://typer/parameters.md",
        "collections": ["typer"],
        "type": "semantic",
        "description": (
            "typer parameters. Rank 1 under BM25 scoped to typer, but falls out of "
            "the unscoped BM25 top 10 that qmd bench uses (ftsLimit = limit when no "
            "collection filter is set), so only the vector/hybrid backends match it. "
            "Retained because a collection-scoped keyword search that the unscoped "
            "one loses is exactly the behaviour the routing change touches."
        ),
    },
    {
        "id": "ty-file-objects",
        "query": "file-like object types",
        "expected": "qmd://typer/file-objects.md",
        "collections": ["typer"],
        "type": "exact",
        "description": "typer's FileText / FileBinaryRead family. Reachable by all four backends.",
    },
    {
        "id": "mc-shell-permissions",
        "query": "ask before running shell commands allow block ordered rules",
        "expected": "qmd://opencode/v2/docs/permissions.md",
        "collections": ["claude_code", "opencode"],
        "type": "cross-domain",
        "description": (
            "Multi-collection: both corpora document ordered shell-permission "
            "rules (claude_code/permissions.md and opencode/v2/docs/permissions.md "
            "both match this query). Scoping to one collection starves the other."
        ),
    },
    {
        "id": "mc-custom-commands",
        "query": "define a custom command that expands to a prompt",
        "expected": "qmd://opencode/v2/docs/commands.md",
        "collections": ["claude_code", "opencode"],
        "type": "cross-domain",
        "description": "Multi-collection: user-defined slash commands in both corpora.",
    },
]


def _entry(spec: dict[str, Any]) -> dict[str, Any]:
    """Expand a spec into the full ``BenchmarkQuery`` plus the LIES-side keys."""
    expected = spec["expected"]
    return {
        "id": spec["id"],
        "query": spec["query"],
        "type": spec["type"],
        "description": spec["description"],
        "expected": expected,
        "expected_files": [expected],
        "expected_in_top_k": 1,
        "collections": list(spec["collections"]),
        **({"paraphrase": True} if spec.get("paraphrase") else {}),
    }


def build(baseline: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "description": (
            "Known-answer benchmark for the LIES qmd read surface. Twelve-plus "
            "queries spanning five collections, two of them multi-collection and "
            "one a deliberate paraphrase. Regenerate with tools/qmd_bench_fixture.py; "
            "every expected path is verified against the live index, never guessed."
        ),
        "version": 1,
        # No fixture-level `collection`: `qmd bench` takes one -c for the whole
        # fixture, so scoping here would exclude every collection but one. The
        # per-query `collections` key carries the scope instead.
        "queries": [_entry(spec) for spec in QUERIES],
        "baseline": baseline or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="fixture path to write (default: %(default)s)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="a `qmd bench --json` result file whose summary is embedded as the baseline",
    )
    args = parser.parse_args()

    baseline: dict[str, Any] | None = None
    if args.baseline:
        raw = json.loads(args.baseline.read_text())
        baseline = {
            "recorded_from": raw["fixture"],
            "qmd_version": _qmd_version(),
            # Compare the quality metrics only. Two runs of the same fixture
            # against an unchanged index produced bit-identical precision,
            # recall, MRR and F1; avg_latency_ms moved from 11666 to 1152
            # (hybrid) purely because the embedding and reranker models were
            # warm on the second run. A latency delta here is model-cache
            # state, not a routing regression.
            "note": "Gate on precision/recall/MRR/F1. avg_latency_ms is not comparable across runs.",
            "summary": raw["summary"],
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build(baseline), indent=2) + "\n")
    print(f"wrote {len(QUERIES)} queries to {args.out}")
    return 0


def _qmd_version() -> str:
    try:
        out = subprocess.run(
            ["qmd", "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out or "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
