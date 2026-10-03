"""Integration tests for the tag-filter end-to-end path (Bundle C).

Drives the real ``qmd`` CLI against a fixture library of three collections
so the post-qmd per-collection drop
(:func:`lies.qmd.cli.qmd_query`) and the ``searched_scope`` envelope
(:func:`lies.query.synthesizer._searched_scope`) are exercised as they
actually run, not as a mock would.

What is real and what is stubbed: **the qmd path is not mocked.** The
librarian's retrieval here is the production
:func:`lies.qmd.cli.qmd_query` running against a *throwaway per-test*
index at ``$XDG_CACHE_HOME/qmd/index.sqlite`` (which the autouse
``_isolated_xdg`` redirects to ``tmp_path/xdg/cache/qmd/``), so the
per-collection drop under test is the real one. The session-scoped
``_live_qmd_index_unchanged`` guard verifies the live index at
``~/.cache/qmd/index.sqlite`` is byte-identical before and after the
run, so a regression that points the fixture at the live index trips
the guard. What *is* stubbed is the LLM — at two seams,
``_patched_librarian`` (which decides which paths to read, and does so
by asking that same real ``qmd_query``) and ``_patched_synthesizer``
(which turns excerpts into prose). A ``TestModel`` cannot stand in for
either: it calls each tool once with *generated* arguments, so the
librarian called ``read(["a"])`` and every page was skipped as
unrecognised — which left ``captured`` empty and made every
``for rel_path in captured`` loop in this file vacuously true.
Substituting the LLM is what makes retrieval assertions possible at
all; it is not the same as mocking the thing under test.

Tests are gated on ``INTEGRATION=1``; default CI skips them via
``pytest.mark.skipif``. The integration workflow in
``.github/workflows/`` runs with ``INTEGRATION=1`` enabled.
"""

from __future__ import annotations

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
from lies.qmd.integrity import live_index_snapshot, snapshots_differ
from lies.wiki.wiki import Wiki


def _save_collection(_wiki: Wiki, c: Collection) -> None:
    """Adapter shim: persist a library record to the library singleton.

    The test fixture seeds a collection into the library singleton at
    ``<library>/collections/<slug>/config.yaml`` so the sync pipeline can
    resolve it. ``wiki`` is accepted for signature parity with the legacy
    wiki-yaml shim but is unused: the library record carries its own
    slug and writes through ``config_path_for(slug)``.
    """
    target = config_path_for(c.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_config(c, force=True)


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


#: The collections this fixture registers in qmd's throwaway per-test
#: index. The teardown's iteration set is derived from this tuple plus
#: the wiki-named collection (see :func:`_registered_by_this_fixture`),
#: so a collection added here is removed by teardown without anyone
#: having to remember the second site.
FIXTURE_COLLECTIONS = ("airflow", "amazon", "pyspark", "prefect")


def _registered_by_this_fixture(wiki: Wiki) -> tuple[str, ...]:
    """The qmd collection names the fixture and reachable product code register.

    Single source of truth for both the seed loop and the teardown loop,
    so a future change that adds a collection cannot add to one without
    the other. The four :data:`FIXTURE_COLLECTIONS` are registered by
    :func:`_seed_qmd`; the ``wiki_<name>`` collection is registered by
    the product's :func:`lies.wiki.layout.ensure_wiki_qmd_registered`,
    which ``Orchestrator.run_query`` calls on every MCP query path. The
    fixture does not invoke that helper directly, but the test's
    orchestrator does on every ``run_query`` call, and the teardown's
    job is hygiene on the throwaway the test writes to -- which a
    wiki-named collection counts as, whether the fixture wrote it or
    a reachable product path did.

    The unit tests
    ``test_registered_by_this_fixture_includes_the_wiki_collection`` and
    ``test_unseed_qmd_iterates_the_fixture_owned_set`` pin the union:
    a regression that iterates only :data:`FIXTURE_COLLECTIONS` would
    leave the wiki collection behind and the second test fails with
    a precise diff.
    """
    return (*FIXTURE_COLLECTIONS, f"wiki_{wiki.name}")


def _live_qmd_index_path() -> Path | None:
    """Absolute path to qmd's live (host-default) index, if present.

    Bypasses XDG so the autouse ``_isolated_xdg`` redirect does not
    shadow it. qmd's only well-known cache layout is
    ``$XDG_CACHE_HOME/qmd/index.sqlite``; ``~/.cache`` is the
    ``XDG_CACHE_HOME`` default on this host, and the spec's global
    constraint scopes all read-only diagnostics to this absolute path.
    Returns ``None`` when no live index is reachable (e.g. on CI).
    """
    candidate = Path.home() / ".cache" / "qmd" / "index.sqlite"
    return candidate if candidate.exists() else None


def _live_index_snapshot():
    """Snapshot the live qmd index via :func:`lies.qmd.integrity.live_index_snapshot`.

    Thin wrapper that resolves the live path under ``~/.cache`` (the
    XDG default the daemon serves against) and returns the four-field
    snapshot the comparator :func:`lies.qmd.integrity.snapshots_differ`
    diffs. Returns ``None`` if no live index is present.

    The pre-fix guard snapshot only captured two aggregates
    (collection names, active document count). The four live-index
    orphans on 2026-10-03 belong to the exact defect class that two-
    field snapshot could not see — ``content_vectors`` rows whose
    backing ``content`` and ``documents`` rows never landed — and the
    guard therefore passed silently while the writes happened. The
    :class:`LiveIndexSnapshot` adds ``total_vectors`` and
    ``orphan_vectors``, both of which move on that write. The unit
    tests in ``tests/unit/qmd/test_integrity.py`` pin each field's
    discriminating power against a throwaway index.
    """
    return live_index_snapshot(_live_qmd_index_path())


@pytest.fixture(scope="session", autouse=True)
def _live_qmd_index_unchanged(request: pytest.FixtureRequest) -> Iterator[None]:
    """Assert the live qmd index is unchanged before and after this file.

    "Unchanged" means the four aggregates from
    :class:`lies.qmd.integrity.LiveIndexSnapshot` are equal on both
    sides — not a byte comparison. Two runs that embed nothing against
    the live index produce byte-identical files anyway; the aggregates
    are what a leak would actually move, and comparing bytes would be
    a claim this does not make.

    The fixture's qmd subprocesses inherit the redirected
    ``XDG_CACHE_HOME`` from ``_isolated_xdg`` and write to
    ``tmp_path/xdg/cache/qmd/index.sqlite`` — a throwaway. The guard
    bypasses XDG and reads ``~/.cache/qmd/index.sqlite`` directly,
    capturing the snapshot at session start and asserting equality at
    session end. A regression that points the fixture at the live
    index (the exact class of failure this branch exists to detect)
    trips this assertion with a precise diff that names which
    aggregate moved.

    The four aggregates are the load-bearing ones for the defects
    observed on this branch:

    - ``collection_names`` — ``qmd collection add`` against the live
      index, or ``syncConfigToDb``-driven reconciliation.
    - ``active_doc_count`` — ``qmd update`` against the live index.
    - ``total_vectors`` — any write that touches ``content_vectors``,
      including an orphan write whose ``content`` / ``documents``
      rows never landed.
    - ``orphan_vectors`` — the pre-fix blind spot. The four live
      orphans on 2026-10-03 are ``content_vectors`` rows whose hash
      is not in ``content`` and not in ``documents``. The pre-fix
      guard could not detect them; this one does.

    The guard never writes to the live index. Read-only ``sqlite3``
    via :func:`lies.qmd.integrity.open_readonly` is the only I/O. Fires
    only under ``INTEGRATION=1`` (the same gate that collects this
    file) and only on hosts with a live index. No-ops otherwise so a
    CI sandbox with no host index is not blocked.
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

    Uses absolute paths so qmd stores absolute paths in the index —
    transient test tmp paths must not depend on the cwd at call time.
    ``qmd_collection_add_if_missing`` is idempotent on re-runs (its
    stderr check ignores "already exists").

    The subprocess inherits the redirected ``XDG_CACHE_HOME`` from
    ``_isolated_xdg``, so registration and embedding both target
    ``tmp_path/xdg/cache/qmd/index.sqlite`` (a throwaway), not
    ``~/.cache/qmd/index.sqlite``. The session-scoped
    ``_live_qmd_index_unchanged`` guard pins that invariant.

    Raises ``QmdNotInstalledError`` if qmd is missing; the per-test
    fixture-level ``skipif`` on ``shutil.which('qmd')`` usually catches
    this first, but explicit propagation is cheaper than a stack trace.
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
    ``tmp_path/xdg/cache/qmd/index.sqlite`` (because ``_isolated_xdg``
    redirects ``XDG_CACHE_HOME``), and pytest deletes that path with
    ``tmp_path`` at teardown. So the teardown's purpose is hygiene on
    the throwaway, not leak prevention on a shared index. The
    session-scoped ``_live_qmd_index_unchanged`` guard pins the
    live-index invariant independently.

    Within a single test run the teardown is still load-bearing: an
    earlier test's seeded data must not survive to a later test that
    shares the same XDG root. (Pytest's per-test ``tmp_path`` normally
    gives every test a fresh root, but tests that run together in the
    same process share the autouse fixture, and a misconfigured
    session-scoped ``tmp_path`` is the exact class of regression this
    fixture's teardown makes loud.)

    The post-condition is asserted, not assumed: a leftover registration
    here is what made the next test's retrieval silent, so it fails
    loudly here rather than three tests later as a no-results assertion.
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
    """Collection names registered in the qmd index that ``cwd`` resolves to.

    Takes the cwd explicitly and the caller passes the *same* ``wiki.data_root``
    that seeding and removal use. Reading ``Path.cwd()`` here instead would
    query a different root than the one being written to, and the check
    would silently pass on an empty answer -- a safety check that can
    never fail is worse than none, because it reads as one.
    """
    from lies.qmd.cli import _run

    result = _run(["collection", "list"], cwd=cwd, timeout=120)
    if result.returncode != 0:
        return set()
    return {
        line.split("(")[0].strip()
        for line in result.stdout.splitlines()
        if line.strip() and "(" in line
    }


@pytest.fixture
def qmd_fixture_library(tmp_path: Path) -> Iterator[Wiki]:
    """A wiki with four tagged collections, registered and embedded with qmd.

    **Yields, and tears the qmd registration down.** The collections go
    into qmd's *throwaway per-test* index at
    ``$XDG_CACHE_HOME/qmd/index.sqlite`` — which the autouse
    ``_isolated_xdg`` redirects to ``tmp_path/xdg/cache/``. pytest
    deletes ``tmp_path`` at teardown, taking the throwaway with it.
    The teardown's ``qmd collection remove`` is therefore hygiene on
    the throwaway, not a leak fix on a shared index; the
    session-scoped ``_live_qmd_index_unchanged`` guard pins the
    live-index invariant independently.

    The teardown also *asserts* the collections are gone, so a recurrence
    fails here rather than three tests later as somebody else's timeout.


    Function-scoped, and deliberately so. The wiki is built under the
    per-test ``tmp_path`` because ``_isolated_xdg`` redirects the XDG
    roots per test, and ``Wiki`` construction reads them from the
    environment — a session-scoped fixture built the wiki before that
    redirect existed for it and every test then errored at setup with
    ``KeyError: 'XDG_CONFIG_HOME'``.

    That means four embed cycles per test. It is affordable as long as
    they are sequential, which they are (``QMD_EMBED_PARALLELISM=1`` and
    a lock around each call). A CUDA OOM here means two suites were
    embedding concurrently, not that the fixture is wrong. The model
    reload cost is the trade for full per-test isolation; a
    session-scoped embed would share the warm model across tests but
    cannot share the XDG root without losing the per-test wiki.
    """
    if shutil.which("qmd") is None:
        pytest.skip("qmd not installed on PATH")
    # Real qmd daemon must be reachable: the fixture embeds collections
    # into the daemon's global index and the tests assert that
    # ``wiki_search`` surfaces indexed hits. CI doesn't run a qmd daemon
    # by default; skip rather than fail.
    from lies.qmd.health import qmd_daemon_reachable

    if not qmd_daemon_reachable("http://127.0.0.1:8181", timeout=0.5):
        pytest.skip("qmd daemon not reachable at http://127.0.0.1:8181")
    wiki = _build_tag_filter_library(tmp_path, name="tag-filter-lib")
    _seed_qmd(wiki)
    try:
        yield wiki
    finally:
        _unseed_qmd(wiki)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


#: The collection names the librarian's real qmd search spanned, recorded
#: by ``_patched_librarian``. A module-level record because the assertion
#: that reads it runs *after* the patch context has exited.
_librarian_queried_collections: set[str] = set()


def _collection_of(rel_path: str) -> str:
    """The fixture collection a returned path belongs to.

    A path from a real qmd query is ``<qmd collection>/<collection>/<page>``
    — the leading segment is the qmd *collection* (this wiki, registered as
    ``wiki_<name>``), and the second is the fixture collection the tag
    filter addresses. Comparing the leading segment, as these tests did
    before the librarian started doing real retrieval, asserted against
    the wiki's own name rather than the thing under test.

    Returning the last-but-one segment would be wrong for nested paths, so
    this strips exactly one known prefix: if the first segment is the
    wiki collection, the fixture collection is the second.
    """
    segments = rel_path.split("/")
    if len(segments) >= 2 and segments[0].startswith("wiki_"):
        return segments[1]
    return segments[0] if segments else ""


def _patched_librarian(orch: Orchestrator) -> mock._patch:
    """Replace the librarian agent with one that retrieves for real.

    The librarian runs on a pydantic-ai ``TestModel`` (the orchestrator is
    built with ``models_for_tests("test")``). A ``TestModel`` does not
    *decide* anything — it calls each tool once with generated arguments,
    so it called ``read(["a"])``, every path was skipped as unrecognised,
    and the synthesizer received zero excerpts. Since #78 the librarian
    has to ``search()`` and then read the paths that come back; a
    ``TestModel`` cannot perform that sequence, so any test asserting on
    retrieved pages must supply it.

    What this substitutes is only the *LLM* — the part no test can assert
    on deterministically. Retrieval stays real: the stub calls the
    production :func:`lies.qmd.cli.qmd_query` with the filter under test
    and builds excerpts from the pages it returns, so the post-qmd
    per-collection drop — the seam this test exists to exercise — runs
    unmodified against the live index.

    The stub searches *every* fixture collection with no filter, exactly
    as a librarian that had not yet applied the tag would. The filter
    under test is then what narrows the result, which is the whole point:
    a stub handed the resolved set would make "nothing leaked" true by
    construction. ``_librarian_queried_collections`` records what was
    searched so the test can prove the filter had something to remove.

    Patched on the **instance**, not on ``type(agent)``: the librarian and
    the synthesizer are both ``pydantic_ai.Agent``, so a class-level
    patch of ``run_sync`` is shared by both and the synthesizer's stub
    would serve the librarian too, handing back a canned ``QueryAnswer``
    where a ``LibrarianOutput`` is expected.
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

        # First, an unscoped pass across every collection — recorded, so
        # the test can assert the filter had something to remove. This is
        # what a librarian that had not yet applied the tag would see.
        unscoped = qmd_query(orch.wiki.data_root, str(question), limit=10)
        _librarian_queried_collections.update(
            _collection_of(str(h.get("path", ""))) for h in unscoped
        )

        # Then the real filtered pass: the production
        # ``_collections_matching`` resolves the tag expression under
        # test, and the production ``qmd_query`` applies it as
        # ``collection_filter``. Both are the article under test, so
        # neither may be stubbed — a stub handed the resolved set would
        # make "nothing leaked" true by construction.
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


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="requires live qmd daemon with indexed content; CI skips",
)
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

    from lies.query.synthesizer import _collections_matching

    resolved = _collections_matching(tf)

    # The floor. ``+airflow`` (no qualifier ⇒ implicit self-tag) must
    # admit the ``airflow`` collection by name AND ``prefect`` by tag.
    # Without this, everything below is circular: if the tag expression
    # stopped selecting these two, the scope assertion would fail for a
    # reason that has nothing to do with retrieval, and the librarian
    # below would be handed an empty set and return nothing at all.
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

    # The librarian resolves the collection set itself, by calling the
    # production ``qmd_query`` with the filter under test — the seam this
    # test is named for. It is NOT handed ``resolved`` above: if the stub
    # were given the answer, "no non-airflow page leaked" would be true by
    # construction and could not fail.
    #
    # What it asserts is that the filtered qmd pass returned only pages
    # whose first path segment is in the resolved set. That is the
    # post-qmd per-collection drop in ``qmd_query``, running for real.
    assert captured, (
        f"+airflow should have surfaced at least one airflow-tagged page via real qmd; "
        f"captured={captured!r}"
    )
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
            f"+airflow post-qmd filter leaked non-airflow-tagged page: {rel_path!r}"
        )

    # And the librarian really did see every collection, so the filter
    # had something to remove. Without this, a stub that quietly searched
    # only the resolved set would satisfy the loop above vacuously.
    assert _librarian_queried_collections == {"airflow", "amazon", "pyspark", "prefect"}, (
        f"the librarian must search across every collection so the filter "
        f"has something to exclude; it searched {_librarian_queried_collections!r}"
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
    # Task 3 / f15-exclude-compound: ``exclude`` is now an ``Include``
    # AST (no more flat-string + qualifier shim).
    tf = ResolvedTagFilter(include=Include("airflow"), exclude=Include("amazon"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    assert answer.searched_scope == ["airflow", "prefect"]
    # The exclude does not change the resolved set for this fixture
    # (no airflow-tagged collection also carries ``amazon``), but
    # the synthesizer must still see only airflow-tagged pages.
    for rel_path in captured:
        assert _collection_of(rel_path) in {"airflow", "prefect"}, (
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

    The airflow collection has both; the prefect collection has
    ``airflow`` (in tags) but not ``provider`` — so it satisfies the
    OR via the airflow branch. Resolved set is airflow + prefect.
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

    No collection has ``nope`` in its effective tags (name or tag
    list), so :func:`_collections_matching` returns an empty set and
    the resolved collection set is empty, so the librarian's filtered
    pass is skipped (an empty ``collection_filter`` would drop every
    hit) and the orchestrator takes its index fallback, whose own
    per-page read walks ``wiki/index.md`` directly and is therefore
    not filter-scoped. The synthesizer still sees a page from the
    index. The test pins the ``searched_scope`` envelope — the
    effective scope the operator's filter implies — while accepting
    that the fallback bypassed the filter to produce content.

    Note the mechanism is the *stub's*, not ``qmd_query``'s: the
    librarian here short-circuits on an empty resolved set rather than
    issuing a filtered query that would raise ``QmdNoResultsError``.
    A docstring that attributed this to the post-qmd drop would be
    describing a path this file no longer takes.
    """
    orch = _orchestrator(qmd_fixture_library)
    captured: list[str] = []
    tf = ResolvedTagFilter(include=Include("nope"))
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # No collection matches `+nope`: the resolved set is empty, so
    # ``searched_scope`` is the empty list — the operator's filter
    # intent survived even though the index-fallback provided pages.
    assert answer.searched_scope == []
    # The fallback path read ``wiki/index.md``; the synthesizer saw
    # whatever was listed there. The contract under test is the
    # filter, not the fallback contents. ``fallback_reason`` is
    # either ``"qmd_no_results"`` (nothing survived retrieval) or
    # ``"qmd_failed"`` (a qmd command error surfaced as
    # :class:`QmdCommandError`); both mean the same thing to a reader
    # here — the filter left nothing to answer from, and the
    # orchestrator fell back to the index.
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
    with _patched_librarian(orch), _patched_synthesizer(orch, captured=captured):
        answer = orch.run_query(AIRFLOW_PROBE, tag_filter=tf, file=False)

    # Strict-name dispatch: prefect is excluded even though it carries
    # ``airflow`` in its tags.
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
    admits any collection whose name OR tags contain ``airflow``: the
    ``airflow`` collection (name) and the ``prefect`` collection (tag).
    ``searched_scope`` therefore contains both, sorted; the post-qmd
    drop keeps pages from either collection's subdirectory.
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
