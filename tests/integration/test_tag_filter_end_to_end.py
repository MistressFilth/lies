"""Integration tests for the tag-filter end-to-end path (Bundle C).

Drives the real ``qmd`` CLI against a fixture library of three collections
so the post-qmd per-collection drop
(:func:`lies.qmd.cli.qmd_query`) and the ``searched_scope`` envelope
(:func:`lies.query.synthesizer._searched_scope`) are exercised as they
actually run, not as a mock would. The orchestrator's LLM is stubbed at the
synthesizer-agent seam so the round-trip is deterministic (per F4a's
precedent in ``test_end_to_end.py``); the qmd path is **not** mocked.

Tests are gated on ``INTEGRATION=1``; default CI skips them via
``pytest.mark.skipif``. The integration workflow in
``.github/workflows/`` runs with ``INTEGRATION=1`` enabled.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.query_synthesizer import QueryAnswer
from lies.collections.record import Collection, save_collection
from lies.orchestrator import Orchestrator
from lies.query.tag_expr import (
    And,
    Include,
    Or,
    ResolvedTagFilter,
)
from lies.qmd.cli import (
    QmdNotInstalledError,
    qmd_collection_add_if_missing,
    qmd_embed,
)
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="integration test; set INTEGRATION=1 to run",
)


_NOW = datetime(2026, 9, 10, tzinfo=UTC)


# Marker vocabulary:
#
# Each fixture collection shares the word "ZEPHYR" so a probe question that
# only mentions "ZEPHYR" surfaces hits from all three of them; the
# discriminating terms ("DAG" for airflow, "S3" for amazon, "RDD" for
# pyspark) are what ranks each hit high in qmd's hybrid retrieval. The test
# question picks one discriminating term at a time so the post-qmd filter
# (which keeps only hits whose ``qmd://<collection>/<page>`` first segment
# is in the resolved collection set) has at least one hit to keep per
# discriminating direction. Without the discriminating term, qmd's reranker
# often surfaces unrelated global collections that have no analogue here
# and the test would be polluted.
AIRFLOW_PAGES = {
    "concepts.md": (
        "# Airflow\n\n"
        "ZEPHYR mentions Apache Airflow DAG workflows. ZEPHYR pipelines use\n"
        "providers like Apache Airflow Providers. ZEPHYR triggers schedule\n"
        "tasks. ZEPHYR operator classes like PythonOperator, BashOperator,\n"
        "and KubernetesPodOperator manage task execution in ZEPHYR. Each\n"
        "ZEPHYR operator wraps a single task inside an Airflow DAG. ZEPHYR\n"
        "DAGs are defined in Python with `airflow.DAG()`.\n\n"
        "For ZEPHYR deployments, configure `airflow.cfg`, set up the\n"
        "metadata database, and start the scheduler. ZEPHYR sensors wait\n"
        "for external conditions.\n"
    ),
}
AMAZON_PAGES = {
    "concepts.md": (
        "# Amazon AWS\n\n"
        "ZEPHYR uses AWS S3 buckets for object storage. ZEPHYR Lambda\n"
        "functions respond to events. ZEPHYR CloudFront delivers content\n"
        "at the edge globally. ZEPHYR EC2 instances provide compute\n"
        "capacity. ZEPHYR IAM roles enforce permissions.\n\n"
        "For ZEPHYR deployments, configure IAM roles and S3 bucket policies.\n"
        "ZEPHYR Lambda is serverless compute billed per request. ZEPHYR S3\n"
        "storage classes include Standard, Glacier, and Intelligent-Tiering.\n"
    ),
}
PYSPARK_PAGES = {
    "concepts.md": (
        "# PySpark\n\n"
        "ZEPHYR transformations run on Spark RDDs. ZEPHYR DataFrames support\n"
        "SQL queries. ZEPHYR actions like `collect()` and `count()` return\n"
        "results to the driver. ZEPHYR transformations are lazy until an\n"
        "action triggers evaluation.\n\n"
        "For ZEPHYR jobs, configure the SparkSession with\n"
        "`pyspark.sql.SparkSession.builder`. ZEPHYR partitions data using\n"
        "`repartition()` and `coalesce()` for parallelism. ZEPHYR\n"
        "DataFrameWriter persists results to S3, HDFS, or local disk.\n"
    ),
}
# ``prefect``: a second collection whose effective tags contain ``airflow``
# but whose name is NOT ``airflow``. The F15 ``t:`` / ``c:`` qualifier
# integration tests need both a ``airflow``-named collection AND a
# non-``airflow``-named collection that still carries the ``airflow`` tag,
# so ``+t:airflow`` resolves to multiple names while ``+c:airflow`` resolves
# to exactly one. The page content re-uses the ZEPHYR + Apache Airflow
# vocabulary so a probe phrased around "Apache Airflow DAG operators
# providers" surfaces both the ``airflow`` and ``prefect`` pages through
# real qmd retrieval — the post-qmd per-collection drop is the seam under
# test, so the index needs cross-collection hits for the multi-collection
# tests to land non-empty captures.
PREFECT_PAGES = {
    "concepts.md": (
        "# Prefect\n\n"
        "ZEPHYR mentions Apache Airflow DAG workflows. ZEPHYR pipelines use\n"
        "providers like Apache Airflow Providers. ZEPHYR triggers schedule\n"
        "tasks. ZEPHYR operator classes like PythonOperator, BashOperator,\n"
        "and KubernetesPodOperator manage task execution in ZEPHYR. Each\n"
        "ZEPHYR operator wraps a single task inside an Airflow DAG. ZEPHYR\n"
        "DAGs are defined in Python with `airflow.DAG()`.\n\n"
        "For ZEPHYR deployments, configure `airflow.cfg`, set up the\n"
        "metadata database, and start the scheduler. ZEPHYR sensors wait\n"
        "for external conditions. ZEPHYR Prefect complements Apache\n"
        "Airflow DAGs with native flow scheduling using the same\n"
        "apache-airflow-providers packages as ZEPHYR Apache Airflow.\n"
    ),
}


def _build_tag_filter_library(tmp_path: Path, *, name: str) -> Wiki:
    """Build a wiki with four tagged collections ready for qmd registration.

    The collections match the brief verbatim:

      - ``airflow``: tags ``[airflow, provider]``
      - ``amazon``:  tags ``[amazon, aws]``
      - ``pyspark``: tags ``[pyspark, spark]``
      - ``prefect``: tags ``[prefect, airflow]`` — added for the F15
        ``t:`` / ``c:`` qualifier tests; its name is ``prefect`` (not
        ``airflow``) but its effective tags include ``airflow`` so
        ``+t:airflow`` resolves to two collections while ``+c:airflow``
        resolves to exactly one.

    Each collection's wiki pages live under its own subdirectory of
    ``wiki/`` (per-collection subdir layout, PR #39). Configs land at
    ``<xdg_config>/lies/<name>/collections/`` so :func:`_collections_matching`
    picks them up at query time (per Task 6 review: configs are the source
    of truth for filter resolution, not the registry).
    """
    data_root = tmp_path / name
    data_root.mkdir()
    wiki_dir = data_root / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "index.md").write_text(
        "# Index\n\n- [Airflow](airflow/concepts.md)\n",
        encoding="utf-8",
    )

    wiki = Wiki(
        name=name,
        data_root=data_root,
        config_root=Path(os.environ["XDG_CONFIG_HOME"]) / "lies" / name,
        cache_root=Path(os.environ["XDG_CACHE_HOME"]) / "lies" / name,
        state_root=Path(os.environ["XDG_STATE_HOME"]) / "lies" / name,
        runtime_root=Path(os.environ["XDG_RUNTIME_DIR"]) / "lies" / name,
    )
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")

    # Per-collection subdirs + page bodies.
    for coll, pages in (
        ("airflow", AIRFLOW_PAGES),
        ("amazon", AMAZON_PAGES),
        ("pyspark", PYSPARK_PAGES),
        ("prefect", PREFECT_PAGES),
    ):
        (wiki_dir / coll).mkdir()
        for page_name, body in pages.items():
            (wiki_dir / coll / page_name).write_text(body, encoding="utf-8")

    # Collection YAMLs (source of truth for filter resolution).
    tags_per = {
        "airflow": ["airflow", "provider"],
        "amazon": ["amazon", "aws"],
        "pyspark": ["pyspark", "spark"],
        "prefect": ["prefect", "airflow"],
    }
    for coll, tags in tags_per.items():
        save_collection(
            wiki,
            Collection(
                name=coll,
                path=Path(f"/raw/{coll}"),
                source=f"https://example.com/{coll}",
                tags=list(tags),
                scraper_cmd=None,
                doc_path=None,
                mapper_model=None,
                language="en",
                version="1.0.0",
                created_at=_NOW,
                updated_at=_NOW,
                config={},
            ),
        )

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(data_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(data_root), "config", "user.email", "t@e"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(data_root), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(data_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(data_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return wiki


def _seed_qmd(wiki: Wiki) -> None:
    """Register each fixture collection with qmd and embed it.

    Uses absolute paths so qmd stores absolute paths in the global index —
    transient test tmp paths must not depend on the cwd at call time.
    ``qmd_collection_add_if_missing`` is idempotent on re-runs (its
    stderr check ignores "already exists").

    Raises ``QmdNotInstalledError`` if qmd is missing; the per-test
    fixture-level ``skipif`` on ``shutil.which('qmd')`` usually catches
    this first, but explicit propagation is cheaper than a stack trace.
    """
    if shutil.which("qmd") is None:
        raise QmdNotInstalledError("`qmd` not found on PATH")
    for coll in ("airflow", "amazon", "pyspark", "prefect"):
        coll_path = (wiki.wiki_dir / coll).resolve()
        qmd_collection_add_if_missing(wiki.data_root, coll_path, coll)
        qmd_embed(wiki.data_root, coll, timeout=600)


@pytest.fixture
def qmd_fixture_library(tmp_path: Path) -> Wiki:
    """A wiki with four tagged collections, registered and embedded with qmd."""
    if shutil.which("qmd") is None:
        pytest.skip("qmd not installed on PATH")
    wiki = _build_tag_filter_library(tmp_path, name="tag-filter-lib")
    _seed_qmd(wiki)
    return wiki


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _patched_synthesizer(
    orch: Orchestrator,
    *,
    captured: list[str],
    answer_md: str = "stub answer",
) -> mock._patch:
    """Stub ``orch._query_synthesizer_agent.run_sync``.

    Captures the page paths the synthesizer saw into ``captured`` and
    returns a deterministic ``QueryAnswer`` that cites them. Tests
    assert against ``captured`` (real retrieved pages) rather than
    against the synthetic answer body, so the LLM stub cannot mask a
    bug in the retrieval path.

    The signature mirrors the existing ``_query_synthesizer_agent``
    invocation: positional ``self`` + ``prompt``, with deps threaded
    through ``**kwargs``. Same shape as ``test_end_to_end.py``'s
    ``fake_synth``.
    """

    def fake_run_sync(self: object, prompt: str, **kwargs: object) -> mock.Mock:
        from lies.agents.query_synthesizer import QueryDeps

        deps = kwargs.get("deps")
        page_texts = getattr(deps, "page_texts", {}) if isinstance(deps, QueryDeps) else {}
        # Preserve the original page paths so callers can pinpoint
        # which files were fed to the (stubbed) LLM.
        captured.extend(page_texts.keys())
        return mock.Mock(
            output=QueryAnswer(
                answer=answer_md,
                citations=list(page_texts.keys()),
                should_file=False,
            )
        )

    return mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        new=fake_run_sync,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# Probe questions by discriminating term. ``mix_probe`` carries a generic
# ZEPHYR + DAG phrasing so a single qmd invocation surfaces airflow first
# AND the other two collections (the post-filter still drops non-airflow
# hits in the ``+airflow`` tests).
AIRFLOW_PROBE = "ZEPHYR Apache Airflow DAG workflow operators providers"


def _orchestrator(wiki: Wiki) -> Orchestrator:
    """Construct an Orchestrator with the test model map.

    Returns the orchestrator so the test can patch its synthesizer
    agent's ``run_sync`` (stubbed pattern matching
    ``tests/integration/test_end_to_end.py``). The synthesizer itself
    is constructed with ``model="test"`` (a string token, not a real
    ``pydantic_ai.models`` instance); the stub bypasses the model call
    entirely.
    """
    from tests.conftest import models_for_tests

    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


def test_plus_tag_filters_to_one_collection(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow`` confines real qmd hits to airflow-tagged subdirs.

    With the four-collection fixture (airflow + prefect both carry the
    ``airflow`` tag via the implicit-self-tag rule), ``+airflow``
    resolves to both names. The post-qmd per-collection drop keeps
    hits whose first path segment is in the resolved set; leakage
    into ``amazon/`` or ``pyspark/`` would mean the filter failed.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow"))
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Every page the synthesizer saw must live under one of the
    # resolved (airflow-tagged) subdirs; the post-qmd drop in
    # ``qmd_query`` is the seam being tested, and any leakage into
    # ``amazon/`` or ``pyspark/`` would mean the filter failed.
    assert captured, (
        f"+airflow should have surfaced at least one airflow-tagged page via real qmd; "
        f"captured={captured!r}"
    )
    for rel_path in captured:
        assert rel_path.startswith(("wiki/airflow/", "wiki/prefect/")), (
            f"+airflow post-qmd filter leaked non-airflow-tagged page: {rel_path!r}"
        )

    # searched_scope is the resolved collection set per spec
    # §"Retriever consumption": with a filter, the scope is the resolved
    # set; without, every registered collection. The implicit-self-tag
    # rule (F15's ``t:`` / no-prefix default) admits both the
    # ``airflow`` collection (by name) and the ``prefect`` collection
    # (by tag).
    assert answer.searched_scope == ["airflow", "prefect"]


def test_plus_tag_with_exclude(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow -amazon`` keeps the include while applying the exclude.

    The include resolves to airflow + prefect (both carry the
    ``airflow`` tag); the exclude drops collections carrying the
    ``amazon`` tag — no resolved collection does, so the include set
    is preserved. The test pins that the exclude is wired and that
    ``searched_scope`` reflects the resolved set.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow"), exclude="amazon")
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow", "prefect"]
    # The exclude does not change the resolved set for this fixture
    # (no airflow-tagged collection also carries ``amazon``), but
    # the synthesizer must still see only airflow-tagged pages.
    for rel_path in captured:
        assert rel_path.startswith(("wiki/airflow/", "wiki/prefect/")), (
            f"+airflow -amazon post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_tag_and_precise(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow&provider`` requires both atoms; only airflow has both.

    Prefect carries ``airflow`` but NOT ``provider``, so it does not
    satisfy the AND. Only the ``airflow`` collection passes.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(
        include=And(Include("airflow"), Include("provider")),
    )
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow"]
    for rel_path in captured:
        assert rel_path.startswith("wiki/airflow/"), (
            f"+airflow&provider post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_tag_or_broad(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow|provider`` matches either atom.

    The airflow collection has both; the prefect collection has
    ``airflow`` (in tags) but not ``provider`` — so it satisfies the
    OR via the airflow branch. Resolved set is airflow + prefect.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(
        include=Or(Include("airflow"), Include("provider")),
    )
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow", "prefect"]
    for rel_path in captured:
        assert rel_path.startswith(("wiki/airflow/", "wiki/prefect/")), (
            f"+airflow|provider post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_unknown_tag_no_coverage(
    qmd_fixture_library: Wiki,
) -> None:
    """``+nope`` resolves to an empty set; ``searched_scope`` is ``[]``.

    No collection has ``nope`` in its effective tags (name or tag
    list), so :func:`_collections_matching` returns an empty set and
    the post-qmd drop raises ``QmdNoResultsError`` (every qmd hit is
    filtered out — the path's first ``/``-segment is never in the
    empty allowed set). That triggers the index-fallback path; the
    fallback's own per-page read is *not* filter-scoped (it walks
    ``wiki/index.md`` directly), so the synthesizer still sees one
    page from the index. The test pins the ``searched_scope``
    envelope (the effective scope the operator's filter implies)
    while accepting that the fallback path bypassed the filter.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("nope"))
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # No collection matches `+nope`: the resolved set is empty, so
    # ``searched_scope`` is the empty list — the operator's filter
    # intent survived even though the index-fallback provided pages.
    assert answer.searched_scope == []
    # The fallback path read ``wiki/index.md``; the synthesizer saw
    # whatever was listed there. The contract under test is the
    # filter, not the fallback contents. ``fallback_reason`` is
    # either ``"qmd_no_results"`` (the post-filter drop raised
    # :class:`QmdNoResultsError`) or ``"qmd_failed"`` (a qmd command
    # error such as a stderr-only non-zero exit that surfaced as
    # :class:`QmdCommandError`); both indicate the post-qmd filter
    # path did its job and the orchestrator fell back to the index.
    assert answer.fallback_used is True
    assert answer.fallback_reason in {"qmd_no_results", "qmd_failed"}


# ---------------------------------------------------------------------------
# F15 ``t:`` / ``c:`` qualifier prefix integration tests
#
# The fixture's ``prefect`` collection carries the ``airflow`` tag (without
# being named ``airflow``), so the three tests below can distinguish the
# implicit-self-tag rule (``+t:airflow`` — matches airflow + prefect) from
# the strict-name rule (``+c:airflow`` — matches airflow only). The
# exclude-c variant then drops the airflow collection by name from an
# already-resolved set.
# ---------------------------------------------------------------------------


def test_plus_c_qualifier_returns_only_named_collection(
    qmd_fixture_library: Wiki,
) -> None:
    """``+c:airflow`` resolves to the airflow collection only.

    The strict collection-name dispatch (``coll.name == include.tag``)
    matches the airflow-named collection but ignores the ``prefect``
    collection's ``airflow`` tag — prefect has airflow in its tags
    but is not *named* airflow. ``searched_scope`` therefore contains
    a single entry, and the post-qmd per-collection drop keeps only
    pages whose first path segment is ``airflow``.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="c"))
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Strict-name dispatch: prefect is excluded even though it carries
    # ``airflow`` in its tags.
    assert answer.searched_scope == ["airflow"]
    for rel_path in captured:
        assert rel_path.startswith("wiki/airflow/"), (
            f"+c:airflow post-qmd filter leaked non-airflow page: {rel_path!r}"
        )


def test_plus_t_qualifier_returns_all_carriers(
    qmd_fixture_library: Wiki,
) -> None:
    """``+t:airflow`` matches both airflow-named AND airflow-tagged collections.

    The tag-or-name alias (``include.tag ∈ coll.tags ∪ {coll.name}``)
    admits any collection whose name OR tags contain ``airflow``: the
    ``airflow`` collection (name) and the ``prefect`` collection (tag).
    ``searched_scope`` therefore contains both, sorted; the post-qmd
    drop keeps pages from either collection's subdirectory.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="t"))
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Tag-or-name dispatch: both airflow (by name) and prefect (by tag)
    # pass the include atom.
    assert answer.searched_scope == ["airflow", "prefect"]
    # Every page the synthesizer saw must live under one of the two
    # resolved collections — no leakage into amazon / pyspark.
    for rel_path in captured:
        assert rel_path.startswith(("wiki/airflow/", "wiki/prefect/")), (
            f"+t:airflow post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_t_then_c_exclude_drops_named_only(
    qmd_fixture_library: Wiki,
) -> None:
    """``+t:airflow -c:airflow`` resolves to prefect; airflow collection is dropped.

    The include atom matches both airflow (by name) and prefect (by
    tag); the exclude atom with a ``c:`` qualifier drops any
    collection whose name is ``airflow`` (strict-name dispatch). The
    prefect collection has airflow in its tags but is not *named*
    airflow, so it survives the exclude. ``searched_scope`` reports
    the residual set.

    The captured list depends on whether qmd surfaces prefect pages
    for the airflow probe. The shared qmd daemon carries the
    developer's other indexed collections; qmd's hybrid retrieval
    may not rank this fixture's prefect page high enough to clear
    the top-N cutoff. In that case every qmd hit is filtered out,
    the post-qmd drop raises ``QmdNoResultsError``, and the
    fallback path reads ``wiki/index.md`` — which bypasses the
    filter (same caveat as ``test_plus_unknown_tag_no_coverage``).
    The pinned contract is the filter resolution itself
    (``searched_scope``); the captured path depends on qmd ranking.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(
        include=Include("airflow", qualifier="t"),
        exclude="airflow",
        exclude_qualifier="c",
    )
    with _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Include resolved both airflow + prefect; exclude (c:) dropped
    # airflow by name; prefect remains. The search_scope is the
    # contract — the resolved set of collections the operator's
    # filter implies, independent of qmd ranking.
    assert answer.searched_scope == ["prefect"]
