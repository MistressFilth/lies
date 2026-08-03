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
    """Two pages mentioning each other's titles without a markdown link -> 1 finding, safe_to_fix=True."""
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee [Beta](beta.md) for more.\n",
    )
    _write(
        wiki,
        "wiki/concepts/b.md",
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\nAlpha covers the basics.\n",
    )
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki, check=True)

    report = _build_lint_report(_layout(wiki))
    xrefs = [f for f in report.findings if f.category == "missing_xref"]
    # Only one direction has missing link: Alpha links to Beta, Beta mentions Alpha without link.
    assert len(xrefs) == 1
    finding = xrefs[0]
    assert finding.safe_to_fix is True
    assert "wiki/concepts/a.md" in finding.pages or "wiki/concepts/b.md" in finding.pages


def test_missing_xref_ignores_already_linked(wiki: Path) -> None:
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\n[Beta](beta.md) covers details.\n",
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


def _layout(wiki_root: Path):  # type: ignore[no-untyped-def]
    from lies.wiki.layout import WikiLayout

    return WikiLayout(wiki_root)
