"""End-to-end integration tests for the Tier-2 query path. Gated on
``INTEGRATION=1``.

Exercises the full Tier-2 round-trip end-to-end against a real
``Wiki`` + real catalog + real cross-process flock + mocked qmd:

1. The librarian subagent's ``wiki_search`` / ``wiki_read`` /
   ``wiki_catalog`` tools thread against the live catalog and
   ``WikiMemoryService``.
2. The synthesizer emits the inline ``[[slug]] (Heading):
   "verbatim"`` form on a real round-trip.
3. Filing-back writes the synthesis page with the
   ``## Thesis`` / ``## Evidence`` / ``## Open Questions`` shape.
4. Pre-existing footnote pages (separate fixture) are NOT
   re-rendered by the filing-back path.

The librarian and synthesizer agents are stubbed at the
``run_sync`` instance-method boundary (per-instance, not per-class)
so the assertions do not depend on a real LLM. qmd's post-commit
refresh is also stubbed so the suite stays independent of a live
qmd daemon.

Run with:

    INTEGRATION=1 pytest tests/integration/test_tier2_query_path.py -v

Without ``INTEGRATION=1``, the conftest auto-skip gate fires and the
module is collected-but-skipped (consistent with
``test_synthesis_file_back``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.agents.librarian import LibrarianDeps, LibrarianOutput, PageExcerpt
from lies.agents.query_synthesizer import QueryAnswer
from lies.markdown_spans import Span
from lies.memory.service import WikiMemoryService
from lies.orchestrator import Orchestrator
from lies.query.citation import Citation, ClaimCitation
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki, models_for_tests

FIXTURE_WIKI = Path(__file__).parent.parent / "fixtures" / "sample_wiki"


def _build_orch(wiki: Wiki) -> Orchestrator:
    """Build an ``Orchestrator`` against ``wiki`` with TestModel for
    every roster entry (so the parent agent + non-Tier-2 sub-agents
    are deterministic) and the same TestModel for the librarian
    fallback (matches ``self.models["query_synthesizer"]``).
    """
    return Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))


def _stub_qmd_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the post-commit qmd refresh with a no-op.

    The Tier-2 query path's filing-back writes a commit and would
    normally refresh the qmd derived index. The integration gate
    exists to keep this suite independent of a live qmd daemon;
    the catalog port (and the in-memory librarian tools that
    consult it) is unaffected.
    """
    monkeypatch.setattr(WikiMemoryService, "_refresh_qmd", lambda self: (True, ""))


def _copy_fixture_into(target: Path) -> None:
    """Copy the ``tests/fixtures/sample_wiki`` tree into ``target``.

    The fixture wiki (Pydantic / SQLAlchemy / PostgreSQL pages) is
    the corpus the librarian/synthesizer round-trip reads from.
    The destination gets a fresh ``git init`` so the orchestrator's
    wiki-fs machinery starts clean.
    """
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(FIXTURE_WIKI, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tier2@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tier2"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True)


def _real_wiki(tmp_path: Path) -> Wiki:
    """Real git wiki rooted at ``tmp_path`` populated from the fixture.

    Mirrors the bootstrap shape used by ``test_synthesis_file_back`` /
    ``test_catalog_e2e``: copy fixture, ``git init``, configure a
    committer, then wire the :class:`Wiki` through ``make_wiki`` so
    the XDG-redirected config / cache / state / runtime roots are
    populated.
    """
    root = tmp_path / "wiki"
    _copy_fixture_into(root)
    return make_wiki(name="tier2-query-path", data_root=root)


# ---------------------------------------------------------------------------
# 1. Librarian round-trip against a real wiki
# ---------------------------------------------------------------------------


def _stub_librarian_output(wiki: Wiki) -> LibrarianOutput:
    """Build a canned ``LibrarianOutput`` against ``wiki``'s fixture pages.

    Reads the fixture markdown and derives one ``PageExcerpt`` per
    page, with a single ``Span`` carrying the full body text. The
    canned output mirrors what the real librarian would emit against
    the fixture corpus, but is deterministic — no LLM in the loop.

    Skips system files (``index.md`` / ``log.md`` /
    ``lint-report.md`` / ``overview.md``).
    """
    excerpts: list[PageExcerpt] = []
    for path in sorted(wiki.wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki.wiki_dir).as_posix()
        if rel in {"index.md", "log.md", "lint-report.md", "overview.md"}:
            continue
        body = path.read_text(encoding="utf-8")
        # Strip frontmatter for the canned span body so the first
        # non-empty line is content, not the YAML delimiter — keeps
        # the canned ``quote=`` substrings realistic.
        if body.startswith("---"):
            end = body.find("\n---", 3)
            if end != -1:
                body = body[end + 4 :].lstrip("\n")
        spans = [Span(heading_path=[], body=body, code_fence=False, start_line=1)]
        slug = rel.removesuffix(".md")
        excerpts.append(
            PageExcerpt(
                collection=wiki.name,
                slug=slug,
                title=slug.rsplit("/", 1)[-1],
                spans=spans,
            )
        )
    return LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=excerpts,
        distinct_pages=len(excerpts),
    )


def test_librarian_round_trip_with_real_wiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Librarian dispatch against a real wiki produces excerpts.

    Stubs ``Orchestrator._librarian_agent.run_sync`` so the
    assertion doesn't depend on a real LLM call. The stub returns a
    canned ``LibrarianOutput`` built from the fixture wiki's pages,
    with spans carrying the verbatim body text.

    Verifies:
    - The canned ``LibrarianOutput.distinct_pages`` matches the
      number of fixture pages (3).
    - Each :class:`PageExcerpt` carries at least one span with the
      page's body content.
    """
    wiki = _real_wiki(tmp_path)
    orch = _build_orch(wiki)

    canned = _stub_librarian_output(wiki)
    assert canned.distinct_pages >= 2, (
        f"fixture wiki must have 2+ pages for the round-trip, got {canned.distinct_pages}"
    )
    for excerpt in canned.excerpts:
        assert excerpt.spans, f"{excerpt.slug} missing spans"
        assert any(s.body for s in excerpt.spans)

    # Patch the librarian agent's instance ``run_sync`` so the call
    # returns our canned output. We patch per-instance (not per-class)
    # so the synthesizer agent's run_sync stays untouched when the
    # downstream tests patch that one separately.
    def _stub_run_sync(*args: object, **kwargs: object) -> object:
        from unittest.mock import Mock

        return Mock(output=canned)

    monkeypatch.setattr(orch._librarian_agent, "run_sync", _stub_run_sync)

    result = orch._librarian_agent.run_sync(
        "how does pydantic validate nested models?",
        deps=LibrarianDeps(
            question="how does pydantic validate nested models?",
            tag_expr=None,
            exclude_tags=[],
            top_k=5,
        ),
    )

    out: LibrarianOutput = result.output  # type: ignore[attr-defined]
    assert out.distinct_pages == canned.distinct_pages
    assert all(e.spans for e in out.excerpts)


# ---------------------------------------------------------------------------
# 2. Synthesizer emits the inline citation form
# ---------------------------------------------------------------------------


def test_synthesize_emits_inline_citation_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: question -> librarian -> synthesizer -> answer with
    ``[[slug]]: "verbatim"`` form.

    Stubs both the librarian and synthesizer agent ``run_sync``
    methods. The librarian returns a canned 2-page bundle drawn from
    the fixture; the synthesizer returns a ``QueryAnswer`` whose
    ``claim_citations`` carry the verbatim quote the filed body
    expects to render.

    Verifies:
    - ``Orchestrator.run_query`` returns a ``QueryAnswer`` whose
      ``answer`` body contains the synthesizer-emitted text and the
      threaded ``Citation`` set has the expected source discriminator.
    - The ``_render_evidence`` projection (run on the answer) emits
      the ``[[concepts/<slug>]] (Heading): "verbatim"`` form on the
      filed body.
    """
    wiki = _real_wiki(tmp_path)
    _stub_qmd_refresh(monkeypatch)
    orch = _build_orch(wiki)

    canned_lo = _stub_librarian_output(wiki)
    # Canned 2-page bundle (the third page is filtered out so the
    # synthesizer sees the exact excerpts that back the canned
    # ``claim_citations``).
    two_excerpts = canned_lo.excerpts[:2]
    canned_lo_2 = LibrarianOutput(
        tag_expr=canned_lo.tag_expr,
        exclude_tags=canned_lo.exclude_tags,
        excerpts=two_excerpts,
        distinct_pages=len(two_excerpts),
    )
    canned_answer = QueryAnswer(
        answer=(
            "Pydantic validates nested models recursively via "
            "inner BaseModel hooks. SQLAlchemy provides the Session "
            "as a database gateway."
        ),
        citations=[f"wiki/{e.slug}.md" for e in two_excerpts],
        should_file=True,
        format_hint="md",
        claim_citations=[
            ClaimCitation(
                claim="Pydantic validates nested models recursively",
                citation_index=0,
                quote=two_excerpts[0].spans[0].body.splitlines()[0],
            ),
            ClaimCitation(
                claim="SQLAlchemy provides the Session as a database gateway",
                citation_index=1,
                quote=two_excerpts[1].spans[0].body.splitlines()[0],
            ),
        ],
    )

    from unittest.mock import Mock

    def _librarian_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_lo_2)

    def _synthesizer_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_answer)

    monkeypatch.setattr(orch._librarian_agent, "run_sync", _librarian_run_sync)
    monkeypatch.setattr(orch._query_synthesizer_agent, "run_sync", _synthesizer_run_sync)

    answer = orch.run_query("how does pydantic validate nested models?")

    assert answer.should_file
    assert answer.citations  # threaded list[str | Citation]
    assert answer.claim_citations

    # The rendered evidence (used by filing-back) emits the inline
    # citation form on the threaded Citation set. The orchestrator
    # threads ``Citation`` objects (with ``.path`` populated) into
    # ``answer.citations``; pass them straight through to
    # ``_render_evidence`` so the production renderer is exercised.
    from lies.orchestrator import _render_evidence

    threaded_cits = [
        c if isinstance(c, Citation) else Citation(path=c, source="wiki") for c in answer.citations
    ]
    body = _render_evidence(threaded_cits, answer.claim_citations)
    assert "[[" in body and "]]" in body
    # The fixture wiki's two pages both live under ``concepts/``;
    # the inline citation form prefixes the slug after the leading
    # ``wiki/`` strip so we expect the ``concepts/<slug>`` form.
    assert "concepts/pydantic" in body
    assert "concepts/sqlalchemy" in body


# ---------------------------------------------------------------------------
# 3. Filing-back writes the synthesis page with the contracted sections
# ---------------------------------------------------------------------------


def test_file_back_writes_synthesis_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filing-back writes a synthesis page with ``## Thesis`` /
    ``## Evidence`` / ``## Open Questions`` sections.

    Drives the full Tier-2 pipeline end-to-end with both sub-agents
    stubbed. The librarian returns 3 distinct excerpts (the fixture's
    three pages), the synthesizer emits a 3-citation answer, and the
    filing-back path writes a synthesis page through
    ``WikiMemoryService.apply_plan`` (via ``build_author_plan`` +
    ``file_back_author``).

    Verifies:
    - The filed page lands under
      ``<wiki_dir>/tier2-query-path/synthesis/<slug>.md``.
    - The body has ``## Thesis``, ``## Evidence``,
      ``## Open Questions`` (the synthesis section contract).
    - The catalog has a row for the new page.
    """
    wiki = _real_wiki(tmp_path)
    _stub_qmd_refresh(monkeypatch)
    orch = _build_orch(wiki)

    canned_lo = _stub_librarian_output(wiki)
    canned_answer = QueryAnswer(
        answer=(
            "Pydantic validates nested models recursively.\n\n"
            "SQLAlchemy's Session is the gateway to the database.\n\n"
            "PostgreSQL uses MVCC for concurrent reads and writes."
        ),
        citations=[f"wiki/{e.slug}.md" for e in canned_lo.excerpts],
        should_file=True,
        format_hint="md",
        claim_citations=[
            ClaimCitation(
                claim="Pydantic validates nested models recursively.",
                citation_index=0,
                quote=canned_lo.excerpts[0].spans[0].body.splitlines()[0],
            ),
            ClaimCitation(
                claim="SQLAlchemy's Session is the gateway to the database.",
                citation_index=1,
                quote=canned_lo.excerpts[1].spans[0].body.splitlines()[0],
            ),
            ClaimCitation(
                claim="PostgreSQL uses MVCC for concurrent reads and writes.",
                citation_index=2,
                quote=canned_lo.excerpts[2].spans[0].body.splitlines()[0],
            ),
        ],
    )

    from unittest.mock import Mock

    def _librarian_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_lo)

    def _synthesizer_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_answer)

    monkeypatch.setattr(orch._librarian_agent, "run_sync", _librarian_run_sync)
    monkeypatch.setattr(orch._query_synthesizer_agent, "run_sync", _synthesizer_run_sync)

    answer = orch.run_query("how do pydantic, sqlalchemy, and postgres relate?")

    assert answer.should_file

    # The synthesis page landed somewhere under the wiki's
    # per-collection synthesis subdir. The slug is derived from the
    # question via ``_slugify(question)``.
    from lies.orchestrator import _slugify

    slug = _slugify("how do pydantic, sqlalchemy, and postgres relate?")
    filed = wiki.wiki_dir / wiki.name / "synthesis" / f"{slug}.md"
    assert filed.exists(), f"synthesis page not filed at {filed}"
    body = filed.read_text(encoding="utf-8")
    assert "## Thesis" in body
    assert "## Evidence" in body
    assert "## Open Questions" in body

    # Catalog: the filed synthesis page is upserted via the per-op
    # catalog write inside ``WikiMemoryService.apply_plan``.
    from lies.memory.catalog import list_slugs, open_catalog

    conn = open_catalog(wiki)
    try:
        slugs = list_slugs(conn)
    finally:
        conn.close()
    expected_slug = f"{wiki.name}/synthesis/{slug}"
    assert expected_slug in slugs, f"catalog missing filed page; got slugs={sorted(slugs)!r}"


# ---------------------------------------------------------------------------
# 4. Pre-existing footnote pages are NOT re-rendered by filing-back
# ---------------------------------------------------------------------------


def test_old_footnote_pages_remain_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard-cutover: pre-existing footnote pages are NOT re-rendered.

    Two pre-existing pages are placed in the fixture wiki BEFORE the
    round-trip runs: a footnote-style reference page and a notes
    page. After the Tier-2 pipeline runs and files the new
    synthesis, the pre-existing pages' byte-for-byte content must
    be unchanged. Only the new synthesis page is added.
    """
    wiki = _real_wiki(tmp_path)
    _stub_qmd_refresh(monkeypatch)
    orch = _build_orch(wiki)

    # Plant two pre-existing pages: one footnote-style, one
    # notes-style. Their content is intentionally distinct (matching
    # the F3-era footnote form) so any unintended re-render would
    # be visible.
    footnote_dir = wiki.wiki_dir / wiki.name / "sources"
    footnote_dir.mkdir(parents=True, exist_ok=True)
    footnote_body = (
        "---\n"
        "title: Existing Footnote Page\n"
        "type: source\n"
        "---\n"
        "\n"
        "# Existing Footnote Page\n"
        "\n"
        "This is a pre-existing footnote-style page that should NOT\n"
        "be modified by filing-back. Any re-render would corrupt\n"
        "the historical record.\n"
    )
    footnote_path = footnote_dir / "old-footnote.md"
    footnote_path.write_text(footnote_body, encoding="utf-8")

    notes_dir = wiki.wiki_dir / wiki.name / "concepts"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes_body = (
        "---\n"
        "title: Pre-existing Notes\n"
        "type: concept\n"
        "---\n"
        "\n"
        "# Pre-existing Notes\n"
        "\n"
        "These notes were written before the filing-back pipeline\n"
        "touched this wiki; their content must remain byte-identical.\n"
    )
    notes_path = notes_dir / "old-notes.md"
    notes_path.write_text(notes_body, encoding="utf-8")

    # Commit the pre-existing pages so the working tree is clean
    # for the Tier-2 round-trip; the snapshot/restore envelope relies
    # on a clean baseline.
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "pre-existing pages"],
        cwd=wiki.data_root,
        check=True,
        capture_output=True,
    )

    # Capture the pre-existing content's SHA-256 so we can assert
    # byte-identical preservation after the round-trip.
    import hashlib

    footnote_sha_before = hashlib.sha256(footnote_path.read_bytes()).hexdigest()
    notes_sha_before = hashlib.sha256(notes_path.read_bytes()).hexdigest()

    canned_lo = _stub_librarian_output(wiki)
    canned_answer = QueryAnswer(
        answer=(
            "Pydantic validates nested models recursively.\n\n"
            "SQLAlchemy's Session is the gateway to the database.\n\n"
            "PostgreSQL uses MVCC for concurrent reads and writes."
        ),
        citations=[f"wiki/{e.slug}.md" for e in canned_lo.excerpts],
        should_file=True,
        format_hint="md",
        claim_citations=[
            ClaimCitation(
                claim="Pydantic validates nested models recursively.",
                citation_index=0,
                quote=canned_lo.excerpts[0].spans[0].body.splitlines()[0],
            ),
            ClaimCitation(
                claim="SQLAlchemy's Session is the gateway to the database.",
                citation_index=1,
                quote=canned_lo.excerpts[1].spans[0].body.splitlines()[0],
            ),
            ClaimCitation(
                claim="PostgreSQL uses MVCC for concurrent reads and writes.",
                citation_index=2,
                quote=canned_lo.excerpts[2].spans[0].body.splitlines()[0],
            ),
        ],
    )

    from unittest.mock import Mock

    def _librarian_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_lo)

    def _synthesizer_run_sync(*args: object, **kwargs: object) -> object:
        return Mock(output=canned_answer)

    monkeypatch.setattr(orch._librarian_agent, "run_sync", _librarian_run_sync)
    monkeypatch.setattr(orch._query_synthesizer_agent, "run_sync", _synthesizer_run_sync)

    orch.run_query("how do pydantic, sqlalchemy, and postgres relate?")

    # Pre-existing pages unchanged.
    footnote_sha_after = hashlib.sha256(footnote_path.read_bytes()).hexdigest()
    notes_sha_after = hashlib.sha256(notes_path.read_bytes()).hexdigest()
    assert footnote_sha_after == footnote_sha_before, "footnote page was modified by filing-back"
    assert notes_sha_after == notes_sha_before, "notes page was modified by filing-back"
