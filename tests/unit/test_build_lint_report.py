"""Unit tests for the deterministic host-side _build_lint_report."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.orchestrator import _build_lint_report


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return root


def _write(wiki: Path, rel: str, body: str) -> None:
    path = wiki / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=wiki, check=True)


def test_missing_xref_two_pages_mention_each_other(wiki: Path) -> None:
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
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    xrefs = [f for f in report.findings if f.category == "missing_xref"]
    # Only one direction has missing link: A links to B, B mentions A without link.
    assert len(xrefs) == 1
    finding = xrefs[0]
    assert finding.safe_to_fix is True
    assert "wiki/concepts/a.md" in finding.pages or "wiki/concepts/b.md" in finding.pages


def test_missing_xref_ignores_already_linked(wiki: Path) -> None:
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
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    assert all(f.category != "missing_xref" for f in report.findings)


def test_missing_xref_skips_title_collisions(wiki: Path) -> None:
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
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    assert all(f.category != "missing_xref" for f in report.findings)


def test_missing_xref_links_to_third_page_still_flags(wiki: Path) -> None:
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
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    xrefs = [f for f in report.findings if f.category == "missing_xref"]
    # Both directions: A mentions B without linking to B; B mentions A without any link.
    assert len(xrefs) == 2, (
        f"expected 2 missing_xref findings (A mentions B without linking to B; "
        f"B mentions A without any link), got {len(xrefs)}: {[f.message for f in xrefs]}"
    )
    # A→B finding: page references both A and B with A first.
    a_to_b = [
        f
        for f in xrefs
        if f.pages[0] == "wiki/concepts/a.md" and f.pages[1] == "wiki/concepts/b.md"
    ]
    assert a_to_b, "expected a missing_xref finding for A mentioning B (link goes to C, not B)"
    assert a_to_b[0].safe_to_fix is True
    # B→A finding: page references both B and A with B first.
    b_to_a = [
        f
        for f in xrefs
        if f.pages[0] == "wiki/concepts/b.md" and f.pages[1] == "wiki/concepts/a.md"
    ]
    assert b_to_a, "expected a missing_xref finding for B mentioning A without any link"
    assert b_to_a[0].safe_to_fix is True


def test_missing_page_nonexistent_source(wiki: Path) -> None:
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: A\ntype: concept\nsources:\n  - raw/missing.md\n---\n# A\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    missing = [f for f in report.findings if f.category == "missing_page"]
    assert len(missing) == 1
    assert missing[0].safe_to_fix is False
    assert "raw/missing.md" in missing[0].message


def test_missing_page_ignores_existing_sources(wiki: Path) -> None:
    (wiki / "raw").mkdir(exist_ok=True)
    (wiki / "raw" / "present.md").write_text("present", encoding="utf-8")
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: A\ntype: concept\nsources:\n  - raw/present.md\n---\n# A\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    assert all(f.category != "missing_page" for f in report.findings)


def test_shell_emits_no_data_gap(wiki: Path) -> None:
    """data_gap stays LLM-only; deterministic shell never emits it."""
    _write(wiki, "wiki/concepts/a.md", "---\ntitle: A\ntype: concept\n---\n# A\n")
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)
    report = _build_lint_report(_layout(wiki))
    assert all(f.category != "data_gap" for f in report.findings)


def _layout(wiki_root: Path):  # type: ignore[no-untyped-def]
    from lies.wiki.layout import WikiLayout

    return WikiLayout(wiki_root)
