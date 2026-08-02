from pathlib import Path

from lies.etl.quarantine import list_quarantined, quarantine


def test_quarantine_writes_doc_and_reason(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "cpython" / "docs" / "broken.md"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"# broken")
    reason_text = "normalize failed: bad encoding"
    quarantine(tmp_path, "cpython", "docs/broken.md", reason_text)
    poison = tmp_path / ".lies" / "poison" / "cpython" / "docs" / "broken.md"
    assert poison.exists()
    assert poison.read_bytes() == b"# broken"
    reason_sidecar = tmp_path / ".lies" / "poison" / "cpython" / "docs" / "broken.md.reason"
    assert reason_sidecar.exists()
    assert reason_sidecar.read_text(encoding="utf-8") == reason_text


def test_list_quarantined_returns_paths(tmp_path: Path) -> None:
    poison_root = tmp_path / ".lies" / "poison" / "cpython"
    poison_root.mkdir(parents=True)
    (poison_root / "x.md").write_bytes(b"x")
    (poison_root / "y.md").write_bytes(b"y")
    (poison_root / "y.md.reason").write_text("upstream timeout", encoding="utf-8")
    (poison_root / "stray.reason").write_text("orphan reason", encoding="utf-8")
    out = list_quarantined(tmp_path, "cpython")
    paths = [p for p, _ in out]
    assert "x.md" in paths
    assert "y.md" in paths
    assert "stray.reason" not in paths
    assert "y.md.reason" not in paths
    by_path = dict(out)
    assert by_path["x.md"] == ""
    assert by_path["y.md"] == "upstream timeout"


def test_quarantine_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert list_quarantined(tmp_path, "cpython") == []
