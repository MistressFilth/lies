"""Generate ``tests/fixtures/qmd_bench.json``.

Not part of the shipped package: this is a maintenance script, and the
fixture it writes is what ships (to the test suite, not to users).

    uv run python tools/qmd_bench_fixture.py --baseline /tmp/bench.json
    uv run python tools/qmd_bench_fixture.py --baseline /tmp/bench.json --force

There is deliberately no bare ``uv run python tools/qmd_bench_fixture.py``
form any more. The primary documented command used to be that one, and
because an omitted ``--baseline`` yielded ``"baseline": {}`` it quietly
overwrote the committed fixture's recorded numbers with nothing. Writing
an empty baseline over a populated one now needs ``--force``.

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

``tests/unit/test_bench_fixture.py`` imports this module and asserts the
committed JSON equals what ``build()`` produces, so a hand-edit to the
fixture is caught rather than silently reverted by the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "qmd_bench.json"

# The `lies_gate` shape Tasks 4 and 6 fill in. This tuple is the single
# source of truth: the test module imports it rather than restating it,
# and `--lies-gate` validates an incoming file against it.
#
# It is a *different measurement* from the `qmd bench` summary beside it,
# and the difference is the reason this slot exists. `qmd bench` opens its
# own store and calls qmd's four backends in-process — no daemon, no
# per-query collection scope, no LIES `ground()` fan-out. Changing how
# LIES dispatches a query therefore cannot move those numbers at all. The
# summary says "qmd's backends can find these answers"; `lies_gate` says
# "LIES, routed as it now routes, still finds them", and that is the
# number a routing change can regress.
LIES_GATE_KEYS = (
    "corpus_documents",
    "qmd_version",
    "method",
    "queries_total",
    "queries_passing",
)

# Where a Task 4 reader, who sees only the committed JSON and not this
# source, will find the reason the slot exists. Mirrored into the file.
LIES_GATE_NOTE = (
    "Not filled by qmd bench. That summary measures qmd's own four backends "
    "in-process (no daemon, no per-query collection scope, no LIES ground() "
    "fan-out), so no LIES routing change can move it. This slot measures LIES "
    "as it actually routes, and is therefore the only place the "
    "multi-collection starvation signal can land: the two multi-collection "
    "queries below are inert in the qmd_bench numbers, because the harness "
    "takes one global -c and never exercises per-query scope. Record here: "
    + ", ".join(LIES_GATE_KEYS)
    + "."
)

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


# The quality metrics that are stable across runs, and the one that is not.
# `avg_latency_ms` swings ~10x purely on whether the embedding and reranker
# models are warm, so it is lifted out of `summary` entirely: a task reading
# `summary[backend]["avg_latency_ms"]` mechanically would otherwise record a
# "latency regression" that is really a cold cache.
QUALITY_METRICS = (
    "avg_precision",
    "avg_recall",
    "avg_recall_at_1",
    "avg_recall_at_3",
    "avg_recall_at_5",
    "avg_mrr",
    "avg_f1",
)

LATENCY_NOTE = (
    "Observed once, at the time of recording. Not a gate: three runs against an "
    "unchanged index gave bit-identical quality metrics and 10x-different "
    "latency (cold vs warm embedding/reranker models). Compare the quality "
    "metrics; ignore this."
)


def _index_identity() -> dict[str, Any]:
    """Corpus and index identity, which is what actually binds a baseline.

    Not the fixture's own path: that is worktree-specific, wrong in every
    other checkout under the bare-repo+worktree layout, and self-referential.
    A baseline is only comparable against the same corpus and the same index.

    Read-only `qmd status` (the CLI's own description is "View index +
    collection health"); it is not one of the index-writing commands.
    """
    try:
        out = subprocess.run(["qmd", "status"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return {"index_path": "unknown", "corpus_documents": None}

    index = re.search(r"^\s*Index:\s*(.+)$", out, re.M)
    total = re.search(r"^\s*Total:\s*(\d+)\s+files indexed", out, re.M)
    return {
        "index_path": index.group(1).strip() if index else "unknown",
        "corpus_documents": int(total.group(1)) if total else None,
    }


def _qmd_bench_block(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a `qmd bench --json` result into the recorded ``qmd_bench`` block."""
    quality: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}
    for backend, scores in raw["summary"].items():
        quality[backend] = {k: scores[k] for k in QUALITY_METRICS if k in scores}
        if "avg_latency_ms" in scores:
            latency[backend] = scores["avg_latency_ms"]
    return {
        **_index_identity(),
        "qmd_version": _qmd_version(),
        "summary": quality,
        "latency_observed_ms": latency,
        "latency_note": LATENCY_NOTE,
    }


def build(
    baseline: dict[str, Any] | None, lies_gate: dict[str, Any] | None = None
) -> dict[str, Any]:
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
        "baseline": {
            "qmd_bench": baseline or {},
            "lies_gate": lies_gate,
            # The rationale is repeated here, not left in the generator's
            # source: the reader who needs it in Task 4 is reading this
            # file, not this module.
            "lies_gate_note": LIES_GATE_NOTE,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(os.environ.get("QMD_BENCH_FIXTURE", DEFAULT_OUT)),
        help="fixture path to write (default: $QMD_BENCH_FIXTURE, else %(default)s)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="a `qmd bench --json` result file whose summary is embedded as the baseline",
    )
    parser.add_argument(
        "--lies-gate",
        type=Path,
        help="a JSON file holding the recorded LIES-routing gate result (Task 4/6)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow writing an empty baseline over a populated one",
    )
    args = parser.parse_args()

    baseline: dict[str, Any] | None = None
    if args.baseline:
        baseline = _qmd_bench_block(json.loads(args.baseline.read_text()))

    # The bare invocation `uv run python tools/qmd_bench_fixture.py` used to
    # be the documented first form, and it wrote `"baseline": {}` over the
    # committed numbers. It fails loudly, so it is recoverable, but the
    # primary documented command should not be the one that destroys the
    # artifact it regenerates.
    if baseline is None and args.out.exists():
        try:
            existing = json.loads(args.out.read_text()).get("baseline", {})
        except json.JSONDecodeError:
            existing = {}
        if existing.get("qmd_bench") and not args.force:
            parser.error(
                f"{args.out} already records a baseline; refusing to overwrite it "
                f"with an empty one. Pass --baseline <qmd bench --json file>, or "
                f"--force if you really mean to discard the recorded numbers."
            )

    lies_gate: dict[str, Any] | None = None
    if args.lies_gate:
        lies_gate = json.loads(args.lies_gate.read_text())
        missing = [k for k in LIES_GATE_KEYS if k not in lies_gate]
        if missing:
            parser.error(f"--lies-gate is missing required keys: {', '.join(missing)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build(baseline, lies_gate), indent=2) + "\n")
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
