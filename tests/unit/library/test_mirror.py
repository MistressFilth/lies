from pathlib import Path
import pytest
from lies.library.mirror import write_mirror, render_mirror
from lies.library.paths import Library


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    return Library.open()


def test_render_mirror_default_title() -> None:
    text = render_mirror(
        slug="getting-started",
        body="# hello\n",
        source_url="https://example.com/",
        source_path=None,
        source_hash="abc",
        fetched_via="web",
    )
    assert text.startswith("---\n")
    assert "title: Getting Started\n" in text
    assert text.endswith("# hello\n")


def test_render_mirror_with_explicit_title() -> None:
    text = render_mirror(
        slug="x",
        body="b",
        source_url=None,
        source_path="/p",
        source_hash="abc",
        fetched_via="github",
        title="Override",
    )
    assert "title: Override\n" in text


def test_write_mirror_creates_file(lib: Library) -> None:
    coll = lib.collection("claude")
    coll.dir.mkdir(parents=True)
    path = write_mirror(
        coll,
        slug="getting-started",
        body="body content\n",
        source_url="https://example.com/",
        source_hash="abc123",
        fetched_via="web",
    )
    assert path == coll.dir / "getting-started.md"
    assert path.exists()
    content = path.read_text()
    assert content.endswith("body content\n")
    assert "source_hash: abc123\n" in content


def test_write_mirror_collision_without_force_raises(lib: Library) -> None:
    coll = lib.collection("claude")
    coll.dir.mkdir(parents=True)
    write_mirror(
        coll,
        slug="x",
        body="first",
        source_url=None,
        source_hash="a",
        fetched_via="web",
    )
    with pytest.raises(FileExistsError):
        write_mirror(
            coll,
            slug="x",
            body="second",
            source_url=None,
            source_hash="b",
            fetched_via="web",
        )


def test_write_mirror_force_overwrites(lib: Library) -> None:
    coll = lib.collection("claude")
    coll.dir.mkdir(parents=True)
    write_mirror(
        coll,
        slug="x",
        body="first",
        source_url=None,
        source_hash="a",
        fetched_via="web",
    )
    write_mirror(
        coll,
        slug="x",
        body="second",
        source_url=None,
        source_hash="b",
        fetched_via="web",
        force=True,
    )
    assert (coll.dir / "x.md").read_text().endswith("second\n")


def test_write_mirror_invalid_slug_raises(lib: Library) -> None:
    coll = lib.collection("claude")
    with pytest.raises(ValueError):
        write_mirror(
            coll,
            slug="../bad",
            body="x",
            source_url=None,
            source_hash="a",
            fetched_via="web",
        )
