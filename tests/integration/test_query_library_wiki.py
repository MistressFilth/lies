"""End-to-end: wiki with library content + wiki content surfaces both
with source tags in the answer."""

from pathlib import Path

import pytest

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.library.paths import Library
from lies.query.citation import ClaimCitation
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


@pytest.mark.xfail(
    strict=False,
    reason=(
        "F19 hard-cutover retired the `[^N]` footnote-block rendering in favor "
        'of the inline `[[slug]]: "verbatim"` form (spec §1). This test '
        "pins pre-F19 behavior the F19 cutover intentionally removed; the "
        "F19 inline form is pinned by `test_inline_citation_form_emitted_*`. "
        "TODO(F32): post-ingest lint-remediation follow-up may revisit "
        "retired-form rendering for migration paths from the footnote era."
    ),
)
def test_footnote_block_appended_for_md_format(
    subagents_library_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: orchestrator renders the footnote block for prose answers.

    Stubs :func:`retrieve_pages` so no qmd subprocess runs, and stubs
    :meth:`Orchestrator._call_query_synthesizer` so the synthesizer
    agent never executes. The stub returns a ``QueryAnswer`` with one
    citation and a corresponding ``ClaimCitation``; the orchestrator
    is responsible for appending the ``Footnotes:`` block to the body
    and for forwarding the validated ``ClaimCitation`` into
    ``SynthesizedAnswer.claim_citations``.
    """
    from lies.agents.query_synthesizer import QueryAnswer
    from lies.orchestrator import Orchestrator
    from lies.query.synthesizer import PageRead
    from tests.conftest import models_for_tests

    page = PageRead(
        rel_path="claude_code/agent-sdk/subagents.md",
        title="Subagents in the SDK",
        spans=[],
        source="library",
        line=42,
        section="Context isolation",
    )

    output = QueryAnswer(
        answer="Each subagent runs in its own context.[^1]",
        citations=["claude_code/agent-sdk/subagents.md"],
        should_file=False,
        format_hint="md",
        claim_citations=[
            ClaimCitation(
                claim="Each subagent runs in its own context",
                citation_index=0,
            ),
        ],
    )

    monkeypatch.setattr(
        "lies.orchestrator.retrieve_pages",
        lambda *a, **kw: ([page], ""),
    )

    def _stub_synth(self: Orchestrator, question: str, pages: list[PageRead]):
        return output, ""

    monkeypatch.setattr(Orchestrator, "_call_query_synthesizer", _stub_synth)

    orch = Orchestrator(
        wiki=subagents_library_wiki,
        models=models_for_tests("test"),
    )
    ans = orch.run_query("anything", file=False)

    # Footnote block appended to the answer body.
    assert "Footnotes:" in ans.answer
    assert "[^1]:" in ans.answer
    assert "claude_code/agent-sdk/subagents.md#L42" in ans.answer
    assert "Context isolation" in ans.answer
    # The synthesized body comes through unchanged above the block.
    assert "Each subagent runs in its own context.[^1]" in ans.answer
    # The validated claim_citations list is forwarded.
    assert len(ans.claim_citations) == 1
    assert ans.claim_citations[0].claim == "Each subagent runs in its own context"
    assert ans.claim_citations[0].citation_index == 0
    # Retrieval + synthesis both succeeded: searched_scope reflects the
    # wiki's collection set, format is md, no fallback, no drops.
    assert ans.synthesis_used is True
    assert ans.fallback_used is False
    assert ans.format == "md"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "F19 hard-cutover retired the `[^N]` footnote-block rendering in favor "
        'of the inline `[[slug]]: "verbatim"` form (spec §1). This test '
        "pins pre-F19 behavior the F19 cutover intentionally removed; the "
        "F19 inline form is pinned by `test_inline_citation_form_emitted_*`. "
        "TODO(F32): post-ingest lint-remediation follow-up may revisit "
        "retired-form rendering for migration paths from the footnote era."
    ),
)
def test_footnote_block_absent_for_table_format(
    sample_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Format-constrained override: table answers skip the footnote block;
    prose answers still render one.

    Stubs :func:`retrieve_pages` so no qmd subprocess runs and stubs
    ``Orchestrator._query_synthesizer_agent.run_sync`` to return a
    ``QueryAnswer`` with one ``ClaimCitation``. The two cases share a
    single agent stub via parameterization on the answer body and the
    ``cli_format`` override.
    """
    from unittest import mock

    from lies.agents.query_synthesizer import QueryAnswer
    from lies.orchestrator import Orchestrator
    from lies.query.synthesizer import PageRead
    from tests.conftest import models_for_tests

    page = PageRead(
        rel_path="wiki/concepts/alpha.md",
        title="Alpha",
        spans=[],
        source="wiki",
        line=1,
        section="Alpha",
    )

    monkeypatch.setattr(
        "lies.orchestrator.retrieve_pages",
        lambda *a, **kw: ([page], ""),
    )

    orch = Orchestrator(wiki=sample_wiki, models=models_for_tests("test"))

    # 1. ``cli_format="table"`` — agent emits a table body with no
    #    ``[^N]`` markers; orchestrator must NOT append a ``Footnotes:``
    #    block, and the surface format stays "table".
    table_output = QueryAnswer(
        answer="| A | B |\n| --- | --- |\n| 1 | 2 |",
        citations=["wiki/concepts/alpha.md"],
        should_file=False,
        format_hint="table",
        claim_citations=[],
    )
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=table_output),
    ):
        table_ans = orch.run_query_with_format("anything", cli_format="table", file=False)

    assert "Footnotes:" not in table_ans.answer
    assert table_ans.format == "table"

    # 2. ``cli_format="md"`` — agent emits a prose body with ``[^1]``
    #    and a paired ``ClaimCitation``; orchestrator MUST append the
    #    ``Footnotes:`` block, just like the unconstrained ``run_query``
    #    path (see ``test_footnote_block_appended_for_md_format``).
    md_output = QueryAnswer(
        answer="Alpha is the first letter.[^1]",
        citations=["wiki/concepts/alpha.md"],
        should_file=False,
        format_hint="md",
        claim_citations=[
            ClaimCitation(
                claim="Alpha is the first letter",
                citation_index=0,
            ),
        ],
    )
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=md_output),
    ):
        md_ans = orch.run_query_with_format("anything", cli_format="md", file=False)

    assert "Footnotes:" in md_ans.answer
    assert "[^1]:" in md_ans.answer
    assert "wiki/concepts/alpha.md#L1" in md_ans.answer
    assert md_ans.format == "md"
    # The validated claim_citations list is forwarded.
    assert len(md_ans.claim_citations) == 1
    assert md_ans.claim_citations[0].claim == "Alpha is the first letter"
    assert md_ans.claim_citations[0].citation_index == 0


# ---------------------------------------------------------------------------
# F19 reality pins: inline `[[slug]]: "verbatim"` citation form (spec §1).
# Replaces the pre-F18 footnote-block tests' coverage with the F19-correct
# emission. The retired form is xfailed above; these tests pin what F19
# actually emits.
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
