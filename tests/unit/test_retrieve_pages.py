"""Unit tests for the retrieve_pages seam lifted out of synthesize_answer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lies.collections.record import Collection, save_collection
from lies.qmd.cli import QmdCommandError, QmdNoResultsError, QmdNotInstalledError
from lies.query.synthesizer import (
    FALLBACK_REASON_FAILED,
    FALLBACK_REASON_NO_RESULTS,
    FALLBACK_REASON_UNAVAILABLE,
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
    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == ""
    assert [p.rel_path for p in pages] == ["wiki/concepts/alpha.md"]


def test_retrieve_pages_falls_back_when_qmd_not_installed(wiki: Wiki) -> None:
    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QmdNotInstalledError("qmd not on PATH")

    pages, reason = retrieve_pages("what is alpha?", wiki, qmd_search=fake_search)

    assert reason == FALLBACK_REASON_UNAVAILABLE
    assert [p.rel_path for p in pages] == ["wiki/concepts/alpha.md"]


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

    def fake_search(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    answer = synthesize_answer("what is alpha?", wiki, qmd_search=fake_search)

    assert answer.answer == (
        "### what is alpha?\n\n"
        "Based on 1 wiki page(s):\n\n"
        "- alpha — Alpha is the first letter. — [alpha](wiki/concepts/alpha.md)"
    )
    assert answer.citations == ["wiki/concepts/alpha.md"]
    assert answer.pages_read == ["wiki/concepts/alpha.md"]
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
def tagged_wiki(tmp_path: Path) -> Wiki:
    """A wiki with three collections: airflow (with tags), amazon (with
    tags), pyspark (no tags). The implicit self-tag rule means the name
    is always addressable, so ``+pyspark`` matches even though pyspark
    has no tags beyond its own name."""
    root = tmp_path / "tagged"
    root.mkdir()
    (root / "raw").mkdir()
    (root / "wiki").mkdir()
    wiki = make_wiki(name="tagged", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    _COLLECTIONS = {
        "airflow": ["airflow", "provider"],
        "amazon": ["amazon", "aws"],
        "pyspark": [],
    }
    for name, tags in _COLLECTIONS.items():
        save_collection(
            wiki,
            Collection(
                name=name,
                path=wiki.data_root / "raw" / name,
                source=f"https://example.com/{name}",
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
    return wiki


# --- _collections_matching helper: implicit self-tag --------------------


def test_collections_matching_implicit_self_tag_matches_collection_name(
    tagged_wiki: Wiki,
) -> None:
    """A collection matches ``+<name>`` even when ``name`` is not in its
    declared ``tags`` list — pyspark has no tags, but ``+pyspark`` must
    still resolve to it."""
    tf = ResolvedTagFilter(include=Include("pyspark"))
    assert _collections_matching(tagged_wiki, tf) == {"pyspark"}


def test_collections_matching_include_matches_declared_tag(
    tagged_wiki: Wiki,
) -> None:
    """A collection matches ``+<tag>`` when the tag is in its ``tags``."""
    tf = ResolvedTagFilter(include=Include("provider"))
    assert _collections_matching(tagged_wiki, tf) == {"airflow"}


def test_collections_matching_and_chain_intersects(tagged_wiki: Wiki) -> None:
    """``+airflow&provider`` requires both atoms on the same collection."""
    tf = ResolvedTagFilter(include=And(Include("airflow"), Include("provider")))
    assert _collections_matching(tagged_wiki, tf) == {"airflow"}


def test_collections_matching_or_chain_unions(tagged_wiki: Wiki) -> None:
    """``+airflow|amazon`` matches every collection with either atom."""
    tf = ResolvedTagFilter(include=Or(Include("airflow"), Include("amazon")))
    assert _collections_matching(tagged_wiki, tf) == {"airflow", "amazon"}


def test_collections_matching_exclude_drops_matching_collection(
    tagged_wiki: Wiki,
) -> None:
    """A bare ``-amazon`` drops every collection whose ``tags ∪ {name}``
    contains ``amazon``. Without an include, every other collection is
    kept."""
    tf = ResolvedTagFilter(exclude="amazon")
    assert _collections_matching(tagged_wiki, tf) == {"airflow", "pyspark"}


def test_collections_matching_exclude_and_include_compose(
    tagged_wiki: Wiki,
) -> None:
    """``+airflow -provider`` keeps airflow only if ``provider`` is not
    in its effective set — and provider IS in airflow's effective set,
    so airflow is dropped. The exclude wins on collision."""
    tf = ResolvedTagFilter(include=Include("airflow"), exclude="provider")
    assert _collections_matching(tagged_wiki, tf) == set()


def test_collections_matching_no_collections_dir_returns_empty(tmp_path: Path) -> None:
    """A wiki with no ``collections_dir`` matches nothing — there is no
    collection to evaluate."""
    root = tmp_path / "bare"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="bare", data_root=root)
    tf = ResolvedTagFilter(include=Include("airflow"))
    assert _collections_matching(wiki, tf) == set()


def test_collections_matching_unknown_include_tag_returns_empty(
    tagged_wiki: Wiki,
) -> None:
    """An include atom that doesn't match any collection returns the
    empty set. The resolver already validated the tag exists in the
    available set, so this is the retriever-side mirror — the resolver
    says ``+airflow`` exists; the retriever says no collection's
    effective set contains it (would be a bug elsewhere)."""
    tf = ResolvedTagFilter(include=Include("nope"))
    assert _collections_matching(tagged_wiki, tf) == set()


# --- retrieve_pages threads tag_filter → collection_filter -------------


def test_retrieve_pages_threads_tag_filter_as_collection_filter(
    tagged_wiki: Wiki,
) -> None:
    """``retrieve_pages(tag_filter=...)`` resolves the filter to a set
    of allowed collection names and forwards it as ``collection_filter``
    to the qmd_search callable."""
    captured: dict[str, object] = {}

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured["collection_filter"] = kwargs.get("collection_filter")
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

    assert captured["collection_filter"] == {"airflow"}
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
    captured: dict[str, object] = {}

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured["collection_filter"] = kwargs.get("collection_filter")
        return []

    pages, reason = retrieve_pages("anything?", tagged_wiki, qmd_search=fake_search)

    assert captured["collection_filter"] is None
    assert reason == FALLBACK_REASON_NO_RESULTS
    assert pages == []


def test_retrieve_pages_tag_filter_with_no_matching_collections_falls_back(
    tagged_wiki: Wiki,
) -> None:
    """A filter that resolves to zero collections leaves qmd with an
    empty post-filter list, so qmd raises ``QmdNoResultsError`` and the
    synthesizer falls back to ``wiki/index.md``."""
    captured: dict[str, object] = {}

    def fake_search(*_args: object, **kwargs: object) -> list[dict[str, object]]:
        captured["collection_filter"] = kwargs.get("collection_filter")
        raise QmdNoResultsError("nothing matched")

    (tagged_wiki.data_root / "wiki" / "index.md").write_text(
        "# Index\n\n## airflow\n\n- [DAG](airflow/dag.md) — `airflow`\n",
        encoding="utf-8",
    )
    (tagged_wiki.data_root / "wiki" / "airflow").mkdir(parents=True, exist_ok=True)
    (tagged_wiki.data_root / "wiki" / "airflow" / "dag.md").write_text(
        "---\ntitle: DAG\n---\nDag.\n", encoding="utf-8"
    )

    tf = ResolvedTagFilter(include=Include("nonexistent-tag"))
    pages, reason = retrieve_pages("any", tagged_wiki, qmd_search=fake_search, tag_filter=tf)

    assert captured["collection_filter"] == set()
    assert reason == FALLBACK_REASON_NO_RESULTS
    assert [p.rel_path for p in pages] == ["wiki/airflow/dag.md"]
