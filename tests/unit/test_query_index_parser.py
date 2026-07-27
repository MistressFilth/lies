"""Tests for the wiki/index.md markdown link parser."""
from __future__ import annotations

from lies.query.index_parser import parse_index_links

SAMPLE = """\
# Index

## entities
- [Postgres](entities/postgres.md) — PostgreSQL database
- [MySQL](entities/mysql.md) — MySQL database
- [External](https://example.com/page.md) — should be skipped
- [Anchor](#section) — should be skipped
- [Image](assets/diagram.png) — should be skipped
- [Query](entities/foo.md?ref=top) — query stripped, deduped if same target

## concepts
- [MVCC](concepts/mvcc.md) — Multi-Version Concurrency Control
"""


def test_parse_returns_links_in_order() -> None:
    links = parse_index_links(SAMPLE)
    titles = [l.title for l in links]
    # Postgres, MySQL, Query (with query string, kept), MVCC.
    # External, Anchor, and Image are skipped (URL / fragment / non-.md).
    assert titles == ["Postgres", "MySQL", "Query", "MVCC"]


def test_parse_strips_fragments_and_queries() -> None:
    sample = "- [Foo](entities/foo.md?ref=top#section)\n"
    [link] = parse_index_links(sample)
    assert link.path == "entities/foo.md"
    assert link.title == "Foo"


def test_parse_skips_http_urls() -> None:
    sample = "- [Web](https://example.com/page.md)\n- [Local](entities/x.md)\n"
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Local"]


def test_parse_skips_mailto_and_tel() -> None:
    sample = "- [Mail](mailto:a@b.com)\n- [Tel](tel:123)\n- [Local](e.md)\n"
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Local"]


def test_parse_skips_anchor_only_links() -> None:
    sample = "- [Sec](#section)\n- [Local](e.md)\n"
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Local"]


def test_parse_skips_non_md_paths() -> None:
    sample = "- [Pic](assets/foo.png)\n- [Local](e.md)\n"
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Local"]


def test_parse_skips_absolute_paths() -> None:
    sample = "- [Abs](/etc/passwd.md)\n- [Local](e.md)\n"
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Local"]


def test_parse_deduplicates_same_title_and_path() -> None:
    sample = "- [Foo](e.md)\n- [Foo](e.md)\n"
    links = parse_index_links(sample)
    assert len(links) == 1


def test_parse_allows_same_title_different_path() -> None:
    sample = "- [Same](a.md)\n- [Same](b.md)\n"
    links = parse_index_links(sample)
    assert [l.path for l in links] == ["a.md", "b.md"]


def test_parse_empty_input() -> None:
    assert parse_index_links("") == []


def test_parse_handles_no_links() -> None:
    assert parse_index_links("# Just a heading\n\nNo links here.\n") == []


def test_parse_handles_inline_links_in_paragraphs() -> None:
    sample = (
        "See [Foo](entities/foo.md) for the overview and "
        "[Bar](entities/bar.md) for details.\n"
    )
    links = parse_index_links(sample)
    assert [l.title for l in links] == ["Foo", "Bar"]
