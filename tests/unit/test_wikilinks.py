"""Unit tests for the wikilink extraction helper."""

from __future__ import annotations

from lies.wikilinks import extract_wikilinks


class TestExtractWikilinks:
    def test_bare(self) -> None:
        assert extract_wikilinks("see [[Page]] for details") == ["Page"]

    def test_alias_discarded(self) -> None:
        assert extract_wikilinks("see [[Page|display text]] here") == ["Page"]

    def test_heading_discarded(self) -> None:
        assert extract_wikilinks("see [[Page#Heading]] here") == ["Page"]

    def test_mixed_preserves_order(self) -> None:
        text = "[[First]] then [[Second|other]] then [[Third#Anchor]]"
        assert extract_wikilinks(text) == ["First", "Second", "Third"]

    def test_inline_code_skipped(self) -> None:
        text = "real [[Real]] but not `[[Fake]]`"
        assert extract_wikilinks(text) == ["Real"]

    def test_fenced_code_block_skipped(self) -> None:
        text = "real [[Real]]\n\n```\n[[Fake]]\n```\n"
        assert extract_wikilinks(text) == ["Real"]

    def test_tilde_fenced_code_block_skipped(self) -> None:
        text = "real [[Real]]\n\n~~~\n[[Fake]]\n~~~\n"
        assert extract_wikilinks(text) == ["Real"]

    def test_embed_not_captured(self) -> None:
        # ![[...]] is out of scope per the spec; not captured.
        assert extract_wikilinks("see ![[Embed]] here") == []

    def test_empty_target_skipped(self) -> None:
        # Regex requires non-empty target; [[]] produces nothing.
        assert extract_wikilinks("text [[]] more") == []

    def test_no_wikilinks(self) -> None:
        assert extract_wikilinks("just plain text, no links.") == []

    def test_md_extension_preserved_in_extraction(self) -> None:
        # Extension stripping happens in resolve(), not extract().
        assert extract_wikilinks("[[Page.md]]") == ["Page.md"]
