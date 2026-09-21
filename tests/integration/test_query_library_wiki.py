"""End-to-end: wiki with library content + wiki content surfaces both
with source tags in the answer."""

from pathlib import Path

import pytest

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.library.paths import Library
from lies.query.synthesizer import FALLBACK_REASON_WIKI_ONLY
from lies.wiki.wiki import Wiki


@pytest.fixture
def mixed_wiki(tmp_path: Path) -> Wiki:
    """A wiki with both library mirror content and wiki-authored content.

    Library mirror: ``claude_platform/skills.md``
    Wiki: ``wiki/concepts/local.md``
    """
    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = Wiki(
        name="mixed",
        data_root=root,
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / "mixed",
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / "mixed",
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / "mixed",
        runtime_root=xdg.runtime_dir_for("mixed"),
    )

    lib = Library.open()
    mirror_dir = lib.collections_root / "claude_platform"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "skills.md").write_text(
        "---\ntitle: Skills from library\n---\nLibrary content about skills.\n",
        encoding="utf-8",
    )

    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "local.md").write_text(
        "---\ntitle: Local concept\n---\nWiki-authored content.\n",
        encoding="utf-8",
    )

    return wiki


def test_query_returns_library_and_wiki_pages(mixed_wiki: Wiki) -> None:
    """A single retrieve_pages call surfaces both library and wiki
    pages with their respective source tags."""
    from lies.query.synthesizer import retrieve_pages

    Library.open.cache_clear()

    # Patch qmd_query directly so the wrapper sees the same shape
    # real qmd produces but without a subprocess. The first call
    # (library, no wiki_<name> filter) returns the library hit; the
    # second call (wiki-rooted filter) returns the wiki hit.
    call_count = {"n": 0}

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        call_count["n"] += 1
        if collection_filter and any(c.startswith("wiki_") for c in collection_filter):
            return [{"path": "concepts/local.md", "score": 0.9}]
        return [{"path": "claude_platform/skills.md", "score": 0.8}]

    pages, reason = retrieve_pages("skills", mixed_wiki, qmd_search=fake_qmd_query)

    assert call_count["n"] == 2
    sources = sorted(p.source for p in pages)
    assert sources == ["library", "wiki"]
    assert reason == ""


def test_query_wiki_only_when_library_empty(mixed_wiki: Wiki) -> None:
    """Library returns 0 hits, wiki returns hits — fallback_reason
    is wiki_only and the body opens with the not-grounded preamble."""
    from lies.query.synthesizer import retrieve_pages

    Library.open.cache_clear()

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        if collection_filter and any(c.startswith("wiki_") for c in collection_filter):
            return [{"path": "concepts/local.md", "score": 0.9}]
        return []

    pages, reason = retrieve_pages("anything", mixed_wiki, qmd_search=fake_qmd_query)

    assert reason == FALLBACK_REASON_WIKI_ONLY
    assert len(pages) == 1
    assert pages[0].source == "wiki"


@pytest.fixture
def subagents_library_wiki(tmp_path: Path) -> Wiki:
    """A wiki whose library mirror has a single page at
    ``claude_code/agent-sdk/subagents.md`` with a `Context isolation`
    heading. Used by the footnote-block integration test."""
    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    wiki = Wiki(
        name="subagents",
        data_root=root,
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / "subagents",
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / "subagents",
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / "subagents",
        runtime_root=xdg.runtime_dir_for("subagents"),
    )

    lib = Library.open()
    mirror_dir = lib.collections_root / "claude_code" / "agent-sdk"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "subagents.md").write_text(
        "---\ntitle: Subagents in the SDK\n---\n\n"
        "## Context isolation\n\n"
        "Each subagent runs in its own context window.\n",
        encoding="utf-8",
    )

    return wiki


# ---------------------------------------------------------------------------
# F19 reality pins: inline `[[slug]]: "verbatim"` citation form (spec §1).
# Pre-F18 footnote-block tests (``test_footnote_block_appended_for_md_format``
# and ``test_footnote_block_absent_for_table_format``) were deleted when
# F19 retired the ``[^N]`` footnote-block rendering. These tests pin what
# F19 actually emits.
#
# Note on F32: F32's scope (post-ingest repair tools, per TODO.md §12) is
# unrelated to footnote-era migration paths. The conditional
# ``TODO(F32)`` previously attached to this comment block referred to a
# lint-remediation follow-up that has not materialized and would not
# belong to F32 if it did. Removed 2026-09-21 during the chart-format
# addendum review sweep — pre-existing-issue rule per AGENTS.md.
# ---------------------------------------------------------------------------


def test_inline_citation_form_emitted_for_md_format(
    subagents_library_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19 inline citation form for prose ``md`` answers (spec §1).

    Stubs :func:`Orchestrator._query_synthesizer_agent.run_sync` so the
    canned synth answer rides through verbatim, and asserts the F19
    surface: the body carries inline ``[[slug]]: "verbatim"`` citations
    per claim and does NOT carry the retired ``[^N]`` footnote-block
    rendering F19 replaced.
    """
    from lies.agents.query_synthesizer import QueryAnswer
    from lies.orchestrator import Orchestrator
    from tests.conftest import models_for_tests

    body = (
        "Each subagent runs in its own context window.[["
        'claude_code/agent-sdk/subagents]]: "Each subagent runs in '
        'its own context window."'
    )
    synth_answer = QueryAnswer(
        answer=body,
        citations=["claude_code/agent-sdk/subagents"],
        should_file=False,
        format_hint="md",
        claim_citations=[],
    )

    wiki = subagents_library_wiki
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    from unittest import mock

    monkeypatch.setattr(
        type(orch._query_synthesizer_agent),
        "run_sync",
        lambda *a, **kw: mock.Mock(output=synth_answer),
    )
    ans = orch.run_query("anything", file=False)

    # F19 canonical: inline `[[slug]]: "verbatim"` form on the body.
    assert "[[claude_code/agent-sdk/subagents]]:" in ans.answer
    assert '"Each subagent runs in its own context window."' in ans.answer
    # F19 retired: no `Footnotes:` block, no `[^N]` markers.
    assert "Footnotes:" not in ans.answer
    assert "[^1]" not in ans.answer
    assert ans.format == "md"


def test_inline_citation_form_emitted_for_table_format(
    sample_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19 inline citation form for table answers (spec §1).

    Pins the F19 surface on the table format path:
    ``Orchestrator.run_query_with_format(cli_format="table")`` returns
    a ``QueryAnswer`` whose body carries inline ``[[slug]]: "verbatim"``
    citations, NOT the retired ``[^N]`` footnote-block.
    """
    from unittest import mock

    from lies.agents.query_synthesizer import QueryAnswer
    from lies.orchestrator import Orchestrator
    from tests.conftest import models_for_tests

    body = (
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        '[[wiki/concepts/alpha]]: "Alpha is the first letter."'
    )
    synth_answer = QueryAnswer(
        answer=body,
        citations=["wiki/concepts/alpha"],
        should_file=False,
        format_hint="table",
        claim_citations=[],
    )

    wiki = sample_wiki
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    monkeypatch.setattr(
        type(orch._query_synthesizer_agent),
        "run_sync",
        lambda *a, **kw: mock.Mock(output=synth_answer),
    )
    ans = orch.run_query_with_format("anything", cli_format="table", file=False)

    # F19 canonical: inline `[[slug]]: "verbatim"` form on the body.
    assert "[[wiki/concepts/alpha]]:" in ans.answer
    assert '"Alpha is the first letter."' in ans.answer
    # F19 retired: no `Footnotes:` block, no `[^N]` markers.
    assert "Footnotes:" not in ans.answer
    assert "[^1]" not in ans.answer
    assert ans.format == "table"
