"""Tests for the query synthesizer + qmd → wiki/index.md fallback.

These tests pin down the contract documented in the spec's Error
handling table:

    - ``qmd`` not installed → fall back to ``wiki/index.md``.
    - ``qmd query`` returns no results → fall back to ``wiki/index.md``.

And the Query workflow:

    1. Search via qmd.
    2. Read top-N pages (default 5).
    3. Synthesize a cited answer.

The qmd dependency is injected so each test can simulate one of the
failure modes (unavailable, no results, command error, success) without
touching the filesystem outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lies.qmd.cli import (
    QmdCommandError,
    QmdNoResultsError,
    QmdNotInstalledError,
)
from lies.query.models import SynthesizedAnswer
from lies.query.synthesizer import (
    DEFAULT_TOP_N,
    FALLBACK_REASON_FAILED,
    FALLBACK_REASON_NO_RESULTS,
    FALLBACK_REASON_UNAVAILABLE,
    PageRead,
    _qmd_search_dispatch,
    set_qmd_search,
    synthesize_answer,
)
from lies.wiki.wiki import Wiki


def test_page_read_accepts_line_and_section() -> None:
    pr = PageRead(
        rel_path="x.md",
        title="X",
        spans=[],
        source="wiki",
        line=42,
        section="Section",
    )
    assert pr.line == 42
    assert pr.section == "Section"


def test_page_read_line_section_default_none() -> None:
    pr = PageRead(rel_path="x.md", title="X", spans=[], source="wiki")
    assert pr.line is None
    assert pr.section is None


# ---------------------------------------------------------------------------
# Helpers — fake qmd implementations covering each failure mode
# ---------------------------------------------------------------------------


def _qmd_ok(paths: list[str]):
    """A fake qmd_search that returns the given paths."""

    def _fn(cwd: Path, question: str, top_n: int, **kwargs: object) -> list[dict[str, Any]]:
        # The wiki pass is scoped to the ``wiki_<name>`` collection; in
        # these tests there's no qmd-indexed wiki content, so the wiki
        # pass returns no hits. Only the library pass contributes pages.
        if kwargs.get("collection_filter") and any(
            "wiki_" in str(s) for s in kwargs["collection_filter"]
        ):
            return []
        return [{"path": p, "score": 1.0} for p in paths]

    return _fn


def _qmd_ok_real_shape(paths: list[str]):
    """A fake qmd_search that mimics the real qmd --format json shape.

    Each hit has ``file: qmd://<collection>/<path>`` (no top-level
    ``path`` key). After P3-b, :func:`qmd_query` strips the prefix into
    ``path``; the synthesizer sees a normalized payload either way. This
    helper stands in for :func:`lies.qmd.cli.qmd_query` *before* the fix
    would have run (i.e. it represents the raw, un-normalized qmd output)
    so we can prove the synthesizer's contract survives the change.
    """

    def _fn(cwd: Path, question: str, top_n: int, **_kwargs: object) -> list[dict[str, Any]]:
        return [{"file": f"qmd://mywiki/{p}", "score": 1.0, "docid": "#x"} for p in paths]

    return _fn


def _qmd_unavailable(cwd: Path, question: str, top_n: int, **_: object) -> list[dict[str, Any]]:
    raise QmdNotInstalledError("qmd not found on PATH")


def _qmd_no_results(cwd: Path, question: str, top_n: int, **_: object) -> list[dict[str, Any]]:
    raise QmdNoResultsError("no results")


def _qmd_failed(cwd: Path, question: str, top_n: int, **_: object) -> list[dict[str, Any]]:
    raise QmdCommandError("qmd query failed (exit 1): boom")


def _qmd_empty_list(cwd: Path, question: str, top_n: int, **_: object) -> list[dict[str, Any]]:
    """Edge case: qmd returns an empty list instead of raising."""
    return []


# ---------------------------------------------------------------------------
# Empty / trivial inputs
# ---------------------------------------------------------------------------


def test_empty_question_returns_marker(sample_wiki: Wiki) -> None:
    result = synthesize_answer("", sample_wiki)
    assert isinstance(result, SynthesizedAnswer)
    assert "empty question" in result.answer.lower()
    assert result.fallback_used is False
    assert result.citations == []


def test_whitespace_only_question_returns_marker(sample_wiki: Wiki) -> None:
    result = synthesize_answer("   \n  ", sample_wiki)
    assert "empty question" in result.answer.lower()


# ---------------------------------------------------------------------------
# Happy path — qmd returns results
# ---------------------------------------------------------------------------


def test_qmd_happy_path_uses_qmd_results(sample_wiki: Wiki) -> None:
    paths = ["entities/postgres.md", "concepts/mvcc.md"]
    result = synthesize_answer(
        "How does MVCC work?",
        sample_wiki,
        qmd_search=_qmd_ok(paths),
    )
    assert result.fallback_used is False
    assert result.fallback_reason == ""
    # citations and pages_read are now ``list[Citation]`` (Task 6);
    # each entry carries the path + source discriminator. The wiki
    # path is preserved with the "wiki/" prefix for backward
    # compatibility with the CLI's existing markdown-link contract.
    from lies.query.citation import Citation

    assert result.citations == [Citation(path="wiki/" + p, source="wiki") for p in paths]
    assert result.pages_read == [Citation(path="wiki/" + p, source="wiki") for p in paths]
    # page_links carries the markdown-link form for downstream consumers
    # (CLI render dispatch, MCP); the answer body itself uses the inline
    # ``[[slug]]: "verbatim"`` form per the F19 contract.
    assert result.page_links == [
        "[Postgres](wiki/entities/postgres.md)",
        "[MVCC](wiki/concepts/mvcc.md)",
    ]
    assert "How does MVCC work?" in result.answer
    # Each cited page must surface as a ``[[slug]]`` bullet (F19).
    for path in paths:
        bare_slug = path.removesuffix(".md")
        assert f"[[{bare_slug}]]" in result.answer


def test_qmd_results_capped_at_top_n(sample_wiki: Wiki) -> None:
    # Three distinct, on-disk pages, more than top_n=2. ``retrieve_pages``
    # caps at top_n after the per-pass resolver; the dedup on
    # (rel_path, source) doesn't fire here because every path is unique.
    paths = [
        "entities/postgres.md",
        "entities/mysql.md",
        "concepts/mvcc.md",
    ]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        top_n=2,
        qmd_search=_qmd_ok(paths),
    )
    assert result.fallback_used is False
    assert len(result.citations) == 2


def test_qmd_returns_unreadable_paths_returns_empty_answer(sample_wiki: Wiki) -> None:
    """qmd returned paths but none of them exist on disk → both passes
    fail to resolve readable pages → no fallback to ``wiki/index.md``
    (the two-pass refactor retired that fallback) → empty answer."""
    paths = ["does-not-exist.md", "also-missing.md"]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(paths),
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS
    assert result.citations == []


def test_qmd_path_traversal_is_dropped(sample_wiki: Wiki) -> None:
    """Defense in depth: paths that escape the wiki root are skipped.

    Both passes drop the traversal path → no readable pages → empty
    answer (no ``wiki/index.md`` fallback in the two-pass refactor).
    """
    paths = ["../../etc/passwd"]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(paths),
    )
    assert result.fallback_used is True
    assert result.citations == []


def test_qmd_real_shape_with_file_only_triggers_fallback(
    sample_wiki: Wiki,
) -> None:
    """Regression (P3-b): raw qmd payload with only ``file`` keys must not
    silently swallow real hits.

    Before P3-b, ``_qmd_search_dispatch`` read ``r["path"]`` against a
    payload that only had ``r["file"]``; ``qmd_paths`` was always empty
    and the synthesizer fell back to ``wiki/index.md`` even when qmd
    returned hits. The fix is to normalize at the ``qmd_query`` boundary
    in :mod:`lies.qmd.cli`; this test stands in for that boundary by
    injecting a payload that has only ``file`` keys. The synthesizer
    sees no ``path`` and falls back — which is the honest behaviour for
    a caller that bypasses :func:`qmd_query`.
    """
    paths = ["entities/postgres.md", "concepts/mvcc.md"]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok_real_shape(paths),
    )
    # No ``path`` key in the payload → no qmd-sourced citations; fallback
    # to wiki/index.md kicks in. This is the failure mode the P3-b fix
    # prevents when the real ``qmd_query`` is used.
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS


def test_qmd_real_query_end_to_end(sample_wiki: Wiki, monkeypatch) -> None:
    """End-to-end: subprocess mocks the real qmd CLI JSON shape and the
    synthesizer consumes the normalized result through :func:`qmd_query`.

    This is the regression test for the original P3-b report:
    ``lies query "What is a hook?"`` returned "no results" because
    ``_qmd_search_dispatch`` looked for ``r["path"]`` in a payload that
    only had ``r["file"]``. After the fix in :mod:`lies.qmd.cli`, the
    boundary normalizes ``file`` → ``path`` and the synthesizer picks
    up the hit.

    The fixture mirrors the post-#39 per-collection layout: pages live at
    ``<wiki_dir>/<collection>/<rest>``. The qmd mock returns
    ``qmd://mywiki/entities/postgres.md`` so the normalized path becomes
    ``mywiki/entities/postgres.md`` and resolves to the file placed
    under the ``mywiki/`` subdir below.
    """
    import json
    import shutil
    import subprocess
    from unittest.mock import patch

    # Materialize a per-collection wiki subdir for this hit. The fixture
    # was copied at sample_wiki creation time; we add the ``mywiki``
    # collection under ``wiki.wiki_dir`` so the synthesizer can resolve
    # the post-normalization path ``mywiki/entities/postgres.md``.
    wiki_collection = sample_wiki.wiki_dir / "mywiki" / "entities"
    wiki_collection.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        sample_wiki.wiki_dir / "entities" / "postgres.md",
        wiki_collection / "postgres.md",
    )

    payload = json.dumps(
        [
            {
                "docid": "#22b4ff",
                "score": 0.88,
                "file": "qmd://mywiki/entities/postgres.md",
                "title": "What is a hook?",
            }
        ]
    )
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        result = synthesize_answer("What is a hook?", sample_wiki)

    # The hit was preserved through qmd_query → synthesizer; no fallback.
    assert result.fallback_used is False
    assert result.fallback_reason == ""
    # citations are now ``list[Citation]`` (Task 6); the path rides on
    # ``Citation.path``.
    assert any(c.path == "wiki/mywiki/entities/postgres.md" for c in result.citations)


# ---------------------------------------------------------------------------
# Fallback — qmd unavailable
# ---------------------------------------------------------------------------


def test_qmd_unavailable_returns_empty_answer(sample_wiki: Wiki) -> None:
    """Both qmd passes fail with QmdNotInstalledError; no
    ``wiki/index.md`` fallback in the two-pass refactor → empty answer."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    assert result.citations == []
    assert result.pages_read == []
    assert "qmd_unavailable" in result.answer


# ---------------------------------------------------------------------------
# Fallback — qmd returns no results
# ---------------------------------------------------------------------------


def test_qmd_no_results_returns_empty_answer(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_no_results,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS
    assert result.citations == []
    assert "qmd_no_results" in result.answer


def test_qmd_empty_list_returns_empty_answer(sample_wiki: Wiki) -> None:
    """Edge case: qmd returns [] instead of raising."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_empty_list,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS


# ---------------------------------------------------------------------------
# Fallback — qmd command failure
# ---------------------------------------------------------------------------


def test_qmd_command_error_returns_empty_answer(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_failed,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_FAILED
    assert result.citations == []
    assert "qmd_failed" in result.answer


# ---------------------------------------------------------------------------
# Fallback with no index.md
# ---------------------------------------------------------------------------


def test_fallback_with_missing_index_returns_empty_answer(
    empty_wiki: Wiki,
) -> None:
    result = synthesize_answer(
        "anything",
        empty_wiki,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    assert result.citations == []
    assert result.pages_read == []
    assert "no readable pages" in result.answer


def test_no_results_with_missing_index_returns_empty_answer(
    empty_wiki: Wiki,
) -> None:
    result = synthesize_answer(
        "anything",
        empty_wiki,
        qmd_search=_qmd_no_results,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS
    assert "qmd query returned no results" in result.answer


def test_command_failure_with_missing_index_returns_empty_answer(
    empty_wiki: Wiki,
) -> None:
    result = synthesize_answer(
        "anything",
        empty_wiki,
        qmd_search=_qmd_failed,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_FAILED
    assert "qmd query failed" in result.answer


# ---------------------------------------------------------------------------
# Fallback — index references missing pages
# ---------------------------------------------------------------------------


def test_qmd_unavailable_with_index_only_returns_empty_citations(
    wiki_with_missing_pages: Wiki,
) -> None:
    """The ``wiki/index.md`` fallback was retired with the two-pass
    refactor. A wiki with index.md but no qmd-served content yields
    empty citations — both passes fail, and the answer body explains
    the failure rather than silently reading the index."""
    result = synthesize_answer(
        "anything",
        wiki_with_missing_pages,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.citations == []


# ---------------------------------------------------------------------------
# Top-N behavior
# ---------------------------------------------------------------------------


def test_qmd_failure_does_not_fall_back_to_index_with_top_n(sample_wiki: Wiki) -> None:
    """``wiki/index.md`` fallback was retired; top_n no longer applies
    to the index path because the index is no longer read on qmd failure."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        top_n=2,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.citations == []


def test_default_top_n_is_five(sample_wiki: Wiki) -> None:
    """The default top-N should match the schema's default of 5."""
    assert DEFAULT_TOP_N == 5


def test_qmd_failure_with_index_only_returns_empty_citations(
    tmp_path: Path,
) -> None:
    from tests.conftest import make_wiki

    wiki = make_wiki(name="six-pages", data_root=tmp_path)
    wiki.wiki_dir.mkdir(parents=True)
    links = "\n".join(f"- [P{i}](entities/p{i}.md)" for i in range(6))
    (wiki.wiki_dir / "index.md").write_text(f"# Index\n\n{links}\n", encoding="utf-8")
    for i in range(6):
        path = wiki.wiki_dir / "entities" / f"p{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# P{i}\n\nContent for page {i}.\n", encoding="utf-8")

    result = synthesize_answer(
        "anything",
        wiki,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.citations == []


# ---------------------------------------------------------------------------
# Citation / link shape
# ---------------------------------------------------------------------------


def test_citations_are_wiki_relative_paths(sample_wiki: Wiki) -> None:
    """With qmd available, citations follow the wiki-relative contract.

    citations are now ``list[Citation]`` (Task 6); each Citation
    carries its path on ``.path`` and the source on ``.source``.
    """
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(["entities/postgres.md"]),
    )
    for citation in result.citations:
        assert not citation.path.startswith("/")
        assert not citation.path.startswith("..")
        assert citation.path.endswith(".md")
        assert citation.source in ("library", "wiki")


def test_page_links_markdown_format(sample_wiki: Wiki) -> None:
    """With qmd available, page links follow the [Title](path) format."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(["entities/postgres.md"]),
    )
    # Each link is [Title](path).
    for link in result.page_links:
        assert link.startswith("[")
        assert "](" in link
        assert link.endswith(")")


def test_answer_body_includes_question(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "How does Postgres do concurrency?",
        sample_wiki,
        qmd_search=_qmd_unavailable,
    )
    assert "How does Postgres do concurrency?" in result.answer


def test_answer_body_omits_fallback_note_on_happy_path(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(["entities/postgres.md"]),
    )
    # Happy path: no "Note: qmd unavailable" preamble.
    assert "qmd unavailable" not in result.answer.lower()


def test_answer_body_includes_excerpt(sample_wiki: Wiki) -> None:
    """At least one excerpt from a real page should appear in the answer
    when qmd serves hits."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(["entities/postgres.md"]),
    )
    # The Postgres page contains the phrase "MVCC".
    assert "MVCC" in result.answer or "Multi-Version" in result.answer


# ---------------------------------------------------------------------------
# Frontmatter handling
# ---------------------------------------------------------------------------


def test_pages_with_yaml_frontmatter_are_read_correctly(
    sample_wiki: Wiki,
) -> None:
    """Frontmatter at the top of a page must not become the excerpt."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(["entities/postgres.md"]),
    )
    # Postgres page has frontmatter; the first paragraph should be the
    # body, not "title: Postgres".
    assert "title: Postgres" not in result.answer
    assert "PostgreSQL" in result.answer or "MVCC" in result.answer


# ---------------------------------------------------------------------------
# qmd hit metadata threading (Task 5)
# ---------------------------------------------------------------------------


def _qmd_with_lines():
    """Stub qmd_search that returns hits with line numbers."""

    def _fn(cwd, question, top_n, **_kwargs):
        return [
            {"path": "entities/postgres.md", "score": 0.9, "line": 12},
        ]

    return _fn


def test_qmd_hit_line_threads_into_page_read(sample_wiki) -> None:
    """End-to-end: qmd_search returns line → PageRead carries line + section."""
    set_qmd_search(_qmd_with_lines())
    try:
        pages = _qmd_search_dispatch(
            _qmd_with_lines(),
            sample_wiki,
            "anything",
            5,
        )
        assert len(pages) >= 1
        pg = pages[0]
        assert pg.line == 12
        # The fixture has a Postgres page; check section was extracted.
        assert pg.section is not None
    finally:
        from lies.query.synthesizer import qmd_query

        set_qmd_search(qmd_query)


def test_qmd_hit_no_line_yields_none(sample_wiki) -> None:
    """qmd hit without `line` → PageRead.line stays None."""

    def _fn(cwd, question, top_n, **_kwargs):
        return [{"path": "entities/postgres.md", "score": 0.9}]

    pages = _qmd_search_dispatch(_fn, sample_wiki, "anything", 5)
    assert len(pages) >= 1
    assert pages[0].line is None
    assert pages[0].section is None
