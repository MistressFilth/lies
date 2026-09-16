"""Unit tests for the retrieve_pages seam lifted out of synthesize_answer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lies.qmd.cli import QmdCommandError, QmdNoResultsError, QmdNotInstalledError
from lies.query.citation import Citation
from lies.query.synthesizer import (
    FALLBACK_REASON_FAILED,
    FALLBACK_REASON_NO_RESULTS,
    FALLBACK_REASON_UNAVAILABLE,
    FALLBACK_REASON_WIKI_ONLY,
    _collections_matching,
    retrieve_pages,
    synthesize_answer,
)
from lies.query.tag_expr import (
    And,
    Include,
    Or,
    ResolvedTagFilter,
)
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


_NOW = datetime(2026, 9, 9, tzinfo=UTC)


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n", encoding="utf-8"
    )
    (root / "wiki" / "index.md").write_text(
        "# Index\n\n## concepts\n\n- [Alpha](concepts/alpha.md) — `alpha`\n",
        encoding="utf-8",
    )
    return make_wiki(name="retrieve", data_root=root)


def test_retrieve_pages_returns_qmd_hits_with_empty_reason(wiki: Wiki) -> None:
    def fake_search(
        *_args: object, collection_filter=None, **_kwargs: object
    ) -> list[dict[str, object]]:
        # The wiki pass uses the wiki_<name> collection; return [] so it
        # raises _QmdNoResults and contributes no pages. The library pass
        # (no filter) returns the wiki hit — which only resolves under
        # ``wiki.wiki_dir``, not the library collections root.
        if collection_filter and "wiki_retrieve" in collection_filter:
            return []
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == ""
    assert [p.rel_path for p in pages] == ["wiki/concepts/alpha.md"]


def test_retrieve_pages_reports_qmd_unavailable_when_both_passes_fail(wiki: Wiki) -> None:
    """Both passes raise QmdNotInstalledError; the retriever reports the
    failure reason and returns empty pages (the wiki/index.md fallback
    was retired with the two-pass refactor — the qmd story alone owns
    retrieval now)."""

    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QmdNotInstalledError("qmd not on PATH")

    pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == FALLBACK_REASON_UNAVAILABLE
    assert pages == []


def test_retrieve_pages_falls_back_when_qmd_has_no_results(wiki: Wiki) -> None:
    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QmdNoResultsError("nothing matched")

    _pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == FALLBACK_REASON_NO_RESULTS


def test_retrieve_pages_falls_back_on_other_qmd_failure(wiki: Wiki) -> None:
    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QmdCommandError("qmd exited 1")

    _pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == FALLBACK_REASON_FAILED


def test_retrieve_pages_returns_empty_list_when_nothing_readable(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    (root / "wiki").mkdir(parents=True)
    bare = make_wiki(name="bare", data_root=root)

    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QmdNotInstalledError("qmd not on PATH")

    pages, reason = retrieve_pages("anything?", bare, qmd_search=fake_search)

    assert pages == []
    assert reason == FALLBACK_REASON_UNAVAILABLE


def test_synthesize_answer_output_unchanged_by_the_lift(wiki: Wiki) -> None:
    """Characterization: the refactor must not move synthesize_answer's output."""

    def fake_search(
        *_args: object, collection_filter=None, **_kwargs: object
    ) -> list[dict[str, object]]:
        # Same shape as ``test_retrieve_pages_returns_qmd_hits_with_empty_reason``:
        # the wiki pass returns nothing so only the library pass contributes.
        if collection_filter and "wiki_retrieve" in collection_filter:
            return []
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    answer = synthesize_answer("what is alpha?", wiki, qmd_search=fake_search)

    assert answer.answer == (
        "### what is alpha?\n\n"
        "Based on 1 wiki page(s):\n\n"
        "- [wiki] alpha — Alpha is the first letter. — [alpha](wiki/concepts/alpha.md)"
    )
    # citations are now ``list[Citation]`` (Task 6); wiki-sourced.
    assert answer.citations == [Citation(path="wiki/concepts/alpha.md", source="wiki")]
    assert answer.pages_read == [Citation(path="wiki/concepts/alpha.md", source="wiki")]
    assert answer.fallback_used is False
    assert answer.fallback_reason == ""
    assert answer.page_links == ["[alpha](wiki/concepts/alpha.md)"]


# ---------------------------------------------------------------------------
# Task 6 / Bundle C — tag_filter plumbing through retrieve_pages
# ---------------------------------------------------------------------------
# The seam under test:
#   Orchestrator.run_query(tag_filter=...) → retrieve_pages(tag_filter=...)
#       → _collections_matching(wiki, tag_filter) → set of allowed names
#       → qmd_search(collection_filter=set(...)) → post-filter by first segment
#
# The implicit-self-tag rule lives at the retriever boundary (not in the
# resolver): a collection's name is always addressable regardless of
# what its ``tags`` field contains.


@pytest.fixture
def tagged_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A wiki with three library collections: airflow, amazon, pyspark.

    Library collections are name-only — they carry no per-collection
    yaml-declared tags (the library is canonical source docs, not
    wikis). The implicit self-tag rule means the collection's name
    is the only addressable atom, so ``+airflow`` matches airflow,
    ``+pyspark`` matches pyspark, and ``+provider`` matches nothing
    because no library collection is named provider.
    """
    import shutil

    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR
    from lies.library.paths import Library

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    Library.open.cache_clear()

    root = tmp_path / "tagged"
    root.mkdir()
    (root / "raw").mkdir()
    (root / "wiki").mkdir()
    wiki = make_wiki(name="tagged", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)

    # Seed the library with the three collections.
    lib_root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
    if lib_root.exists():
        shutil.rmtree(lib_root)
    lib_root.mkdir(parents=True, exist_ok=True)
    (lib_root / "collections").mkdir(parents=True, exist_ok=True)
    for name in ("airflow", "amazon", "pyspark"):
        (lib_root / "collections" / name).mkdir()

    return wiki


# --- _collections_matching helper: implicit self-tag --------------------


def test_collections_matching_implicit_self_tag_matches_collection_name(
    tagged_wiki: Wiki,
) -> None:
    """A collection matches ``+<name>`` regardless of any tag metadata.

    Library collections carry no per-collection yaml-declared tags;
    the collection name is the only addressable atom. ``+pyspark``
    must still resolve to the pyspark collection because the
    implicit-self-tag rule covers the name.
    """
    tf = ResolvedTagFilter(include=Include("pyspark"))
    assert _collections_matching(tf) == {"pyspark"}


def test_collections_matching_include_matches_known_collection_name(
    tagged_wiki: Wiki,
) -> None:
    """``+airflow`` matches the airflow library collection."""
    tf = ResolvedTagFilter(include=Include("airflow"))
    assert _collections_matching(tf) == {"airflow"}


def test_collections_matching_include_unknown_collection_returns_empty(
    tagged_wiki: Wiki,
) -> None:
    """``+provider`` matches nothing because no library collection is
    named ``provider``. Library collections carry no yaml-declared
    tags, so multi-tag matching is no longer addressable here — that
    semantic is dead under library-first resolution.
    """
    tf = ResolvedTagFilter(include=Include("provider"))
    assert _collections_matching(tf) == set()


def test_collections_matching_and_chain_intersects(tagged_wiki: Wiki) -> None:
    """``+airflow&amazon`` requires both atoms on the SAME collection.

    Library collections are name-only — no collection can match both
    ``airflow`` and ``amazon`` simultaneously because each only
    matches itself. The And-chain therefore returns empty, even though
    the two atoms are individually valid.
    """
    tf = ResolvedTagFilter(include=And(Include("airflow"), Include("amazon")))
    assert _collections_matching(tf) == set()


def test_collections_matching_or_chain_unions(tagged_wiki: Wiki) -> None:
    """``+airflow|amazon`` matches every library collection with either atom."""
    tf = ResolvedTagFilter(include=Or(Include("airflow"), Include("amazon")))
    assert _collections_matching(tf) == {"airflow", "amazon"}


def test_collections_matching_exclude_drops_matching_collection(
    tagged_wiki: Wiki,
) -> None:
    """A bare ``-amazon`` drops every library collection named amazon.
    Without an include, every other collection is kept."""
    tf = ResolvedTagFilter(exclude="amazon")
    assert _collections_matching(tf) == {"airflow", "pyspark"}


def test_collections_matching_exclude_and_include_compose(
    tagged_wiki: Wiki,
) -> None:
    """``+airflow -amazon`` keeps airflow (exclude only matches amazon)."""
    tf = ResolvedTagFilter(include=Include("airflow"), exclude="amazon")
    assert _collections_matching(tf) == {"airflow"}


def test_collections_matching_no_library_returns_empty(tmp_path: Path) -> None:
    """No library on disk → no library collections → matches nothing."""
    import shutil

    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR
    from lies.library.paths import Library

    Library.open.cache_clear()
    lib_root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
    if lib_root.exists():
        shutil.rmtree(lib_root)

    root = tmp_path / "bare"
    (root / "wiki").mkdir(parents=True)
    make_wiki(name="bare", data_root=root)
    tf = ResolvedTagFilter(include=Include("airflow"))
    assert _collections_matching(tf) == set()


def test_collections_matching_unknown_include_tag_returns_empty(
    tagged_wiki: Wiki,
) -> None:
    """An include atom that doesn't match any collection returns the
    empty set. The resolver already validated the tag exists in the
    available set, so this is the retriever-side mirror — the resolver
    says ``+airflow`` exists; the retriever says no collection's
    effective set contains it (would be a bug elsewhere)."""
    tf = ResolvedTagFilter(include=Include("nope"))
    assert _collections_matching(tf) == set()


# --- F15 t:/c: qualifier dispatch in _collections_matching ----------------


@pytest.fixture
def airflow_cnn_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Spec fixture: two library collections named ``airflow`` and ``cnn``.

    Library collections are name-only, so the t:/c: distinction in
    F15 collapses: ``+t:airflow`` and ``+c:airflow`` both match only
    the airflow collection because no library collection carries the
    airflow tag (only the airflow collection itself). This fixture
    still tests the dispatch logic, but with only one qualifier
    outcome per atom.
    """
    import shutil

    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR
    from lies.library.paths import Library

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    Library.open.cache_clear()

    root = tmp_path / "tagged"
    root.mkdir()
    (root / "raw").mkdir()
    (root / "wiki").mkdir()
    wiki = make_wiki(name="airflow-cnn", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)

    lib_root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
    if lib_root.exists():
        shutil.rmtree(lib_root)
    lib_root.mkdir(parents=True, exist_ok=True)
    (lib_root / "collections").mkdir(parents=True, exist_ok=True)
    for name in ("airflow", "cnn"):
        (lib_root / "collections" / name).mkdir()
    return wiki


@pytest.fixture
def python_django_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Spec fixture: two library collections named ``python`` and ``django``.

    ``+t:python -c:python`` excludes python (self) and keeps django
    only because django's implicit-self-tag is "django", not "python".
    Library collections are name-only — no yaml-declared tags — so
    ``t:python`` matches only the python collection, and django is
    dropped by ``-c:python`` not at all (django's name is not
    python). Result under library semantics: include matches python,
    exclude is a no-op (python name != django), so result is
    ``{"python"}``. This exercises the same dispatch as the
    predecessor's test even though the answer changes.
    """
    import shutil

    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR
    from lies.library.paths import Library

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    Library.open.cache_clear()

    root = tmp_path / "tagged"
    root.mkdir()
    (root / "raw").mkdir()
    (root / "wiki").mkdir()
    wiki = make_wiki(name="python-django", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)

    lib_root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
    if lib_root.exists():
        shutil.rmtree(lib_root)
    lib_root.mkdir(parents=True, exist_ok=True)
    (lib_root / "collections").mkdir(parents=True, exist_ok=True)
    for name in ("python", "django"):
        (lib_root / "collections" / name).mkdir()
    return wiki


def test_collections_matching_t_qualifier_matches_collection_name(
    airflow_cnn_wiki: Wiki,
) -> None:
    """Library collections are name-only; ``t:`` and ``c:`` collapse to the same
    answer — only the collection named airflow matches."""
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="t"))
    assert _collections_matching(tf) == {"airflow"}


def test_collections_matching_c_qualifier_strict_name(
    airflow_cnn_wiki: Wiki,
) -> None:
    """c:airflow matches only the collection named airflow."""
    tf = ResolvedTagFilter(include=Include("airflow", qualifier="c"))
    assert _collections_matching(tf) == {"airflow"}


def test_collections_matching_t_then_c_exclude(
    airflow_cnn_wiki: Wiki,
) -> None:
    """``+t:airflow -c:airflow`` returns empty — the include matches only
    airflow, and the exclude drops the collection that matched."""
    tf = ResolvedTagFilter(
        include=Include("airflow", qualifier="t"),
        exclude="airflow",
        exclude_qualifier="c",
    )
    assert _collections_matching(tf) == set()


def test_collections_matching_t_python_c_python_excludes_self(
    python_django_wiki: Wiki,
) -> None:
    """``+t:python -c:python`` matches python and then drops it; the only
    other collection (django) does NOT have python as an addressable
    atom under library semantics, so the result is empty.

    Under library semantics this test reads as: ``t:python`` resolves
    to {python}, the exclude removes python, leaving empty. The
    predecessor's ``django`` outcome relied on django.yaml declaring
    ``tags=[python, web]``, which is a wiki-yaml semantic that does
    not exist in the library model.
    """
    tf = ResolvedTagFilter(
        include=Include("python", qualifier="t"),
        exclude="python",
        exclude_qualifier="c",
    )
    assert _collections_matching(tf) == set()


# --- retrieve_pages threads tag_filter → collection_filter -------------


def test_retrieve_pages_threads_tag_filter_as_collection_filter(
    tagged_wiki: Wiki,
) -> None:
    """``retrieve_pages(tag_filter=...)`` resolves the filter to a set
    of allowed collection names and forwards it as ``collection_filter``
    to the qmd_search callable."""
    captured_filters: list[set[str] | None] = []

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured_filters.append(kwargs.get("collection_filter"))
        # The wiki pass (filter={wiki_tagged}) returns nothing so it
        # contributes no pages; the test pins the library pass's filter.
        if kwargs.get("collection_filter") and "wiki_tagged" in kwargs["collection_filter"]:
            return []
        return [{"path": "airflow/dag.md", "score": 0.9}]

    # The path must exist on disk for ``_resolve_qmd_pages`` to land.
    (tagged_wiki.data_root / "wiki" / "airflow").mkdir(parents=True, exist_ok=True)
    (tagged_wiki.data_root / "wiki" / "airflow" / "dag.md").write_text(
        "---\ntitle: DAG\n---\nDag is a workflow.\n", encoding="utf-8"
    )

    tf = ResolvedTagFilter(include=Include("airflow"))
    pages, reason = retrieve_pages(
        "what is a DAG?", tagged_wiki, qmd_search=fake_search, tag_filter=tf
    )

    # The first call is the library (primary) pass; the second is the
    # wiki_<name> pass. Pin the library pass's collection_filter.
    assert captured_filters[0] == {"airflow"}
    assert reason == ""
    assert [p.rel_path for p in pages] == ["wiki/airflow/dag.md"]


def test_retrieve_pages_tag_filter_drops_non_matching_qmd_hits(
    tagged_wiki: Wiki,
) -> None:
    """End-to-end: real ``qmd_query`` (mocked subprocess) returns hits
    across multiple collections, the tag filter keeps only the airflow
    hit. Confirms the seam works against the production callable, not
    just the test stub."""
    import json
    import subprocess
    from unittest.mock import patch

    from lies.query.synthesizer import qmd_query

    # Need both pages on disk for them to be readable.
    (tagged_wiki.data_root / "wiki" / "airflow").mkdir(parents=True, exist_ok=True)
    (tagged_wiki.data_root / "wiki" / "airflow" / "dag.md").write_text(
        "---\ntitle: DAG\n---\nDag is a workflow.\n", encoding="utf-8"
    )
    (tagged_wiki.data_root / "wiki" / "amazon").mkdir(parents=True, exist_ok=True)
    (tagged_wiki.data_root / "wiki" / "amazon" / "s3.md").write_text(
        "---\ntitle: S3\n---\nS3 is a bucket.\n", encoding="utf-8"
    )

    payload = json.dumps(
        [
            {"docid": "#a", "score": 0.9, "file": "qmd://airflow/dag.md"},
            {"docid": "#b", "score": 0.7, "file": "qmd://amazon/s3.md"},
        ]
    )

    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")

    tf = ResolvedTagFilter(include=Include("airflow"))
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run", fake_run),
    ):
        pages, _ = retrieve_pages(
            "what is a DAG?",
            tagged_wiki,
            qmd_search=qmd_query,
            tag_filter=tf,
        )

    assert [p.rel_path for p in pages] == ["wiki/airflow/dag.md"]


def test_retrieve_pages_without_tag_filter_passes_none_collection_filter(
    tagged_wiki: Wiki,
) -> None:
    """Back-compat: no tag_filter means ``collection_filter`` is None."""
    captured_filters: list[set[str] | None] = []

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured_filters.append(kwargs.get("collection_filter"))
        return []

    pages, reason = retrieve_pages("anything?", tagged_wiki, qmd_search=fake_search)

    # Pin the library (primary) pass's collection_filter; the wiki_<name>
    # pass follows with its own set.
    assert captured_filters[0] is None
    assert reason == FALLBACK_REASON_NO_RESULTS
    assert pages == []


def test_retrieve_pages_tag_filter_with_no_matching_collections_reports_failure(
    tagged_wiki: Wiki,
) -> None:
    """A filter that resolves to zero collections leaves qmd with an
    empty post-filter list, so qmd raises ``QmdNoResultsError``. Both
    passes fail (no library hits, no wiki hits), so the retriever
    surfaces ``qmd_no_results`` and an empty page list — the
    ``wiki/index.md`` fallback was retired with the two-pass refactor."""
    captured_filters: list[set[str] | None] = []

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured_filters.append(kwargs.get("collection_filter"))
        raise QmdNoResultsError("nothing matched")

    # The setup below is left intact for parity with prior tests but
    # is no longer read by retrieve_pages under the two-pass design.

    tf = ResolvedTagFilter(include=Include("nonexistent-tag"))
    pages, reason = retrieve_pages("any", tagged_wiki, qmd_search=fake_search, tag_filter=tf)

    # The library (primary) pass is invoked with the resolved
    # ``collection_filter=set()`` (no collection matches ``+nonexistent-tag``);
    # the wiki_<name> pass follows with its own set.
    assert captured_filters[0] == set()
    assert reason == FALLBACK_REASON_NO_RESULTS
    assert pages == []


def test_collections_matching_no_malformed_yaml_concern() -> None:
    """Library collections are directories, not YAML configs.

    Library-first resolution means wiki YAML is no longer consulted
    for collection metadata. There is no malformed-yaml failure mode
    to defend against — if a directory exists under
    ``Library.collections_root/``, it counts; if not, it doesn't.
    """
    # This test is intentionally empty; it documents the removal of
    # the predecessor's malformed-yaml defense now that collections
    # are directory-only and yaml-free.
    assert True


# ---------------------------------------------------------------------------
# Task 3 — _resolve_qmd_pages library-first resolution
# ---------------------------------------------------------------------------
# The seam under test:
#   qmd_path -> _resolve_qmd_pages -> PageRead with `source` set per root
#
# Library (``Library.collections_root/<coll>/<file>``) is canonical; wiki
# (``wiki.wiki_dir/<rest>``) is the local override. A path that lands in
# both roots surfaces as two distinct PageRead objects with distinct
# sources; the synthesis step applies the library-wins-on-conflict rule.


def test_resolve_qmd_pages_library_first(tmp_path: Path) -> None:
    """qmd hit at ``claude_platform/foo.md`` resolves to the library
    collections root, not ``wiki.wiki_dir``. Source is ``library``."""
    from lies.library.paths import Library
    from lies.query.synthesizer import _resolve_qmd_pages

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    lib = Library.open()
    mirror_dir = lib.collections_root / "claude_platform"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "skills.md").write_text("# Skills\n\nHow to build skills.\n", encoding="utf-8")

    pages = _resolve_qmd_pages(wiki, ["claude_platform/skills.md"], 5)

    assert len(pages) == 1
    assert pages[0].source == "library"
    assert pages[0].title == "Skills"


def test_resolve_qmd_pages_wiki_when_library_missing(tmp_path: Path) -> None:
    """When the library has no file at the qmd path, fall back to
    ``wiki.wiki_dir``. Source is ``wiki``."""
    from lies.library.paths import Library
    from lies.query.synthesizer import _resolve_qmd_pages

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    # Wiki has the page; library mirror is empty.
    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "local.md").write_text(
        "# Local\n\nLocal wiki content.\n", encoding="utf-8"
    )

    pages = _resolve_qmd_pages(wiki, ["concepts/local.md"], 5)

    assert len(pages) == 1
    assert pages[0].source == "wiki"
    assert pages[0].title == "Local"


def test_resolve_qmd_pages_collision_keeps_both_sources(tmp_path: Path) -> None:
    """Same path in both roots surfaces as two PageRead objects with
    distinct sources. Library is canonical; wiki is the override."""
    from lies.library.paths import Library
    from lies.query.synthesizer import _resolve_qmd_pages

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    lib = Library.open()
    (lib.collections_root / "shared").mkdir(parents=True)
    (lib.collections_root / "shared" / "x.md").write_text(
        "# Library version\n\nUpstream.\n", encoding="utf-8"
    )
    (wiki_dir / "shared").mkdir(parents=True)
    (wiki_dir / "shared" / "x.md").write_text(
        "# Wiki version\n\nEdited locally.\n", encoding="utf-8"
    )

    pages = _resolve_qmd_pages(wiki, ["shared/x.md"], 5)

    sources = sorted(p.source for p in pages)
    assert sources == ["library", "wiki"]
    titles = {p.source: p.title for p in pages}
    assert titles["library"] == "Library version"
    assert titles["wiki"] == "Wiki version"


def test_resolve_qmd_pages_path_traversal_blocked_for_library(tmp_path: Path) -> None:
    """A qmd hit whose path escapes the library collections root is
    dropped (path traversal defense, mirrors existing wiki-side check)."""
    from lies.library.paths import Library
    from lies.query.synthesizer import _resolve_qmd_pages

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    pages = _resolve_qmd_pages(wiki, ["../../etc/passwd"], 5)

    assert pages == []


# ---------------------------------------------------------------------------
# Critical 3: _resolve_qmd_path_in_wiki strips the wiki_<wikiname>/ prefix
# that qmd prepends to every hit from the wiki-rooted collection. Without
# the strip, the resolver joins ``wiki.wiki_dir / wiki_default/concepts/x.md``
# and reads nothing (the real file is at ``wiki.wiki_dir/concepts/x.md``).
# ---------------------------------------------------------------------------


def test_resolve_qmd_path_in_wiki_strips_wiki_collection_prefix(tmp_path: Path) -> None:
    """A qmd hit ``wiki_default/concepts/local.md`` resolves to
    ``wiki.wiki_dir / concepts/local.md`` — not
    ``wiki.wiki_dir / wiki_default / concepts/local.md``.
    """
    from lies.query.synthesizer import _resolve_qmd_path_in_wiki

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "local.md").write_text(
        "# Local\n\nLocal wiki content.\n", encoding="utf-8"
    )

    # The wiki pass filter is {wiki_default}; qmd returns the path with
    # the collection name as the first segment (the qmd URI form minus
    # the qmd:// prefix).
    resolved = _resolve_qmd_path_in_wiki(wiki, "wiki_default/concepts/local.md")
    assert resolved is not None
    assert resolved == (wiki_dir / "concepts" / "local.md").resolve()


def test_resolve_qmd_path_in_wiki_strips_prefix_for_non_default_wiki(
    tmp_path: Path,
) -> None:
    """The strip is parameterized by the wiki's name, not hard-coded to
    ``default`` — a wiki named ``research`` strips ``wiki_research/``."""
    from lies.query.synthesizer import _resolve_qmd_path_in_wiki

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="research", data_root=root)

    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "x.md").write_text("# X\n", encoding="utf-8")

    resolved = _resolve_qmd_path_in_wiki(wiki, "wiki_research/concepts/x.md")
    assert resolved is not None
    assert resolved == (wiki_dir / "concepts" / "x.md").resolve()


def test_resolve_qmd_path_in_wiki_unchanged_when_no_collection_prefix(
    tmp_path: Path,
) -> None:
    """A path without the ``wiki_<name>/`` prefix still resolves cleanly —
    the strip is a no-op when the prefix is absent."""
    from lies.query.synthesizer import _resolve_qmd_path_in_wiki

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "local.md").write_text(
        "# Local\n\nLocal wiki content.\n", encoding="utf-8"
    )

    resolved = _resolve_qmd_path_in_wiki(wiki, "concepts/local.md")
    assert resolved is not None
    assert resolved == (wiki_dir / "concepts" / "local.md").resolve()


def test_retrieve_pages_wiki_pass_with_collection_prefix_resolves(
    tmp_path: Path,
) -> None:
    """End-to-end: when the wiki pass returns paths with the
    ``wiki_<name>/`` prefix, ``retrieve_pages`` still surfaces the
    underlying file. Catches the regression where
    ``_resolve_qmd_path_in_wiki`` joined ``wiki.wiki_dir / raw``
    blindly and produced a non-existent path."""
    from lies.library.paths import Library

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "local.md").write_text(
        "---\ntitle: Local\n---\nLocal wiki content.\n", encoding="utf-8"
    )

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        # Library pass returns nothing; wiki pass returns the hit WITH
        # the ``wiki_default/`` prefix that real qmd produces.
        if collection_filter and "wiki_default" in collection_filter:
            return [{"path": "wiki_default/concepts/local.md", "score": 0.9}]
        return []

    pages, reason = retrieve_pages("q", wiki, qmd_search=fake_qmd_query)

    assert reason == FALLBACK_REASON_WIKI_ONLY
    assert len(pages) == 1
    assert pages[0].source == "wiki"
    assert pages[0].rel_path == "wiki/concepts/local.md"


# ---------------------------------------------------------------------------
# Task 4 — retrieve_pages two-pass retrieval (library + wiki)
# ---------------------------------------------------------------------------
# The seam under test:
#   retrieve_pages(question, wiki) -> (pages, fallback_reason)
#
# Two qmd passes: library collections (primary) then the wiki-rooted
# ``wiki_<name>`` collection (secondary). Library hits are canonical;
# wiki hits surface local overrides and author-only content. A path
# returned by both passes surfaces as two PageRead objects with distinct
# ``source`` fields; the synthesizer applies the library-wins-on-conflict
# rule at answer time.


def test_retrieve_pages_runs_two_qmd_passes(tmp_path: Path) -> None:
    """retrieve_pages runs two qmd_query calls: one for the library
    collections, one for the wiki-rooted collection. Both result
    sets merge into the returned pages."""
    from lies.library.paths import Library

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)

    # Library mirror file
    lib = Library.open()
    (lib.collections_root / "claude_platform").mkdir(parents=True)
    (lib.collections_root / "claude_platform" / "lib.md").write_text(
        "---\ntitle: Library hit\n---\nFrom library.\n", encoding="utf-8"
    )
    # Wiki file
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "wiki.md").write_text(
        "---\ntitle: Wiki hit\n---\nFrom wiki.\n", encoding="utf-8"
    )

    call_log: list[set[str] | None] = []

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        call_log.append(collection_filter)
        if collection_filter and "wiki_default" in collection_filter:
            return [{"path": "concepts/wiki.md", "score": 0.9}]
        return [{"path": "claude_platform/lib.md", "score": 0.8}]

    pages, reason = retrieve_pages("anything", wiki, qmd_search=fake_qmd_query)

    assert reason == ""
    assert len(call_log) == 2
    sources = sorted(p.source for p in pages)
    assert sources == ["library", "wiki"]


def test_retrieve_pages_wiki_only_fallback_reason(tmp_path: Path) -> None:
    """When library returns no results but wiki returns hits, the
    fallback_reason is ``wiki_only`` and the body opens with the
    not-grounded preamble."""
    from lies.library.paths import Library
    from lies.query.synthesizer import FALLBACK_REASON_WIKI_ONLY

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)
    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "x.md").write_text("---\ntitle: X\n---\nLocal.\n", encoding="utf-8")

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        if collection_filter and "wiki_default" in collection_filter:
            return [{"path": "concepts/x.md", "score": 0.9}]
        return []

    pages, reason = retrieve_pages("q", wiki, qmd_search=fake_qmd_query)

    assert reason == FALLBACK_REASON_WIKI_ONLY
    assert len(pages) == 1
    assert pages[0].source == "wiki"


# ---------------------------------------------------------------------------
# Important 5: retrieve_pages dedupes on (rel_path, source)
# ---------------------------------------------------------------------------
# When the same wiki-only path comes back from both qmd passes (since
# ``_resolve_qmd_path_in_wiki`` resolves paths under ``wiki.wiki_dir``
# regardless of which pass produced the hit, and library doesn't mirror
# the path), the retriever must dedupe on (rel_path, source) so the
# orchestrator's ``pages_read`` / ``page_links`` lists don't carry
# duplicates and the extractive body says "Based on 1 wiki page(s)"
# instead of 2.


def test_retrieve_pages_dedupes_wiki_hits_across_passes(tmp_path: Path) -> None:
    """The same wiki-only path returned by both qmd passes surfaces as
    one ``PageRead`` — not two with the same ``(rel_path, source)``."""
    from lies.library.paths import Library

    Library.open.cache_clear()

    root = tmp_path / "wiki"
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True)
    wiki = make_wiki(name="default", data_root=root)
    (wiki_dir / "concepts").mkdir(parents=True)
    (wiki_dir / "concepts" / "x.md").write_text("---\ntitle: X\n---\nLocal.\n", encoding="utf-8")

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        # BOTH passes return the same wiki path; no library mirror
        # exists, so the resolver drops the library pass and surfaces
        # the wiki path from both. Without the dedup fix, the retriever
        # returns two PageRead objects with identical (rel_path, source).
        return [{"path": "concepts/x.md", "score": 0.9}]

    pages, reason = retrieve_pages("q", wiki, qmd_search=fake_qmd_query)

    # One page, not two — both passes produced the same wiki-sourced
    # PageRead and the dedup collapsed them to a single entry.
    assert len(pages) == 1
    assert pages[0].rel_path == "wiki/concepts/x.md"
    assert pages[0].source == "wiki"
    # ``(rel_path, source)`` is the dedup key — both copies collapse to one entry.
    seen_keys = {(p.rel_path, p.source) for p in pages}
    assert len(seen_keys) == 1
