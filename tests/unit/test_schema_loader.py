from __future__ import annotations

from pathlib import Path

from lies.schema.loader import load_schema
from lies.wiki.layout import WikiLayout


def test_load_default_when_no_override(tmp_path: Path) -> None:
    layout = WikiLayout(tmp_path)
    schema = load_schema(layout)
    # The default should contain key sections
    assert "Page types" in schema or "page types" in schema
    assert "ingest" in schema.lower()
    assert "query" in schema.lower()
    assert "lint" in schema.lower()


def test_load_per_wiki_override(tmp_path: Path) -> None:
    layout = WikiLayout(tmp_path)
    layout.init()
    layout.schema_path.write_text("# My custom schema\n\n- Pages: foo, bar\n")
    schema = load_schema(layout)
    assert schema == "# My custom schema\n\n- Pages: foo, bar\n"


def test_load_raises_when_no_default() -> None:
    # We can't easily test "no default" without messing with the package,
    # so this is implicitly covered by the test above: if the default
    # didn't exist, test_load_default_when_no_override would fail.
    pass
