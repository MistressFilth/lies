from lies.library.frontmatter import build_frontmatter


def test_build_frontmatter_full() -> None:
    fm = build_frontmatter(
        title="Getting Started",
        source_url="https://example.com/start.html",
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    assert fm.startswith("---\n")
    assert fm.endswith("---\n")
    assert "title: Getting Started\n" in fm
    assert "source_url: https://example.com/start.html\n" in fm
    assert "source_path: null\n" in fm or "source_path:\n" in fm
    assert "source_hash: a3f0c8d4b1\n" in fm
    assert "fetched_via: web\n" in fm
    assert "ingested_at: " in fm
    assert "type:" not in fm  # library mirrors are type-less


def test_ingested_at_deterministic_from_hash() -> None:
    a = build_frontmatter(
        title="X",
        source_url=None,
        source_path="/x",
        source_hash="00000000ffff",
        fetched_via="pdf",
    )
    b = build_frontmatter(
        title="X",
        source_url=None,
        source_path="/x",
        source_hash="00000000ffff",
        fetched_via="pdf",
    )
    assert a == b


def test_ingested_at_differs_for_different_hashes() -> None:
    a = build_frontmatter(
        title="X",
        source_url=None,
        source_path="/x",
        source_hash="00000000ffff",
        fetched_via="pdf",
    )
    b = build_frontmatter(
        title="X",
        source_url=None,
        source_path="/x",
        source_hash="ffff00000000",
        fetched_via="pdf",
    )
    assert a != b


def test_ingested_at_capped_to_2099() -> None:
    fm = build_frontmatter(
        title="X",
        source_url=None,
        source_path="/x",
        source_hash="ffffffffffff",
        fetched_via="pdf",
    )
    assert "ingested_at: 2099-12-31\n" in fm
