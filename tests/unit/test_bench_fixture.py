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

import importlib.util
import json
import os
import pathlib
import re
import sys
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).parents[2]
GENERATOR_PATH = REPO_ROOT / "tools" / "qmd_bench_fixture.py"

# The committed fixture. `QMD_BENCH_FIXTURE` overrides it, so the same
# shape tests can be pointed at a candidate fixture in Task 4/6 without
# editing the committed file — which is how "did this routing change
# regress recall" gets asked before the candidate becomes the fixture.
DEFAULT_FIXTURE = pathlib.Path(__file__).parents[1] / "fixtures" / "qmd_bench.json"


def fixture_path() -> pathlib.Path:
    """Resolve the fixture, honouring ``QMD_BENCH_FIXTURE``."""
    return pathlib.Path(os.environ.get("QMD_BENCH_FIXTURE", DEFAULT_FIXTURE))


def generator() -> Any:
    """Import ``tools/qmd_bench_fixture.py`` as a module.

    ``tools/`` is not a package and is not on ``sys.path``, so it is
    loaded by path. The generator is imported rather than restated: the
    query table and the ``lies_gate`` key list are each a single source
    of truth, and a second transcription of either is how the two drift.
    """
    spec = importlib.util.spec_from_file_location("_qmd_bench_fixture", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_fixture_meets_the_step_four_minimum() -> None:
    """The brief's Step 4 asks for 12; the count above is a looser floor.

    Two separate numbers because they are two different claims: "the suite
    has a known-answer set worth scoring" (>=10) and "the brief's coverage
    requirement is met" (>=12). Trimming QUERIES to 10 must fail here.
    """
    data = _load()
    assert len(data["queries"]) >= 12, [q["id"] for q in data["queries"]]


def test_generator_refuses_to_wipe_a_recorded_baseline(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The bare generator invocation must not erase the recorded numbers.

    `uv run python tools/qmd_bench_fixture.py` used to be the first
    documented form, and an omitted `--baseline` meant it wrote
    `"baseline": {}` over the committed scores. It failed loudly, so it was
    recoverable, but the primary documented command destroyed the artifact
    it regenerates. It now refuses unless `--force` is passed.
    """
    out = tmp_path / "fixture.json"
    out.write_text(
        json.dumps(
            {
                "description": "x",
                "version": 1,
                "queries": [],
                "baseline": {"qmd_bench": {"summary": {"bm25": {"avg_recall": 0.5}}}},
            }
        )
    )
    before = out.read_text()

    monkeypatch.setattr(sys, "argv", ["qmd_bench_fixture.py", "--out", str(out)])
    with pytest.raises(SystemExit) as exc:
        generator().main()
    assert exc.value.code == 2, "a refusal should be argparse's usage error"
    assert out.read_text() == before, "the fixture must be left untouched"

    # --force is the documented way to discard a recorded baseline.
    monkeypatch.setattr(sys, "argv", ["qmd_bench_fixture.py", "--out", str(out), "--force"])
    generator().main()
    assert json.loads(out.read_text())["baseline"]["qmd_bench"] == {}


def test_committed_fixture_matches_the_generator() -> None:
    """The committed JSON is generated, not hand-maintained.

    A hand-edit correcting a wrong `expected` in the fixture looks like a
    fix and is silently reverted by the next regeneration. Pinning the
    committed file to `build()` means the correction has to happen in the
    generator, where it survives. The baseline half is excluded: it records
    a measurement of a run, not an input.
    """
    committed = _load()
    rebuilt = generator().build(committed["baseline"].get("qmd_bench") or None)
    for key in committed:
        if key != "baseline":
            assert committed[key] == rebuilt[key], key


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


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3}


def test_at_least_one_query_is_a_paraphrase() -> None:
    """A query whose wording shares no vocabulary with its expected document.

    What is actually checked, precisely: for every query flagged
    `paraphrase`, the query and the *expected document's path* share no
    content word of three or more letters. The document body is not in this
    repo, so "shares no vocabulary with the document" cannot be verified
    here and is not claimed — the path is a proxy that the generator
    controls, and it is enough to catch the real failure, which is a
    paraphrase quietly rewritten into a keyword query.

    The `paraphrase` flag on its own would not catch that: the fixture
    agreeing with itself is not evidence.
    """
    data = _load()
    paraphrase = [q for q in data["queries"] if q.get("paraphrase")]
    assert len(paraphrase) >= 1, [q["id"] for q in data["queries"]]
    for q in paraphrase:
        query_words = _content_words(q["query"])
        path_words = _content_words(q["expected"].rsplit("/", 1)[-1])
        shared = query_words & path_words
        assert not shared, (q["id"], sorted(shared))


def test_baseline_is_recorded() -> None:
    """The recorded scores are the number every routing change is measured against."""
    data = _load()
    baseline = data["baseline"]
    assert baseline, "baseline is the regression gate; an empty one measures nothing"
    bench = baseline["qmd_bench"]
    assert bench["summary"], baseline
    for backend, scores in bench["summary"].items():
        assert "avg_recall" in scores, backend
        assert 0.0 <= scores["avg_recall"] <= 1.0, (backend, scores)


def test_baseline_is_bound_to_a_corpus_and_index() -> None:
    """A baseline is only comparable against the same corpus and index.

    It used to record the fixture's own absolute path, which is
    worktree-specific, wrong in every other checkout, and tells the reader
    nothing about what was measured.
    """
    bench = _load()["baseline"]["qmd_bench"]
    assert isinstance(bench["corpus_documents"], int) and bench["corpus_documents"] > 0
    assert bench["index_path"].endswith(".sqlite"), bench["index_path"]
    assert bench["qmd_version"].startswith("qmd "), bench["qmd_version"]


def test_latency_is_outside_the_gate_block() -> None:
    """`avg_latency_ms` swings ~10x on model-cache warmth, so it is not a metric.

    It is lifted out of `summary` rather than annotated in place: a task
    reading `summary[backend]["avg_latency_ms"]` mechanically would never
    see a note explaining why the number is noise.
    """
    bench = _load()["baseline"]["qmd_bench"]
    for backend, scores in bench["summary"].items():
        assert "avg_latency_ms" not in scores, backend
    assert bench["latency_observed_ms"], "the observation is kept, just not in the gate"
    assert bench["latency_note"], "and it says why it is not a gate"


def _assert_gate_shape(gate: dict[str, Any]) -> None:
    """The shape `lies_gate` must have once Task 4/6 populate it.

    Reads the key list from the generator rather than restating it, so the
    two cannot drift into disagreeing about what "valid" means.
    """
    for key in generator().LIES_GATE_KEYS:
        assert key in gate, key
    assert 0 <= gate["queries_passing"] <= gate["queries_total"], gate


def test_lies_gate_slot_is_reserved() -> None:
    """The `lies_gate` slot exists so Task 4/6 have somewhere to record.

    Task 4 populated the slot with a measured run of the
    fixture through ``lies.mcp.search``. The shape check is now
    strict — a null slot would mean the gate is empty and the
    routing comparison has no number to diff against.
    """
    baseline = _load()["baseline"]
    assert "lies_gate" in baseline, "the LIES-routing gate slot must be reserved"
    assert baseline["lies_gate_note"], "the slot's rationale must be readable from the file"

    gate = baseline["lies_gate"]
    assert gate is not None, (
        "Task 4 committed to populating the lies_gate; a null slot "
        "means the gate number was not recorded and the routing "
        "comparison has no number to diff against"
    )
    _assert_gate_shape(gate)


def test_lies_gate_shape_check_works_today() -> None:
    """Exercise the shape check against a populated sample.

    The committed value is `null`, so leaving the check inline would mean
    the first exercise of it is the first real value Task 4 records — a
    broken check discovered exactly when it matters. It runs here, on a
    well-formed sample and a malformed one.
    """
    sample = {key: None for key in generator().LIES_GATE_KEYS}
    sample.update(
        {
            "corpus_documents": 5987,
            "qmd_version": "qmd 2.5.3",
            "method": "lies ground() fan-out over the fixture's collections",
            "queries_total": 15,
            "queries_passing": 15,
        }
    )
    _assert_gate_shape(sample)  # a well-formed gate passes

    with pytest.raises(AssertionError):
        _assert_gate_shape({k: v for k, v in sample.items() if k != "method"})
    with pytest.raises(AssertionError):
        _assert_gate_shape({**sample, "queries_passing": 99})
