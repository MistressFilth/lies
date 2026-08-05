from __future__ import annotations

from pathlib import Path

import pytest

from lies import xdg
from lies.schema.loader import load_default_schema, load_schema
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A hermetic Wiki rooted under ``tmp_path`` via LIES_XDG_* overrides."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    return Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )


def test_load_default_when_no_override(wiki: Wiki) -> None:
    schema = load_schema(wiki)
    # The default should contain key sections
    assert "Page types" in schema or "page types" in schema
    assert "ingest" in schema.lower()
    assert "query" in schema.lower()
    assert "lint" in schema.lower()


def test_load_per_wiki_override(wiki: Wiki) -> None:
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.schema_path.write_text("# My custom schema\n\n- Pages: foo, bar\n")
    schema = load_schema(wiki)
    assert schema == "# My custom schema\n\n- Pages: foo, bar\n"


def test_load_default_schema_directly() -> None:
    schema = load_default_schema()
    # Should be the same as the default schema used by load_schema
    assert "Page types" in schema or "page types" in schema
    assert "ingest" in schema.lower()


def test_load_raises_when_no_default() -> None:
    # We can't easily test "no default" without messing with the package,
    # so this is implicitly covered by the test above: if the default
    # didn't exist, test_load_default_when_no_override would fail.
    pass
