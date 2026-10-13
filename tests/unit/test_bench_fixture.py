"""Shape tests for the known-answer bench fixture."""

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

# The committed fixture. `QMD_BENCH_FIXTURE` overrides it, so the
# same shape tests can be pointed at a candidate fixture without
# editing the committed file.
DEFAULT_FIXTURE = pathlib.Path(__file__).parents[1] / "fixtures" / "qmd_bench.json"


def fixture_path() -> pathlib.Path:
    """Resolve the fixture, honouring ``QMD_BENCH_FIXTURE``."""
    return pathlib.Path(os.environ.get("QMD_BENCH_FIXTURE", DEFAULT_FIXTURE))


def generator() -> Any:
    """Import ``tools/qmd_bench_fixture.py`` as a module.

    ``tools/`` is not a package and not on ``sys.path``. Imported
    rather than restated so the query table and ``lies_gate`` key
    list each have one source of truth.
    """
    spec = importlib.util.spec_from_file_location("_qmd_bench_fixture", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# The four collections the routing work must cover. `opencode` is
# required by the multi-collection starvation queries.
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
    """The query count meets the agreed floor."""
    data = _load()
    assert len(data["queries"]) >= 12, [q["id"] for q in data["queries"]]


def test_generator_refuses_to_wipe_a_recorded_baseline(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The bare generator invocation must not erase the recorded numbers."""
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
    """The committed JSON is generated, not hand-maintained."""
    committed = _load()
    rebuilt = generator().build(
        committed["baseline"].get("qmd_bench") or None,
        committed["baseline"].get("lies_gate"),
        committed.get("decisions"),
    )
    for key in committed:
        if key not in ("baseline", "decisions"):
            assert committed[key] == rebuilt[key], key


def test_every_query_declares_its_expected_document() -> None:
    data = _load()
    for q in data["queries"]:
        assert q["collections"], q
        assert q.get("expected"), q
        assert q["expected"].startswith("qmd://"), q


def test_every_query_is_scoreable_by_qmd_bench() -> None:
    """`expected_files`/`expected_in_top_k` are what `qmd bench` actually reads."""
    data = _load()
    for q in data["queries"]:
        assert q["id"], q
        assert q["query"], q
        assert q["type"] in {"exact", "semantic", "topical", "cross-domain", "alias"}, q
        assert q["description"], q
        assert q["expected_files"], q
        assert q["expected_in_top_k"] >= 1, q


def test_expected_and_expected_files_agree() -> None:
    """`expected` is the single-answer view; `expected_files` is the scored set."""
    data = _load()
    for q in data["queries"]:
        assert q["expected_files"] == [q["expected"]], q


def test_fixture_declares_no_docid_keys() -> None:
    """Docids are unusable as keys: `hash[0:6]` has live prefix collisions."""
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
    """Starvation regression detection."""
    data = _load()
    multi = [q for q in data["queries"] if len(q["collections"]) > 1]
    assert len(multi) >= 2, [q["id"] for q in data["queries"]]


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3}


def test_at_least_one_query_is_a_paraphrase() -> None:
    """A query whose wording shares no vocabulary with its expected document."""
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
    """A baseline is only comparable against the same corpus and index."""
    bench = _load()["baseline"]["qmd_bench"]
    assert isinstance(bench["corpus_documents"], int) and bench["corpus_documents"] > 0
    assert bench["index_path"].endswith(".sqlite"), bench["index_path"]
    assert bench["qmd_version"].startswith("qmd "), bench["qmd_version"]


def test_latency_is_outside_the_gate_block() -> None:
    """`avg_latency_ms` swings ~10x on model-cache warmth, so it is not a metric."""
    bench = _load()["baseline"]["qmd_bench"]
    for backend, scores in bench["summary"].items():
        assert "avg_latency_ms" not in scores, backend
    assert bench["latency_observed_ms"], "the observation is kept, just not in the gate"
    assert bench["latency_note"], "and it says why it is not a gate"


def _assert_gate_shape(gate: dict[str, Any]) -> None:
    """The shape ``lies_gate`` must have.

    Reads the key list from the generator rather than restating it.
    """
    for key in generator().LIES_GATE_KEYS:
        assert key in gate, key
    assert 0 <= gate["queries_passing"] <= gate["queries_total"], gate


def test_lies_gate_slot_is_reserved() -> None:
    """The `lies_gate` slot exists; a strict shape test pins the gate."""
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
    """Exercise the shape check against a populated sample."""
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


DECISION_KEYS = ("hyde", "paraphrase_count", "recall_regression")


def test_decisions_block_exists_with_three_keys() -> None:
    """The plan's three open questions, all recorded."""
    decisions = _load().get("decisions")
    assert decisions is not None, "Task 6 must record a decisions block"
    assert set(decisions) == set(DECISION_KEYS), decisions


def test_hyde_decision_records_top1_and_rank_deltas() -> None:
    """The hyde decision carries top-1 and rank deltas."""
    hyde = _load()["decisions"]["hyde"]
    assert hyde["queries_measured"] == 15
    for q in hyde["per_query"]:
        assert q["baseline_top1"], q
        assert q["hyde_top1"], q
        assert isinstance(q["baseline_expected_rank"], int), q
        assert isinstance(q["hyde_expected_rank"], int), q
        assert isinstance(q["top1_changed"], bool), q
        assert isinstance(q["rank_changed"], bool), q
    assert hyde["top1_changed_count"] == sum(1 for q in hyde["per_query"] if q["top1_changed"])


def test_paraphrase_count_decision_records_the_curve() -> None:
    """The paraphrase-count curve carries per-query and aggregate data."""
    para = _load()["decisions"]["paraphrase_count"]
    assert para["queries_measured"] == 15
    for n in (1, 2, 3, 4):
        assert str(n) in para["passing_by_vec_count"], n
        assert str(n) in para["avg_latency_ms_by_vec_count"], n
    baseline_passing = _load()["baseline"]["lies_gate"]["queries_passing"]
    assert para["passing_by_vec_count"]["1"] == baseline_passing, (
        "vec1 passing must equal the LIES-baseline passing; otherwise "
        "the paraphrase-count decision was taken against a different "
        "lies_gate number than the one the fixture commits"
    )


def test_recall_regression_decision_records_diff_with_task4() -> None:
    """The recall-regression decision records the diff against the baseline."""
    rec = _load()["decisions"]["recall_regression"]
    assert rec["queries_total"] == 15
    assert rec["queries_passing"] == 13
    fixture_baseline_passing = _load()["baseline"]["lies_gate"]["queries_passing"]
    assert rec["queries_passing"] == fixture_baseline_passing, (
        "the recorded recall_regression.queries_passing must equal the "
        "lies_gate.queries_passing the fixture commits"
    )
    assert rec["regressions"] == [], rec["regressions"]
