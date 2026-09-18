"""Unit tests for ``lies.memory.provenance``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lies.memory.catalog import open_catalog, upsert_page
from lies.memory.catalog_models import CatalogPage
from lies.memory.provenance import (
    ProvenanceRecord,
    list_provenance_pages,
    render_provenance_json,
    render_provenance_tsv,
)


@pytest.fixture
def catalog_conn(tmp_path: Path):
    """Bootstrap a wiki with three pages: two synthesised, one source."""
    from lies.wiki.wiki import Wiki

    data_root = tmp_path / "test-wiki"
    wiki_root = data_root / "wiki"
    wiki_root.mkdir(parents=True)
    wiki = Wiki(
        name="test-wiki",
        data_root=data_root,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    conn = open_catalog(wiki)
    try:
        # Source pages (no derived_from) — never appear in results.
        upsert_page(
            conn, CatalogPage(slug="claude-code/concepts/hooks", title="Hooks", type="concept")
        )
        upsert_page(
            conn, CatalogPage(slug="claude-code/concepts/skills", title="Skills", type="concept")
        )
        # Synthesised pages with provenance.
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/synthesis/synthesis-of-hooks",
                title="Synthesis of Hooks",
                type="synthesis",
                derived_from="claude-code/concepts/hooks,claude-code/concepts/skills",
            ),
        )
        # Dangling — cites a slug that does not exist.
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/synthesis/orphan",
                title="Orphan",
                type="synthesis",
                derived_from="claude-code/concepts/skills,claude-code/concepts/missing",
            ),
        )
        yield conn
    finally:
        conn.close()


def test_provenance_record_is_frozen_dataclass() -> None:
    rec = ProvenanceRecord(
        slug="a/b",
        title="A",
        type="synthesis",
        source_pkg="a",
        updated="2026-09-18",
        derived_from=("c/d", "e/f"),
    )
    with pytest.raises((AttributeError, Exception)):
        rec.slug = "mutated"  # type: ignore[misc]


def test_list_returns_only_pages_with_derived_from(catalog_conn) -> None:
    records = list_provenance_pages(catalog_conn)
    slugs = {r.slug for r in records}
    # Source pages (hooks, skills) have empty derived_from — must be excluded.
    assert "claude-code/concepts/hooks" not in slugs
    assert "claude-code/concepts/skills" not in slugs
    assert slugs == {
        "claude-code/synthesis/synthesis-of-hooks",
        "claude-code/synthesis/orphan",
    }


def test_list_filters_by_page(catalog_conn) -> None:
    records = list_provenance_pages(catalog_conn, page="claude-code/synthesis/synthesis-of-hooks")
    assert len(records) == 1
    assert records[0].slug == "claude-code/synthesis/synthesis-of-hooks"


def test_list_filters_missing_page(catalog_conn) -> None:
    records = list_provenance_pages(catalog_conn, page="claude-code/concepts/none")
    assert records == []


def test_list_orphan_filters_to_dangling_only(catalog_conn) -> None:
    records = list_provenance_pages(catalog_conn, orphan=True)
    slugs = {r.slug for r in records}
    assert slugs == {"claude-code/synthesis/orphan"}


def test_list_page_with_orphan_true_returns_empty_when_clean(catalog_conn) -> None:
    records = list_provenance_pages(
        catalog_conn,
        page="claude-code/synthesis/synthesis-of-hooks",
        orphan=True,
    )
    assert records == []


def test_list_page_with_orphan_true_returns_record_when_dangling(catalog_conn) -> None:
    records = list_provenance_pages(
        catalog_conn,
        page="claude-code/synthesis/orphan",
        orphan=True,
    )
    assert len(records) == 1
    assert records[0].slug == "claude-code/synthesis/orphan"


def test_list_empty_derived_from_fragment_is_filtered(catalog_conn) -> None:
    """Sloppy ``derived_from: 'a,,b,'`` storage must yield only ``('a', 'b')``."""
    from lies.memory.catalog import upsert_page
    from lies.memory.catalog_models import CatalogPage

    upsert_page(
        catalog_conn,
        CatalogPage(
            slug="claude-code/synthesis/sloppy",
            title="Sloppy",
            type="synthesis",
            derived_from="claude-code/concepts/hooks,,claude-code/concepts/skills,",
        ),
    )
    records = list_provenance_pages(catalog_conn, page="claude-code/synthesis/sloppy")
    assert len(records) == 1
    assert records[0].derived_from == ("claude-code/concepts/hooks", "claude-code/concepts/skills")


def test_list_derived_from_is_tuple_not_list(catalog_conn) -> None:
    records = list_provenance_pages(catalog_conn)
    for r in records:
        assert isinstance(r.derived_from, tuple)


def _sample_records() -> list[ProvenanceRecord]:
    return [
        ProvenanceRecord(
            slug="synthesis/a",
            title="A title",
            type="synthesis",
            source_pkg="default",
            updated="2026-09-18T00:00:00Z",
            derived_from=("concept/x", "concept/y"),
        ),
        ProvenanceRecord(
            slug="synthesis/b",
            title="B title",
            type="synthesis",
            source_pkg="default",
            updated="2026-09-18T01:00:00Z",
            derived_from=("concept/z",),
        ),
    ]


def test_render_tsv_one_row_per_page() -> None:
    out = render_provenance_tsv(_sample_records())
    assert out.splitlines() == [
        "synthesis/a\tA title\tsynthesis\tdefault\t2026-09-18T00:00:00Z\tconcept/x,concept/y",
        "synthesis/b\tB title\tsynthesis\tdefault\t2026-09-18T01:00:00Z\tconcept/z",
    ]


def test_render_tsv_empty_records_is_empty_string() -> None:
    assert render_provenance_tsv([]) == ""


def test_render_tsv_single_source_no_trailing_comma() -> None:
    recs = [
        ProvenanceRecord(
            slug="x",
            title="X",
            type="synthesis",
            source_pkg="p",
            updated="",
            derived_from=("only",),
        ),
    ]
    out = render_provenance_tsv(recs)
    assert out == "x\tX\tsynthesis\tp\t\tonly"


def test_render_json_emits_array_of_objects() -> None:
    out = render_provenance_json(_sample_records())
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert payload[0] == {
        "slug": "synthesis/a",
        "title": "A title",
        "type": "synthesis",
        "source_pkg": "default",
        "updated": "2026-09-18T00:00:00Z",
        "derived_from": ["concept/x", "concept/y"],
    }


def test_render_json_derived_from_is_array_not_string() -> None:
    out = render_provenance_json(_sample_records())
    for row in json.loads(out):
        assert isinstance(row["derived_from"], list)
        for s in row["derived_from"]:
            assert isinstance(s, str)


def test_render_json_empty_records_is_empty_array() -> None:
    assert json.loads(render_provenance_json([])) == []
