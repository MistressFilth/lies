"""Run the bench fixture through LIES's ``search()`` and record the result.

The fixture's ``qmd_bench`` block measures qmd's own four backends
in-process — no daemon, no per-query collection scope, no LIES code on
the path. The ``lies_gate`` block is the only place a Task 4 reader
can see how LIES, routed as it now routes, scores the same fixture.
This script is the producer of that block.

Usage::

    uv run python tools/qmd_lies_gate.py > /tmp/lies_gate.json
    uv run python tools/qmd_bench_fixture.py --lies-gate /tmp/lies_gate.json

The output is a JSON object shaped to ``LIES_GATE_KEYS`` (the same
tuple ``tools/qmd_bench_fixture.py`` validates against), so the
fixture generator accepts it via ``--lies-gate``. A query is recorded
as *passing* when the LIES-routed result's top-1 hit matches the
fixture's ``expected`` (suffix-match, mirroring qmd bench's own
scoring so a query that lands in ``claude_code/agent-sdk/hooks.md``
for an expected ``claude_code/hooks.md`` reads the same as it would
under ``qmd bench``).

Why a separate script and not a one-liner in
``tools/qmd_bench_fixture.py``: the gate calls LIES code, which
imports the daemon seam and the qmd library, which import the
llama-stack. A measurement tool that imports the library under
test imports the thing it is measuring, and a fixture generator
that does the same would couple a maintenance script to the very
package it is about. Separating the two keeps each script's
import graph narrow.

Corpus identity (``corpus_documents``, ``qmd_version``) is read
from ``qmd status`` so the gate's metadata is the same source as
``qmd_bench``'s, and a future change to the corpus is visible in
both at once.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "qmd_bench.json"

# Mirror of qmd bench's ``scoreResults.pathsMatch`` — strip the
# ``qmd://`` prefix *and* the leading collection segment, lowercase,
# drop leading/trailing slashes, then compare by suffix. The
# collection-strip is the load-bearing half: qmd bench's normalised
# form is just the filename (e.g. ``hooks.md``), so a result at
# ``claude_code/agent-sdk/hooks.md`` matches an expected of
# ``claude_code/hooks.md`` via the ``ne.endsWith("hooks.md")``
# branch. A gate that does not strip the collection will report a
# false miss on every query where qmd placed the hit one segment
# deeper than the expected.
_QMD_PREFIX = "qmd://"


def _normalize_path(p: str) -> str:
    if p.startswith(_QMD_PREFIX):
        rest = p[len(_QMD_PREFIX) :]
        slash = rest.find("/")
        # Drop the collection segment entirely. qmd bench does
        # this (see ``dist/bench/score.js``) so an ``expected``
        # like ``qmd://claude_code/hooks.md`` matches any hit
        # whose path ends in ``hooks.md``. The cost: a query
        # whose expected is ``agents.md`` and whose top hit is
        # ``sub-agents.md`` also matches. The bench accepts that
        # imprecision, so the gate does too — anything stricter
        # would diverge from the ``qmd_bench`` numbers the gate
        # is meant to mirror.
        p = rest[slash + 1 :] if slash >= 0 else rest
    elif "/" in p:
        # Live daemon paths come back unprefixed as
        # ``<collection>/<page>``. Mirror the bench's behaviour
        # by dropping the collection segment.
        p = p.split("/", 1)[1]
    return p.lower().strip("/")


def _paths_match(result: str, expected: str) -> bool:
    nr, ne = _normalize_path(result), _normalize_path(expected)
    return nr == ne or nr.endswith(ne) or ne.endswith(nr)


def _corpus_identity() -> tuple[str, int]:
    """Read corpus document count and qmd version, mirroring the bench.

    Both are read from ``qmd status`` (CLI verb, not daemon tool),
    the same source ``qmd_bench``'s ``_index_identity`` consults.
    A direct subprocess rather than the daemon because the gate
    is a measurement tool — it should not import the seam it is
    about to test through.
    """
    out = subprocess.run(["qmd", "status"], capture_output=True, text=True, timeout=120).stdout
    total = re.search(r"^\s*Total:\s*(\d+)\s+files indexed", out, re.M)
    version = subprocess.run(
        ["qmd", "--version"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    return version or "unknown", int(total.group(1)) if total else 0


def _run_query(question: str, collections: list[str]) -> list[str]:
    """One LIES-routed query; returns the hit paths in rank order.

    The daemon-side collection list is the same as the LIES
    registry at the time of the gate run; the pre-check
    short-circuits on an unknown collection and returns no
    hits, which the gate records as a failure with the same
    semantics as a real miss. ``search.fn`` is the live MCP
    entry point; using it (rather than a private helper) is
    the point of the gate: the same code path a user invokes.
    """
    from lies.mcp.search import search

    result = search.fn(question=question, tag_expr="|".join(f"c:{c}" for c in collections))
    return [hit.get("path", "") for hit in result.get("hits", [])]


def _gate_from_fixture(fixture: dict, *, top_k: int = 1) -> dict:
    """Run every fixture query through LIES and count the top-K matches.

    The bench's ``expected_in_top_k`` is the k the bench scores
    against; the gate uses the same number per query so the two
    summaries mean the same thing.
    """
    version, corpus_docs = _corpus_identity()

    queries = fixture.get("queries", [])
    queries_total = len(queries)
    queries_passing = 0
    per_query: list[dict] = []
    for q in queries:
        expected = q.get("expected", "")
        if not expected:
            continue
        k = q.get("expected_in_top_k", top_k)
        hit_paths = _run_query(q["query"], q.get("collections", []))
        passing = any(_paths_match(p, expected) for p in hit_paths[:k])
        if passing:
            queries_passing += 1
        per_query.append(
            {
                "id": q.get("id"),
                "expected": expected,
                "top_hit": hit_paths[0] if hit_paths else None,
                "passing": passing,
            }
        )

    return {
        "corpus_documents": corpus_docs,
        "qmd_version": version,
        "method": (
            "lies.mcp.search() against the live daemon with collections "
            "pushed into qmd; suffix-match against the bench's `expected` "
            "per query, scored at the bench's `expected_in_top_k`."
        ),
        "queries_total": queries_total,
        "queries_passing": queries_passing,
        "per_query": per_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="fixture to score (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: stdout)",
    )
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text())
    gate = _gate_from_fixture(fixture)
    payload = json.dumps(gate, indent=2) + "\n"
    if args.out is not None:
        args.out.write_text(payload)
        print(
            f"recorded {gate['queries_passing']}/{gate['queries_total']} "
            f"passing ({gate['corpus_documents']} docs, {gate['qmd_version']}) "
            f"to {args.out}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
