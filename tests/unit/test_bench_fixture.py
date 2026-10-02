"""Shape tests for the known-answer bench fixture.

The fixture is the regression gate for the qmd daemon-routing work: every
routing change is scored against the ``baseline`` recorded inside it. The
assertions below stand in for the validation ``qmd bench`` does not do —
it checks that ``queries`` is an array and nothing else.

Two views of the same answer are carried, and the split matters.

``qmd bench`` reads ``queries[]`` and, for each entry, passes
``expected_files`` to ``scoreResults`` and ``expected_in_top_k`` to the
top-k cut. It does **not** check either key exists, so a fixture missing
them scores a silent ``0`` — or, when ``expected_files`` is absent
entirely, throws a ``TypeError`` from iterating ``expectedFiles`` in
``scoreResults``, after the searches have already run.

``collections`` and ``expected`` are *not* read by ``qmd bench``: it takes
one global ``-c`` for the whole fixture, so per-query scope is
unexpressible there. They exist for the LIES-side retrieval gate, where a
query declares the collection scope it is meant to be answered from and
the single document that answers it, as a ``qmd://<collection>/<path>``
URI. Docids are forbidden as keys — ``docid`` is ``documents.hash[0:6]``
resolved by ``LIKE '<prefix>%' LIMIT 1`` with no ``ORDER BY``, and three
live collisions exist among the 5987 active documents.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

# The committed fixture. `QMD_BENCH_FIXTURE` overrides it, so the same
# shape tests can be pointed at a candidate fixture in Task 4/6 without
# editing the committed file — which is how "did this routing change
# regress recall" gets asked before the candidate becomes the fixture.
DEFAULT_FIXTURE = pathlib.Path(__file__).parents[1] / "fixtures" / "qmd_bench.json"


def fixture_path() -> pathlib.Path:
    """Resolve the fixture, honouring ``QMD_BENCH_FIXTURE``."""
    return pathlib.Path(os.environ.get("QMD_BENCH_FIXTURE", DEFAULT_FIXTURE))


# The default, for callers that want the committed fixture specifically.
FIXTURE = DEFAULT_FIXTURE

# The four collections the routing work must cover. `opencode` is required
# by the multi-collection starvation queries, so it is in the span even
# though it is not one of the four primary ones.
PRIMARY_COLLECTIONS = {"claude_code", "mermaid", "fastmcp", "typer"}


def _load() -> dict[str, Any]:
    return json.loads(fixture_path().read_text())


def test_fixture_path_honors_the_env_override(tmp_path: pathlib.Path, monkeypatch) -> None:
    """`QMD_BENCH_FIXTURE` must actually redirect the loader, not just parse."""
    override = tmp_path / "candidate.json"
    override.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "id": "candidate-1",
                        "query": "q",
                        "type": "exact",
                        "description": "d",
                        "expected": "qmd://c/x.md",
                        "expected_files": ["qmd://c/x.md"],
                        "expected_in_top_k": 1,
                        "collections": ["c"],
                    }
                ],
                "baseline": {
                    "qmd_bench": {"summary": {"bm25": {"avg_recall": 0.5}}},
                    "lies_gate": None,
                },
            }
        )
    )
    monkeypatch.setenv("QMD_BENCH_FIXTURE", str(override))
    assert fixture_path() == override
    assert _load()["queries"][0]["id"] == "candidate-1"

    monkeypatch.delenv("QMD_BENCH_FIXTURE")
    assert fixture_path() == DEFAULT_FIXTURE


def test_fixture_has_ten_or_more_queries() -> None:
    data = _load()
    assert len(data["queries"]) >= 10


def test_every_query_declares_its_expected_document() -> None:
    data = _load()
    for q in data["queries"]:
        assert q["collections"], q  # scope is required; unscoped queries
        assert q.get("expected"), q  # would not detect a routing regression
        assert q["expected"].startswith("qmd://"), q


def test_every_query_is_scoreable_by_qmd_bench() -> None:
    """`expected_files`/`expected_in_top_k` are what `qmd bench` actually reads.

    The first two tests would pass on a fixture that `qmd bench` cannot
    score at all, so the harness-facing half of the shape is asserted
    separately and pinned to the shipped `BenchmarkQuery` type.
    """
    data = _load()
    for q in data["queries"]:
        assert q["id"], q
        assert q["query"], q
        assert q["type"] in {"exact", "semantic", "topical", "cross-domain", "alias"}, q
        assert q["description"], q
        assert q["expected_files"], q
        assert q["expected_in_top_k"] >= 1, q


def test_expected_and_expected_files_agree() -> None:
    """`expected` is the single-answer view; `expected_files` is the scored set.

    Later tasks read `expected` (one known answer per query). If the two
    keys disagree, the gate scores a document the routing test never
    checks, and a regression in the checked document passes silently.
    """
    data = _load()
    for q in data["queries"]:
        assert q["expected_files"] == [q["expected"]], q


def test_fixture_declares_no_docid_keys() -> None:
    """Docids are unusable as keys: `hash[0:6]` has live prefix collisions.

    Walks the parsed structure rather than grepping the text, so the rule is
    stated as what it is — no string value is a `#docid` — instead of
    forbidding a character that JSON happens not to need.
    """
    offenders: list[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, str) and node.startswith("#"):
            offenders.append(node)

    _walk(_load())
    assert not offenders, offenders


def test_queries_span_at_least_four_collections() -> None:
    data = _load()
    spanned = {c for q in data["queries"] for c in q["collections"]}
    assert len(spanned) >= 4, spanned
    assert PRIMARY_COLLECTIONS <= spanned, spanned


def test_at_least_two_queries_are_multi_collection() -> None:
    """Starvation regression detection.

    The CLI's single `-c` cannot answer a multi-collection question; the
    daemon's `collections` filter can. A single-collection fixture cannot
    tell the two apart.
    """
    data = _load()
    multi = [q for q in data["queries"] if len(q["collections"]) > 1]
    assert len(multi) >= 2, [q["id"] for q in data["queries"]]


def test_at_least_one_query_is_a_paraphrase() -> None:
    """A query whose wording shares no vocabulary with its expected document.

    Without one, the fixture only measures lexical overlap and the
    `hyde`/paraphrase-count questions are unanswerable.
    """
    data = _load()
    paraphrase = [q for q in data["queries"] if q.get("paraphrase")]
    assert len(paraphrase) >= 1, [q["id"] for q in data["queries"]]


def test_baseline_is_recorded() -> None:
    """The recorded scores are the number every routing change is measured against."""
    data = _load()
    baseline = data["baseline"]
    assert baseline, "baseline is the regression gate; an empty one measures nothing"
    assert baseline["qmd_bench"]["summary"], baseline
    for backend, scores in baseline["qmd_bench"]["summary"].items():
        assert "avg_recall" in scores, backend
        assert 0.0 <= scores["avg_recall"] <= 1.0, (backend, scores)


def test_lies_gate_slot_is_reserved() -> None:
    """The `lies_gate` slot exists so Task 4/6 have somewhere to record.

    It is `null` until they fill it, and the suite stays green while it is.
    What matters is that the key is reserved and, once populated, carries
    the documented keys — otherwise the routing comparison lands
    somewhere unasserted and nothing checks its shape.
    """
    baseline = _load()["baseline"]
    assert "lies_gate" in baseline, "the LIES-routing gate slot must be reserved"

    gate = baseline["lies_gate"]
    if gate is None:
        return  # not yet recorded; the slot is what this test guards
    for key in ("recorded_from", "qmd_version", "method", "queries_total", "queries_passing"):
        assert key in gate, key
    assert 0 <= gate["queries_passing"] <= gate["queries_total"], gate
