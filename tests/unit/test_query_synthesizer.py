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
    synthesize_answer,
)
from lies.wiki.wiki import Wiki

# ---------------------------------------------------------------------------
# Helpers — fake qmd implementations covering each failure mode
# ---------------------------------------------------------------------------


def _qmd_ok(paths: list[str]):
    """A fake qmd_search that returns the given paths."""

    def _fn(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
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

    def _fn(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
        return [{"file": f"qmd://mywiki/{p}", "score": 1.0, "docid": "#x"} for p in paths]

    return _fn


def _qmd_unavailable(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
    raise QmdNotInstalledError("qmd not found on PATH")


def _qmd_no_results(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
    raise QmdNoResultsError("no results")


def _qmd_failed(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
    raise QmdCommandError("qmd query failed (exit 1): boom")


def _qmd_empty_list(cwd: Path, question: str, top_n: int) -> list[dict[str, Any]]:
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
    # citations and pages_read are data_root-relative (the synthesizer
    # preserves the "wiki/" prefix for backward compatibility with the
    # CLI's existing markdown-link contract).
    assert result.citations == ["wiki/" + p for p in paths]
    assert result.pages_read == ["wiki/" + p for p in paths]
    assert result.page_links == [
        "[Postgres](wiki/entities/postgres.md)",
        "[MVCC](wiki/concepts/mvcc.md)",
    ]
    assert "How does MVCC work?" in result.answer
    # Each cited page must appear as a markdown link in the answer body.
    for link in result.page_links:
        assert link in result.answer


def test_qmd_results_capped_at_top_n(sample_wiki: Wiki) -> None:
    paths = ["entities/postgres.md"] * 10  # more than top_n
    result = synthesize_answer(
        "anything",
        sample_wiki,
        top_n=2,
        qmd_search=_qmd_ok(paths),
    )
    assert result.fallback_used is False
    assert len(result.citations) == 2


def test_qmd_returns_unreadable_paths_triggers_fallback(sample_wiki: Wiki) -> None:
    """qmd returned paths but none of them exist on disk → fallback."""
    paths = ["does-not-exist.md", "also-missing.md"]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(paths),
    )
    # The synthesizer treats "qmd returned hits but no readable files"
    # as a no-results signal, so it falls back.
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS
    # The fallback path found the real pages from the index.
    assert "wiki/entities/postgres.md" in result.citations


def test_qmd_path_traversal_is_dropped(sample_wiki: Wiki) -> None:
    """Defense in depth: paths that escape the wiki root are skipped.

    With nothing readable from qmd, the fallback runs.
    """
    paths = ["../../etc/passwd"]
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_ok(paths),
    )
    # The traversal path was dropped → qmd gave 0 readable → fallback.
    assert result.fallback_used is True
    # Fallback found pages from the index.
    assert any("entities/" in c for c in result.citations)


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
    assert "wiki/mywiki/entities/postgres.md" in result.citations


# ---------------------------------------------------------------------------
# Fallback — qmd unavailable
# ---------------------------------------------------------------------------


def test_qmd_unavailable_falls_back_to_index(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    # Index lists 4 pages; default top_n=5 covers all of them.
    assert len(result.citations) == 4
    assert "wiki/entities/postgres.md" in result.citations
    assert "wiki/concepts/mvcc.md" in result.citations
    # The answer body surfaces that we used the fallback.
    assert "qmd_unavailable" in result.answer


# ---------------------------------------------------------------------------
# Fallback — qmd returns no results
# ---------------------------------------------------------------------------


def test_qmd_no_results_falls_back_to_index(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_no_results,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_NO_RESULTS
    assert len(result.citations) == 4
    assert "qmd_no_results" in result.answer


def test_qmd_empty_list_falls_back_to_index(sample_wiki: Wiki) -> None:
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


def test_qmd_command_error_falls_back_to_index(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_failed,
    )
    assert result.fallback_used is True
    assert result.fallback_reason == FALLBACK_REASON_FAILED
    assert "qmd_failed" in result.answer
    assert len(result.citations) == 4


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


def test_fallback_skips_missing_pages_silently(
    wiki_with_missing_pages: Wiki,
) -> None:
    result = synthesize_answer(
        "anything",
        wiki_with_missing_pages,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    # Only 1 of the 3 referenced pages actually exists.
    assert result.citations == ["wiki/entities/real.md"]


# ---------------------------------------------------------------------------
# Top-N behavior
# ---------------------------------------------------------------------------


def test_fallback_respects_top_n(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        top_n=2,
        qmd_search=_qmd_unavailable,
    )
    assert result.fallback_used is True
    # 4 pages exist, but top_n=2 caps the read.
    assert len(result.citations) == 2
    # The first two links in the index are Postgres, MySQL.
    assert result.citations == [
        "wiki/entities/postgres.md",
        "wiki/entities/mysql.md",
    ]


def test_default_top_n_is_five(sample_wiki: Wiki) -> None:
    """The default top-N should match the schema's default of 5."""
    assert DEFAULT_TOP_N == 5


def test_fallback_with_six_pages_only_reads_five(
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
    assert len(result.citations) == 5


# ---------------------------------------------------------------------------
# Citation / link shape
# ---------------------------------------------------------------------------


def test_citations_are_wiki_relative_paths(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_unavailable,
    )
    for citation in result.citations:
        assert not citation.startswith("/")
        assert not citation.startswith("..")
        assert citation.endswith(".md")


def test_page_links_markdown_format(sample_wiki: Wiki) -> None:
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_unavailable,
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
    """At least one excerpt from a real page should appear in the answer."""
    result = synthesize_answer(
        "anything",
        sample_wiki,
        qmd_search=_qmd_unavailable,
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
