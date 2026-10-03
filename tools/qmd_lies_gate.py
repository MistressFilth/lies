"""Run the bench fixture through LIES's ``search()`` and record the result.

The fixture's ``qmd_bench`` block measures qmd's own backends
in-process — no daemon, no per-query collection scope, no LIES
code on the path. The ``lies_gate`` block is the only place a
reader can see how LIES, routed as it routes, scores the same
fixture. This script is the producer of that block.

Usage::

    uv run python tools/qmd_lies_gate.py > /tmp/lies_gate.json
    uv run python tools/qmd_bench_fixture.py --lies-gate /tmp/lies_gate.json

Output is shaped to ``LIES_GATE_KEYS`` (the tuple
``tools/qmd_bench_fixture.py`` validates against), so the
fixture generator accepts it via ``--lies-gate``. A query passes
when the LIES-routed top-1 hit suffix-matches ``expected``
(mirrors qmd bench's own scoring).

Kept as a separate script (not folded into
``tools/qmd_bench_fixture.py``) so the gate's import graph stays
narrow — folding them would couple a maintenance script to the
package it is about. Corpus identity
(``corpus_documents``, ``qmd_version``) is read from
``qmd status``.
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

# Mirror of qmd bench's ``scoreResults.pathsMatch``: strip
# ``qmd://`` and the leading collection segment, lowercase, strip
# slashes, compare by suffix. The collection-strip is load-bearing:
# qmd bench's normalised form is just the filename (e.g.
# ``hooks.md``), so a hit at ``claude_code/agent-sdk/hooks.md``
# matches an expected of ``claude_code/hooks.md`` via the suffix
# branch. A gate that does not strip the collection reports a false
# miss on every hit one segment deeper than expected.
_QMD_PREFIX = "qmd://"


def _normalize_path(p: str) -> str:
    if p.startswith(_QMD_PREFIX):
        rest = p[len(_QMD_PREFIX) :]
        slash = rest.find("/")
        # Drop the collection segment so the result matches the
        # bench's normalised filename form. The cost: a query whose
        # expected is ``agents.md`` matches a top hit of
        # ``sub-agents.md``. The bench accepts that imprecision;
        # anything stricter diverges from the ``qmd_bench`` numbers
        # the gate mirrors.
        p = rest[slash + 1 :] if slash >= 0 else rest
    elif "/" in p:
        # Live daemon paths come back unprefixed.
        p = p.split("/", 1)[1]
    return p.lower().strip("/")


def _paths_match(result: str, expected: str) -> bool:
    nr, ne = _normalize_path(result), _normalize_path(expected)
    return nr == ne or nr.endswith(ne) or ne.endswith(nr)


def _corpus_identity() -> tuple[str, int]:
    """Read corpus document count and qmd version, mirroring the bench.

    A direct subprocess, not the daemon — the gate is a
    measurement tool and should not import the seam it tests.
    """
    out = subprocess.run(["qmd", "status"], capture_output=True, text=True, timeout=120).stdout
    total = re.search(r"^\s*Total:\s*(\d+)\s+files indexed", out, re.M)
    version = subprocess.run(
        ["qmd", "--version"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    return version or "unknown", int(total.group(1)) if total else 0


def _run_query(question: str, collections: list[str]) -> list[str]:
    """One LIES-routed query; returns hit paths in rank order.

    ``search.fn`` is the live MCP entry point; using it (rather
    than a private helper) is the point of the gate.
    """
    from lies.mcp.search import search

    result = search.fn(question=question, tag_expr="|".join(f"c:{c}" for c in collections))
    return [hit.get("path", "") for hit in result.get("hits", [])]


def _gate_from_fixture(fixture: dict, *, top_k: int = 1) -> dict:
    """Run every fixture query through LIES; count top-K matches.

    The bench's ``expected_in_top_k`` is the k the bench scores
    against; the gate uses the same number per query.
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


def _compare(measured: dict, committed: dict) -> int:
    """Report per-query movement against a committed gate block.

    Returns 0 when nothing regressed, 1 when a previously-passing query
    now fails. Improvements are reported but do not fail the run — the
    committed block is the reference, and a change that improves recall
    should be recorded deliberately rather than by a Make target.
    """
    old = {q["id"]: q["passing"] for q in committed.get("per_query", [])}
    moved = [
        q["id"]
        for q in measured["per_query"]
        if q["id"] in old and old[q["id"]] and not q["passing"]
    ]
    gained = [
        q["id"]
        for q in measured["per_query"]
        if q["id"] in old and not old[q["id"]] and q["passing"]
    ]
    print(
        f"lies_gate: {measured['queries_passing']}/{measured['queries_total']} passing "
        f"(committed {committed['queries_passing']}/{committed['queries_total']}, "
        f"corpus {measured['corpus_documents']} vs {committed['corpus_documents']} docs)"
    )
    for qid in moved:
        print(f"  REGRESSED: {qid}")
    for qid in gained:
        print(f"  improved: {qid}")
    return 1 if moved else 0


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
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="score, diff against the committed lies_gate block, and exit "
        "non-zero if a passing query regressed. Does not write the fixture.",
    )
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text())
    gate = _gate_from_fixture(fixture)

    if args.compare is not None:
        committed = json.loads(args.compare.read_text())["baseline"]["lies_gate"]
        return _compare(gate, committed)

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
