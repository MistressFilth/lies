"""Tests for CatalogPage + PageSection."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from lies.memory.catalog_models import CatalogPage, PageSection


def test_catalog_page_minimal() -> None:
    page = CatalogPage(slug="claude-code/concepts/hooks")
    assert page.slug == "claude-code/concepts/hooks"
    assert page.title == ""
    assert page.type == ""
    assert page.source_pkg == ""
    assert page.section is PageSection.wiki
    assert page.updated == ""
    assert page.hash == ""
    assert page.derived_from == ""


def test_catalog_page_frozen() -> None:
    page = CatalogPage(slug="x")
    with pytest.raises(ValidationError):
        page.slug = "y"  # type: ignore[misc]


def test_catalog_page_section_enum_values() -> None:
    assert PageSection.wiki.value == "wiki"
    assert PageSection.ingested.value == "ingested"


def test_from_path_parses_frontmatter(tmp_path: Path) -> None:
    """`from_path` reads the file at wiki-relative path, parses YAML frontmatter."""
    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    page_dir = wiki_root / "wiki" / "claude-code" / "concepts"
    page_dir.mkdir(parents=True)
    page_file = page_dir / "hooks.md"
    today = date.today().isoformat()
    page_file.write_text(
        f"---\ntitle: Hooks\ntype: concept\nupdated: {today}\n---\n\n# Hooks\n",
        encoding="utf-8",
    )
    wiki = _wiki(wiki_root)

    page = CatalogPage.from_path(wiki, "claude-code/concepts/hooks")

    assert page.slug == "claude-code/concepts/hooks"
    assert page.title == "Hooks"
    assert page.type == "concept"
    assert page.source_pkg == "claude-code"
    assert page.section is PageSection.wiki
    assert page.updated == today
    assert page.hash != ""  # sha256 of body


def test_from_path_handles_missing_file(tmp_path: Path) -> None:
    """Missing file → today() as updated, empty title, empty hash, no raise."""
    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    wiki = _wiki(wiki_root)

    page = CatalogPage.from_path(wiki, "missing/slug")
    assert page.slug == "missing/slug"
    assert page.updated == date.today().isoformat()
    assert page.title == ""


def test_from_path_handles_unparseable_frontmatter(tmp_path: Path) -> None:
    """Bad frontmatter → fall back to slug as title; no raise."""
    from lies.wiki.wiki import Wiki

    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    page_dir = wiki_root / "wiki" / "x"
    page_dir.mkdir(parents=True)
    (page_dir / "broken.md").write_text("not yaml\n---\nstill not", encoding="utf-8")
    wiki = Wiki(
        name="test-wiki",
        data_root=wiki_root,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    page = CatalogPage.from_path(wiki, "x/broken")
    # Falls back gracefully; exact fallback shape is implementation choice
    # (slug used as title, or empty). Test pins the no-raise contract.
    assert page.slug == "x/broken"


def _wiki(wiki_root: Path):
    from lies.wiki.wiki import Wiki

    return Wiki(
        name="test-wiki",
        data_root=wiki_root,
        config_root=wiki_root.parent / "config",
        cache_root=wiki_root.parent / "cache",
        state_root=wiki_root.parent / "state",
        runtime_root=wiki_root.parent / "runtime",
    )
