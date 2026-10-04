"""Integration tests for the tag-filter end-to-end path (Bundle C).

Drives the real ``qmd`` CLI against a fixture wiki of three collections
so the post-qmd per-collection drop
(:func:`lies.qmd.cli.qmd_query`) and the ``searched_scope`` envelope
are exercised as they actually run, not as a mock would.

The qmd path is not mocked — retrieval runs against a *throwaway
per-test* index at ``$XDG_CACHE_HOME/qmd/index.sqlite`` (redirected by
``_isolated_xdg`` to ``tmp_path/xdg/cache/qmd/``). The session-scoped
``_live_qmd_index_unchanged`` guard verifies the live index is
unchanged before/after, so a regression that points the fixture at
the live index trips it. The LLM is stubbed — ``TestModel`` calls
tools once with generated arguments, leaving ``captured`` empty and
making every ``for rel_path in captured`` loop vacuously true.

Gated on ``INTEGRATION=1``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.query_synthesizer import QueryAnswer
from lies.library.config_io import config_path_for, save_config
from lies.library.record import LibraryCollectionConfig as Collection
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
    qmd_collection_remove,
    qmd_embed,
)
from lies.qmd.integrity import (
    LiveIndexSnapshot,
    live_index_snapshot,
    snapshots_differ,
)
from lies.wiki.wiki import Wiki


def _save_collection(_wiki: Wiki, c: Collection) -> None:
    """Persist a library record to the library singleton.

    The fixture seeds a collection into the library singleton at
    ``<library>/collections/<slug>/config.yaml`` so the sync pipeline
    can resolve it. ``wiki`` is accepted for signature parity with
    the legacy wiki-yaml shim but is unused.
    """
    target = config_path_for(c.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_config(c, force=True)


pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="integration test; set INTEGRATION=1 to run",
)


_NOW = datetime(2026, 9, 10, tzinfo=UTC)


# Each fixture collection shares the word "ZEPHYR" so a probe that
# mentions "ZEPHYR" surfaces hits from all of them; the discriminating
# terms ("DAG" for airflow, "S3" for amazon, "RDD" for pyspark) are
# what ranks each hit high in qmd's hybrid retrieval.
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
# ``prefect``: a second collection whose effective tags contain
# ``airflow`` but whose name is NOT ``airflow``. The F15 ``t:`` /
# ``c:`` qualifier integration tests need both an ``airflow``-named
# collection AND a non-``airflow``-named collection that still
# carries the ``airflow`` tag, so ``+t:airflow`` resolves to multiple
# names while ``+c:airflow`` resolves to exactly one.
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

    The collections match the brief:

      - ``airflow``: tags ``[airflow, provider]``
      - ``amazon``:  tags ``[amazon, aws]``
      - ``pyspark``: tags ``[pyspark, spark]``
      - ``prefect``: tags ``[prefect, airflow]`` (name is not airflow
        but tags include airflow — distinguishes ``+t:airflow`` from
        ``+c:airflow``).
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

    for coll, pages in (
        ("airflow", AIRFLOW_PAGES),
        ("amazon", AMAZON_PAGES),
        ("pyspark", PYSPARK_PAGES),
        ("prefect", PREFECT_PAGES),
    ):
        (wiki_dir / coll).mkdir()
        for page_name, body in pages.items():
            (wiki_dir / coll / page_name).write_text(body, encoding="utf-8")

    tags_per = {
        "airflow": ("airflow", "provider"),
        "amazon": ("amazon", "aws"),
        "pyspark": ("pyspark", "spark"),
        "prefect": ("prefect", "airflow"),
    }
    for coll, tags in tags_per.items():
        _save_collection(
            wiki,
            Collection(
                name=coll,
                source=f"https://example.com/{coll}",
                tags=tags,
                scraper_cmd=None,
                doc_path=None,
                mapper_model=None,
                language="en",
                version="1",
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


#: Collections registered in qmd's throwaway per-test index. The
#: teardown's iteration set is derived from this tuple plus the
#: wiki-named collection.
FIXTURE_COLLECTIONS = ("airflow", "amazon", "pyspark", "prefect")


def _registered_by_this_fixture(wiki: Wiki) -> tuple[str, ...]:
    """The qmd collection names the fixture and reachable product code register.

    Single source of truth for both the seed loop and the teardown
    loop. The four :data:`FIXTURE_COLLECTIONS` are registered by
    :func:`_seed_qmd`; ``wiki_<name>`` is registered by
    ``Orchestrator.run_query`` via
    :func:`lies.wiki.layout.ensure_wiki_qmd_registered`.
    """
    return (*FIXTURE_COLLECTIONS, f"wiki_{wiki.name}")


def _live_qmd_index_path() -> Path | None:
    """Absolute path to qmd's live (host-default) index, if present.

    Bypasses XDG so the autouse ``_isolated_xdg`` redirect does not
    shadow it.
    """
    candidate = Path.home() / ".cache" / "qmd" / "index.sqlite"
    return candidate if candidate.exists() else None


def _live_index_snapshot() -> LiveIndexSnapshot | None:
    """Snapshot the live qmd index via :func:`lies.qmd.integrity.live_index_snapshot`.

    Returns ``None`` when no live index is present, which is the normal
    case on a CI runner. ``_live_qmd_index_path`` returns ``None`` for a
    host with no index, and passing that straight into
    ``live_index_snapshot`` reaches ``db.exists()`` on ``None``. The
    guard's whole contract is that it no-ops on such a host, so the
    check belongs here rather than only inside the library function.
    """
    live = _live_qmd_index_path()
    if live is None:
        return None
    return live_index_snapshot(live)


@pytest.fixture(scope="session", autouse=True)
def _live_qmd_index_unchanged(request: pytest.FixtureRequest) -> Iterator[None]:
    """Assert the live qmd index is unchanged before and after this file.

    "Unchanged" means the four aggregates from
    :class:`lies.qmd.integrity.LiveIndexSnapshot` are equal on both
    sides — not a byte comparison. The guard bypasses XDG and reads
    the live index directly; the fixture writes to a throwaway.
    """
    if os.environ.get("INTEGRATION") != "1":
        yield
        return
    before = _live_index_snapshot()
    yield
    after = _live_index_snapshot()
    changed, msg = snapshots_differ(before, after)
    if changed:
        pytest.fail(msg)


def _seed_qmd(wiki: Wiki) -> None:
    """Register each fixture collection with qmd and embed it.

    Uses absolute paths; ``qmd_collection_add_if_missing`` is
    idempotent. The subprocess inherits the redirected
    ``XDG_CACHE_HOME`` so registration and embedding both target
    the per-test throwaway.
    """
    if shutil.which("qmd") is None:
        raise QmdNotInstalledError("`qmd` not found on PATH")
    for coll in FIXTURE_COLLECTIONS:
        coll_path = (wiki.wiki_dir / coll).resolve()
        qmd_collection_add_if_missing(wiki.data_root, coll_path, coll)
        qmd_embed(wiki.data_root, coll, timeout=600)


def _unseed_qmd(wiki: Wiki) -> None:
    """Remove every collection :func:`_seed_qmd` registered, then assert it.

    Each test writes to its own throwaway index at
    ``tmp_path/xdg/cache/qmd/index.sqlite`` (pytest deletes it with
    ``tmp_path`` at teardown), so teardown is hygiene on the throwaway,
    not leak prevention on a shared index.
    """
    leftovers: list[str] = []
    for coll in _registered_by_this_fixture(wiki):
        try:
            qmd_collection_remove(wiki.data_root, coll)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask test failures
            leftovers.append(f"{coll}: removal failed ({type(exc).__name__}: {exc})")
            continue
        if coll in _registered_collections(wiki.data_root):
            leftovers.append(f"{coll}: still registered after removal")
    if leftovers:
        pytest.fail(
            "the fixture left collections registered in this test's own "
            "qmd index: " + "; ".join(leftovers) + ". That index is the "
            "per-test throwaway under tmp_path (qmd resolves it from "
            "XDG_CACHE_HOME), not the live one -- so this is hygiene "
            "within the run, not a leak into the operator's index, which "
            "the session-scoped _live_qmd_index_unchanged guard covers."
        )


def _registered_collections(cwd: Path) -> set[str]:
    """Collection names registered in the qmd index that ``cwd`` resolves to."""
    from lies.qmd.cli import _run

    result = _run(["collection", "list"], cwd=cwd, timeout=120)
    if result.returncode != 0:
        return set()
    return {
        line.split("(")[0].strip()
        for line in result.stdout.splitlines()
        if line.strip() and "(" in line
    }


@contextlib.contextmanager
def _seeded_qmd_context(wiki: Wiki) -> Iterator[Wiki]:
    """Seed qmd, yield, and unseed -- with the unseed covering the seed.

    The cleanup has to wrap the *seeding*, not just the yield.
    :func:`_seed_qmd` registers a collection and then embeds it, and
    the embed can raise: node-llama-cpp intermittently aborts on the
    CUDA VMM reservation (``cuMemAddressReserve`` ->
    ``CUDA error: out of memory`` -> ``ggml_abort``), which reaches
    the test as ``QmdError: qmd embed failed``. When it does, seeding
    never reaches a yield -- and a ``try/finally`` written around the
    yield alone never runs its ``finally``. That is how
    ``wiki_tag-filter-lib`` came to be registered in the operator's
    live index and stayed there.

    Ordering is the whole invariant, so it lives in one named context
    manager rather than spread across a fixture body.
    ``tests/unit/test_qmd_fixture_envelope.py`` drives it directly,
    which it could not do through a fixture.
    """
    try:
        _seed_qmd(wiki)
        yield wiki
    except BaseException as exc:
        # Cleanup runs on every path, and a cleanup that fails must not
        # replace the exception already in flight. A bare
        # ``finally: _unseed_qmd(...)`` does exactly that: ``_unseed``
        # calls ``pytest.fail`` when a collection is still registered,
        # and the reader loses "the embed aborted on the CUDA
        # reservation" in favour of a hygiene message about a
        # throwaway index. The cleanup outcome rides along as a note.
        try:
            _unseed_qmd(wiki)
        except BaseException as cleanup_exc:
            exc.add_note(f"qmd fixture cleanup also failed: {cleanup_exc!r}")
        raise
    else:
        # Clean run: a cleanup failure is the finding, so it surfaces.
        _unseed_qmd(wiki)


@pytest.fixture
def qmd_fixture_library(tmp_path: Path) -> Iterator[Wiki]:
    """A wiki with four tagged collections, registered and embedded with qmd.

    Yields, then tears the qmd registration down. The collections
    go into qmd's *throwaway per-test* index at
    ``$XDG_CACHE_HOME/qmd/index.sqlite`` (redirected by
    ``_isolated_xdg`` to ``tmp_path/xdg/cache/``).
    """
    if shutil.which("qmd") is None:
        pytest.skip("qmd not installed on PATH")
    # Real qmd daemon must be reachable: the fixture embeds into the
    # daemon's index and the tests assert that ``wiki_search`` surfaces
    # indexed hits. CI doesn't run a qmd daemon by default.
    from lies.qmd.health import qmd_daemon_reachable

    if not qmd_daemon_reachable("http://127.0.0.1:8181", timeout=0.5):
        pytest.skip("qmd daemon not reachable at http://127.0.0.1:8181")
    wiki = _build_tag_filter_library(tmp_path, name="tag-filter-lib")
    with _seeded_qmd_context(wiki):
        yield wiki


#: Collection names the librarian's real qmd search spanned.
#: Module-level because the assertion reads it after the patch context
#: has exited.
_librarian_queried_collections: set[str] = set()


def _collection_of(rel_path: str) -> str:
    """The fixture collection a returned path belongs to.

    A path from a real qmd query is
    ``<qmd collection>/<collection>/<page>`` — the leading segment is
    the qmd *collection* (this wiki, registered as ``wiki_<name>``),
    and the second is the fixture collection the tag filter
    addresses. If the first segment is the wiki collection, the
    fixture collection is the second.
    """
    segments = rel_path.split("/")
    if len(segments) >= 2 and segments[0].startswith("wiki_"):
        return segments[1]
    return segments[0] if segments else ""


def _patched_librarian(orch: Orchestrator) -> mock._patch:
    """Replace the librarian agent with one that retrieves for real.

    The librarian runs on a pydantic-ai ``TestModel``, which calls
    each tool once with generated arguments. What this substitutes
    is only the *LLM*; retrieval stays real and uses the production
    :func:`lies.qmd.cli.qmd_query` with the filter under test.

    Patched on the **instance**, not on ``type(agent)``: the librarian
    and the synthesizer are both ``pydantic_ai.Agent``, so a
    class-level patch of ``run_sync`` would be shared by both.
    """
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import parse_spans
    from lies.qmd.cli import qmd_query
    from lies.query.synthesizer import _collections_matching

    _librarian_queried_collections.clear()
    wiki_dir = orch.wiki.wiki_dir

    def fake_run_sync(prompt: str, **kwargs: object) -> mock.Mock:
        deps = kwargs.get("deps")
        question = getattr(deps, "question", prompt)

        # Unscoped pass — recorded so the test can assert the filter
        # had something to remove.
        unscoped = qmd_query(orch.wiki.data_root, str(question), limit=10)
        _librarian_queried_collections.update(
            _collection_of(str(h.get("path", ""))) for h in unscoped
        )

        # Real filtered pass: production ``_collections_matching`` +
        # production ``qmd_query`` apply the resolved filter.
        resolved = _collections_matching(
            ResolvedTagFilter(
                include=getattr(deps, "tag_expr", None),
                exclude=getattr(deps, "exclude_expr", None),
            )
        )
        hits = (
            unscoped
            if not resolved
            else qmd_query(
                orch.wiki.data_root,
                str(question),
                limit=10,
                collection_filter=resolved,
            )
        )

        excerpts = []
        for hit in hits:
            rel = str(hit.get("path", ""))
            collection, _, page = rel.partition("/")
            on_disk = wiki_dir / collection / page
            text = (
                on_disk.read_text(encoding="utf-8")
                if on_disk.exists()
                else str(hit.get("snippet", ""))
            )
            excerpts.append(
                PageExcerpt(
                    collection=collection,
                    slug=rel,
                    title=str(hit.get("title", page)),
                    spans=parse_spans(text),
                    source_kind="wiki",
                )
            )
        return mock.Mock(
            output=LibrarianOutput(
                tag_expr=None,
                exclude_expr=None,
                excerpts=excerpts,
                distinct_pages=len(excerpts),
                no_coverage=not excerpts,
                searched_scope=sorted({e.collection for e in excerpts}),
            )
        )

    return mock.patch.object(orch._librarian_agent, "run_sync", new=fake_run_sync)


def _patched_synthesizer(
    orch: Orchestrator,
    *,
    captured: list[str],
    answer_md: str = "stub answer",
) -> mock._patch:
    """Stub ``orch._query_synthesizer_agent.run_sync``.

    Captures the page paths the synthesizer saw into ``captured`` and
    returns a deterministic ``QueryAnswer`` that cites them. Tests
    assert against ``captured`` (real retrieved pages).
    """

    def fake_run_sync(self: object, prompt: str, **kwargs: object) -> mock.Mock:
        from lies.agents.query_synthesizer import QueryDeps

        deps = kwargs.get("deps")
        page_texts = getattr(deps, "page_texts", {}) if isinstance(deps, QueryDeps) else {}
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


# AIRFLOW_PROBE carries generic ZEPHYR + DAG phrasing so a single
# qmd invocation surfaces airflow first AND the other collections
# (the post-filter still drops non-airflow hits in the ``+airflow`` tests).
AIRFLOW_PROBE = "ZEPHYR Apache Airflow DAG workflow operators providers"


def _orchestrator(wiki: Wiki) -> Orchestrator:
    """Construct an Orchestrator with the test model map.

    Returns the orchestrator so the test can patch its synthesizer
    agent's ``run_sync`` (matching the pattern in
    ``tests/integration/test_end_to_end.py``).
    """
    from tests.conftest import models_for_tests

    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="requires live qmd daemon with indexed content; CI skips",
)
def test_plus_tag_filters_to_one_collection(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow`` confines real qmd hits to airflow-tagged subdirs.

    With the four-collection fixture (airflow + prefect both carry
    the ``airflow`` tag via the implicit-self-tag rule), ``+airflow``
    resolves to both names. The post-qmd per-collection drop keeps
    hits whose first path segment is in the resolved set.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow"))

    from lies.query.synthesizer import _collections_matching

    resolved = _collections_matching(tf)

    # Floor: ``+airflow`` (no qualifier ⇒ implicit self-tag) must
    # admit ``airflow`` by name AND ``prefect`` by tag.
    assert resolved == {"airflow", "prefect"}, (
        f"+airflow must resolve to airflow and prefect (prefect is tagged "
        f"airflow); got {sorted(resolved)!r}"
    )
    assert not (resolved & {"amazon", "pyspark"}), (
        f"+airflow must not resolve to amazon or pyspark; got {sorted(resolved)!r}"
    )

    with (
        _patched_librarian(orch),
        _patched_synthesizer(orch, captured=captured),
    ):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # The librarian resolves the collection set itself, by calling
    # the production ``qmd_query`` with the filter under test — the
    # seam this test is named for. It is NOT handed ``resolved``
    # above: a stub given the answer would satisfy the leakage check
    # trivially.
    assert captured, (
        f"+airflow should have surfaced at least one airflow-tagged page via real qmd; "
        f"captured={captured!r}"
    )
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
            f"+airflow post-qmd filter leaked non-airflow-tagged page: {rel_path!r}"
        )

    # The librarian really did see every collection, so the filter
    # had something to remove.
    assert _librarian_queried_collections == {"airflow", "amazon", "pyspark", "prefect"}, (
        f"the librarian must search across every collection so the filter "
        f"has something to exclude; it searched {_librarian_queried_collections!r}"
    )

    # searched_scope is the resolved collection set: with a filter,
    # the scope is the resolved set; without, every registered
    # collection.
    assert answer.searched_scope == ["airflow", "prefect"]


def test_plus_tag_with_exclude(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow -amazon`` keeps the include while applying the exclude.

    The include resolves to airflow + prefect; the exclude drops
    collections carrying the ``amazon`` tag — no resolved collection
    does, so the include set is preserved.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow"), exclude=Include("amazon"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow", "prefect"]
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
            f"+airflow -amazon post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_tag_and_precise(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow&provider`` requires both atoms; only airflow has both.

    Prefect carries ``airflow`` but NOT ``provider``.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(
        include=And(Include("airflow"), Include("provider")),
    )
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow"]
    for rel_path in captured:
        assert _collection_of(rel_path) == "airflow", (
            f"+airflow&provider post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_tag_or_broad(
    qmd_fixture_library: Wiki,
) -> None:
    """``+airflow|provider`` matches either atom.

    The airflow collection has both; prefect has ``airflow`` in tags.
    Resolved set is airflow + prefect.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(
        include=Or(Include("airflow"), Include("provider")),
    )
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow", "prefect"]
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
            f"+airflow|provider post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_unknown_tag_no_coverage(
    qmd_fixture_library: Wiki,
) -> None:
    """``+nope`` resolves to an empty set; ``searched_scope`` is ``[]``.

    No collection has ``nope`` in its effective tags. The librarian's
    filtered pass is skipped (an empty ``collection_filter`` would
    drop every hit), and the orchestrator takes its index fallback.

    The mechanism is the *stub's*, not ``qmd_query``'s.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("nope"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # No collection matches `+nope`: the resolved set is empty, so
    # ``searched_scope`` is the empty list.
    assert answer.searched_scope == []
    # The fallback path read ``wiki/index.md``; the contract under
    # test is the filter, not the fallback contents.
    assert answer.fallback_used is True
    assert answer.fallback_reason in {"qmd_no_results", "qmd_failed"}


# F15 ``t:`` / ``c:`` qualifier prefix integration tests. The
# ``prefect`` collection carries the ``airflow`` tag (without being
# named ``airflow``), so the three tests below can distinguish
# implicit-self-tag (``+t:airflow`` matches airflow + prefect) from
# strict-name (``+c:airflow`` matches airflow only).


def test_plus_c_qualifier_returns_only_named_collection(
    qmd_fixture_library: Wiki,
) -> None:
    """``+c:airflow`` resolves to the airflow collection only.

    Strict collection-name dispatch (``coll.name == include.tag``):
    prefect is excluded even though it carries ``airflow`` in its tags.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="c"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow"]
    for rel_path in captured:
        assert _collection_of(rel_path) == "airflow", (
            f"+c:airflow post-qmd filter leaked non-airflow page: {rel_path!r}"
        )


def test_plus_t_qualifier_returns_all_carriers(
    qmd_fixture_library: Wiki,
) -> None:
    """``+t:airflow`` matches both airflow-named AND airflow-tagged collections.

    The tag-or-name alias (``include.tag ∈ coll.tags ∪ {coll.name}``)
    admits any collection whose name OR tags contain ``airflow``:
    the ``airflow`` collection (name) and ``prefect`` (tag).
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="t"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Tag-or-name dispatch: both airflow (by name) and prefect (by tag)
    # pass the include atom.
    assert answer.searched_scope == ["airflow", "prefect"]
    # Every page the synthesizer saw must live under one of the two
    # resolved collections — no leakage into amazon / pyspark.
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
            f"+t:airflow post-qmd filter leaked: {rel_path!r}"
        )


def test_plus_t_then_c_exclude_drops_named_only(
    qmd_fixture_library: Wiki,
) -> None:
    """``+t:airflow -c:airflow`` resolves to prefect; airflow collection is dropped.

    The include atom matches both airflow (by name) and prefect (by
    tag); the exclude atom with a ``c:`` qualifier drops any
    collection whose name is ``airflow``. The prefect collection has
    airflow in its tags but is not *named* airflow, so it survives.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    # Task 3 / f15-exclude-compound: ``ResolvedTagFilter.exclude`` is
    # now a ``TagExpr | None`` AST (not a flat string + qualifier).
    # The ``c:`` qualifier lives on the ``Include`` atom itself, so
    # the same single-atom exclude that the pre-Task-3
    # ``exclude="airflow", exclude_qualifier="c"`` shape captured is
    # now an ``Include("airflow", qualifier="c")`` AST.
    tf = ResolvedTagFilter(
        include=Include("airflow", qualifier="t"),
        exclude=Include("airflow", qualifier="c"),
    )
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Include resolved both airflow + prefect; exclude (c:) dropped
    # airflow by name; prefect remains. The search_scope is the
    # contract — the resolved set of collections the operator's
    # filter implies, independent of qmd ranking.
    assert answer.searched_scope == ["prefect"]
