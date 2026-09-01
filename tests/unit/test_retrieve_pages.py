"""Unit tests for the retrieve_pages seam lifted out of synthesize_answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd.cli import QmdCommandError, QmdNoResultsError, QmdNotInstalledError
from lies.query.synthesizer import (
    FALLBACK_REASON_FAILED,
    FALLBACK_REASON_NO_RESULTS,
    FALLBACK_REASON_UNAVAILABLE,
    retrieve_pages,
    synthesize_answer,
)
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


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
