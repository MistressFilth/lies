from pathlib import Path

from lies.etl.quarantine import list_quarantined, quarantine


def test_quarantine_writes_doc_and_reason(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "cpython" / "docs" / "broken.md"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"# broken")
    quarantine(tmp_path, "cpython", "docs/broken.md", "normalize failed: bad encoding")
    poison = tmp_path / ".lies" / "poison" / "cpython" / "docs" / "broken.md"
    assert poison.exists()
    assert poison.read_bytes() == b"# broken"


def test_list_quarantined_returns_paths(tmp_path: Path) -> None:
    (tmp_path / ".lies" / "poison" / "cpython").mkdir(parents=True)
    (tmp_path / ".lies" / "poison" / "cpython" / "x.md").write_bytes(b"x")
    (tmp_path / ".lies" / "poison" / "cpython" / "y.md").write_bytes(b"y")
    out = list_quarantined(tmp_path, "cpython")
    assert any(p == "x.md" for p, _ in out)


def test_quarantine_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert list_quarantined(tmp_path, "cpython") == []
