"""Unit tests for the deterministic host-side _build_lint_report."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.orchestrator import _build_lint_report
from lies.wikilinks import WikiLinkResolver
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path):
    """A Wiki dataclass rooted at ``tmp_path/wiki`` with a git-initialised
    data root containing ``wiki/`` (markdown), ``raw/``, and a seeded index.
    """
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return make_wiki(name="lint-test", data_root=root)


def _write(wiki, rel: str, body: str) -> None:
    """Write ``body`` at ``wiki.data_root / rel`` and ``git add`` it.

    The fixture parameter shadows the Wiki dataclass name; we resolve the
    on-disk data root via ``wiki.data_root`` so git operations land in the
    correct repo regardless of the XDG role routing.
    """
    data_root: Path = wiki.data_root
    path = data_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=data_root, check=True)


def test_missing_xref_two_pages_mention_each_other(wiki) -> None:
    """Two pages mentioning each other's titles without a markdown link -> 1 finding, safe_to_fix=True.

    A's link [Beta](b.md) resolves to wiki/concepts/b.md (B's path) under
    source-directory resolution, so A→B direction is not flagged. B has
    no links at all, so B→A emits a finding.
    """
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee [Beta](b.md) for more.\n",
    )
    _write(
        wiki,
        "wiki/concepts/b.md",
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\nAlpha covers the basics.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    xrefs = [f for f in report.findings if f.category == "missing_xref"]
    # Only one direction has missing link: A links to B, B mentions A without link.
    assert len(xrefs) == 1
    finding = xrefs[0]
    assert finding.safe_to_fix is True
    assert "concepts/a.md" in finding.pages or "concepts/b.md" in finding.pages


def test_missing_xref_ignores_already_linked(wiki) -> None:
    """Both directions cross-link; A→B resolves to wiki/concepts/b.md and B→A to wiki/concepts/a.md.

    The heuristic is target-specific: each pair checks whether the
    source's resolved targets include the other's wiki-relative path.
    """
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\n[Beta](b.md) covers details.\n",
    )
    _write(
        wiki,
        "wiki/concepts/b.md",
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\n[Alpha](a.md) covers the basics.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    assert all(f.category != "missing_xref" for f in report.findings)


def test_missing_xref_skips_title_collisions(wiki) -> None:
    """Two pages with identical titles -> check skips both to avoid false positives."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Same\ntype: concept\n---\n# Same\n\nThe other Same page covers this.\n",
    )
    _write(
        wiki,
        "wiki/concepts/b.md",
        "---\ntitle: Same\ntype: concept\n---\n# Same\n\nThe other Same page covers that.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    assert all(f.category != "missing_xref" for f in report.findings)


def test_missing_xref_links_to_third_page_still_flags(wiki) -> None:
    """A mentions B's title but its only link resolves to C (not B).

    Distinguishes "specific link" from "any link" semantics. With
    target-specific resolution, A's link [Gamma](c.md) resolves to
    wiki/concepts/c.md, so A still gets flagged for mentioning B
    without linking to B. B has no links at all and mentions A, so B
    is flagged too. Expect 2 findings.
    """
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\n"
        "Beta is interesting; see [Gamma](c.md).\n",
    )
    _write(
        wiki,
        "wiki/concepts/b.md",
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\nAlpha covers the basics.\n",
    )
    _write(
        wiki,
        "wiki/concepts/c.md",
        "---\ntitle: Gamma\ntype: concept\n---\n# Gamma\n\ndetails.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    xrefs = [f for f in report.findings if f.category == "missing_xref"]
    # Both directions: A mentions B without linking to B; B mentions A without any link.
    assert len(xrefs) == 2, (
        f"expected 2 missing_xref findings (A mentions B without linking to B; "
        f"B mentions A without any link), got {len(xrefs)}: {[f.message for f in xrefs]}"
    )
    # A→B finding: page references both A and B with A first.
    a_to_b = [f for f in xrefs if f.pages[0] == "concepts/a.md" and f.pages[1] == "concepts/b.md"]
    assert a_to_b, "expected a missing_xref finding for A mentioning B (link goes to C, not B)"
    assert a_to_b[0].safe_to_fix is True
    # B→A finding: page references both B and A with B first.
    b_to_a = [f for f in xrefs if f.pages[0] == "concepts/b.md" and f.pages[1] == "concepts/a.md"]
    assert b_to_a, "expected a missing_xref finding for B mentioning A without any link"
    assert b_to_a[0].safe_to_fix is True


def test_missing_page_nonexistent_source(wiki) -> None:
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: A\ntype: concept\nsources:\n  - raw/missing.md\n---\n# A\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    missing = [f for f in report.findings if f.category == "missing_page"]
    assert len(missing) == 1
    assert missing[0].safe_to_fix is False
    assert "raw/missing.md" in missing[0].message


def test_missing_page_ignores_existing_sources(wiki) -> None:
    (wiki.data_root / "raw").mkdir(exist_ok=True)
    (wiki.data_root / "raw" / "present.md").write_text("present", encoding="utf-8")
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: A\ntype: concept\nsources:\n  - raw/present.md\n---\n# A\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    report = _build_lint_report(wiki)
    assert all(f.category != "missing_page" for f in report.findings)


def test_shell_emits_no_data_gap(wiki) -> None:
    """data_gap stays LLM-only; deterministic shell never emits it."""
    _write(wiki, "wiki/concepts/a.md", "---\ntitle: A\ntype: concept\n---\n# A\n")
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)
    report = _build_lint_report(wiki)
    assert all(f.category != "data_gap" for f in report.findings)


def _wikilink_resolver(wiki) -> WikiLinkResolver:
    """Build a resolver over wiki/ + raw/ for the lint-report test fixture."""
    return WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))


def test_wikilink_resolved_no_finding(wiki) -> None:
    """[[Page]] that resolves via the corpus emits no missing_page finding."""
    _write(
        wiki, "wiki/concepts/a.md", "---\ntitle: Alpha\n---\n# Alpha\n\nSee [[Beta]] for details.\n"
    )
    _write(wiki, "wiki/concepts/b.md", "---\ntitle: Beta\n---\n# Beta\n")
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    resolver = _wikilink_resolver(wiki)
    report = _build_lint_report(wiki, resolver=resolver)
    missing_pages = [f for f in report.findings if f.category == "missing_page"]
    assert missing_pages == []


def test_wikilink_broken_emits_missing_page(wiki) -> None:
    """[[Broken]] that doesn't resolve emits a missing_page finding."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\n---\n# Alpha\n\nSee [[Broken]] for details.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    resolver = _wikilink_resolver(wiki)
    report = _build_lint_report(wiki, resolver=resolver)
    missing_pages = [f for f in report.findings if f.category == "missing_page"]
    assert len(missing_pages) == 1
    finding = missing_pages[0]
    assert "concepts/a.md" in finding.pages
    assert "Broken" in finding.message


def test_wikilink_inside_code_block_ignored(wiki) -> None:
    """Wikilink syntax inside a fenced code block does not flag."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\n---\n# Alpha\n\n```\n[[Broken]]\n```\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    resolver = _wikilink_resolver(wiki)
    report = _build_lint_report(wiki, resolver=resolver)
    missing_pages = [f for f in report.findings if f.category == "missing_page"]
    assert missing_pages == []


def test_wikilink_mixed_with_markdown_link(wiki) -> None:
    """Markdown and wikilink on one page are classified independently."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\n---\n# Alpha\n\nSee [Beta](b.md) and [[Beta]] and [[Broken]].\n",
    )
    _write(wiki, "wiki/concepts/b.md", "---\ntitle: Beta\n---\n# Beta\n")
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    resolver = _wikilink_resolver(wiki)
    report = _build_lint_report(wiki, resolver=resolver)
    missing_pages = [f for f in report.findings if f.category == "missing_page"]
    # Only [[Broken]] flags; [[Beta]] and [Beta](b.md) both resolve.
    assert len(missing_pages) == 1
    assert "Broken" in missing_pages[0].message


def test_empty_corpus_flags_every_wikilink(wiki) -> None:
    """Empty corpus → every wikilink flagged as missing_page."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\n---\n# Alpha\n\n[[Beta]] [[Gamma]]\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    resolver = WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))
    # Sanity: corpus has alpha.md but not Beta.md nor Gamma.md, so only
    # [[Beta]] and [[Gamma]] flag.
    report = _build_lint_report(wiki, resolver=resolver)
    missing_pages = [f for f in report.findings if f.category == "missing_page"]
    flagged = sorted(f.message for f in missing_pages)
    assert len(flagged) == 2
    assert any("Beta" in m for m in flagged)
    assert any("Gamma" in m for m in flagged)
