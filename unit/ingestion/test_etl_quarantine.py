from pathlib import Path

import pytest

from lies import xdg
from lies.etl.quarantine import list_quarantined, quarantine
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A Wiki with all five XDG roots under ``tmp_path`` so tests are hermetic."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_quarantine_writes_doc_and_reason(wiki: Wiki) -> None:
    raw = wiki.data_root / "raw" / "cpython" / "docs" / "broken.md"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"# broken")
    reason_text = "normalize failed: bad encoding"
    quarantine(wiki, "cpython", "docs/broken.md", reason_text)
    poison = wiki.poison_root / "cpython" / "docs" / "broken.md"
    assert poison.exists()
    assert poison.read_bytes() == b"# broken"
    reason_sidecar = wiki.poison_root / "cpython" / "docs" / "broken.md.reason"
    assert reason_sidecar.exists()
    assert reason_sidecar.read_text(encoding="utf-8") == reason_text


def test_list_quarantined_returns_paths(wiki: Wiki) -> None:
    poison_root = wiki.poison_root / "cpython"
    poison_root.mkdir(parents=True)
    (poison_root / "x.md").write_bytes(b"x")
    (poison_root / "y.md").write_bytes(b"y")
    (poison_root / "y.md.reason").write_text("upstream timeout", encoding="utf-8")
    (poison_root / "stray.reason").write_text("orphan reason", encoding="utf-8")
    out = list_quarantined(wiki, "cpython")
    paths = [p for p, _ in out]
    assert "x.md" in paths
    assert "y.md" in paths
    assert "stray.reason" not in paths
    assert "y.md.reason" not in paths
    by_path = dict(out)
    assert by_path["x.md"] == ""
    assert by_path["y.md"] == "upstream timeout"


def test_quarantine_empty_dir_returns_empty(wiki: Wiki) -> None:
    assert list_quarantined(wiki, "cpython") == []
