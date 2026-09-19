from pathlib import Path
from unittest.mock import patch

from lies.schema.sections import SectionContract
from lies.wiki import Wiki


def _wiki_with_schema(tmp_path: Path, schema_text: str | None) -> Wiki:
    # Wiki's frozen dataclass takes name + five role roots; the brief's
    # ``Wiki(wiki_dir=..., config_dir=...)`` helper assumed a different
    # constructor, so we map ``config_dir`` onto ``config_root`` (where
    # ``schema.md`` lives) and stub the other roots under ``tmp_path``.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    if schema_text is not None:
        (config_dir / "schema.md").write_text(schema_text, encoding="utf-8")
    return Wiki(
        name="test",
        data_root=tmp_path / "wiki",
        config_root=config_dir,
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


def test_wiki_without_schema_uses_default(tmp_path):
    wiki = _wiki_with_schema(tmp_path, schema_text=None)
    contract = wiki.section_contract
    assert "## Evidence" in contract.for_type("synthesis")
    assert "## Overview" in contract.for_type("entity")


def test_wiki_schema_replaces_default(tmp_path):
    wiki = _wiki_with_schema(
        tmp_path,
        schema_text=("## Section contract\n\n- **entity** — `## Custom`, `## Only`\n"),
    )
    contract = wiki.section_contract
    assert contract.for_type("entity") == ["## Custom", "## Only"]
    # synthesis disappears (override block omits it)
    assert contract.for_type("synthesis") == []


def test_wiki_schema_without_block_falls_back(tmp_path):
    wiki = _wiki_with_schema(
        tmp_path,
        schema_text="# Other docs\n\nNo contract here.\n",
    )
    contract = wiki.section_contract
    # Falls back to default → synthesis contract present
    assert "## Evidence" in contract.for_type("synthesis")


def test_section_contract_is_cached(tmp_path):
    wiki = _wiki_with_schema(tmp_path, schema_text=None)
    with patch(
        "lies.schema.loader.parse_section_contract",
        wraps=__import__(
            "lies.schema.loader", fromlist=["parse_section_contract"]
        ).parse_section_contract,
    ) as spy:
        _ = wiki.section_contract
        _ = wiki.section_contract
        _ = wiki.section_contract
        assert spy.call_count == 1


def test_section_contract_empty_when_no_doc(tmp_path, monkeypatch):
    """When default_schema.md is unreadable, contract is empty (no raise)."""
    wiki = _wiki_with_schema(tmp_path, schema_text=None)
    monkeypatch.setattr(
        "lies.schema.loader.parse_section_contract",
        lambda _md: SectionContract(),
    )
    contract = wiki.section_contract
    assert contract.for_type("entity") == []
