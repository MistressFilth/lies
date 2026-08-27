"""Pin placement and content of the 'Invisible maintenance contract' section."""

from __future__ import annotations

from pathlib import Path

SCHEMA_PATH = Path("src/lies/schema/default_schema.md")


def test_schema_doc_has_invisible_maintenance_contract_section() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    assert "## Invisible maintenance contract" in text


def test_contract_section_appears_before_frontmatter() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    contract_idx = text.index("## Invisible maintenance contract")
    frontmatter_idx = text.index("## Frontmatter")
    assert contract_idx < frontmatter_idx


def test_contract_section_appears_after_page_types() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    page_types_idx = text.index("## Page types")
    contract_idx = text.index("## Invisible maintenance contract")
    assert page_types_idx < contract_idx


def test_contract_section_mentions_memory_plans_jsonl() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    contract_section = text[
        text.index("## Invisible maintenance contract") : text.index("## Frontmatter")
    ]
    assert "memory_plans.jsonl" in contract_section


def test_contract_section_mentions_lies_memory_reconcile() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    contract_section = text[
        text.index("## Invisible maintenance contract") : text.index("## Frontmatter")
    ]
    assert "lies memory reconcile" in contract_section
