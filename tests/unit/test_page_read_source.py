from lies.query.synthesizer import PageRead


def test_page_read_source_required() -> None:
    p = PageRead(
        rel_path="wiki/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    assert p.source == "wiki"


def test_page_read_source_distinguishes_library_from_wiki() -> None:
    lib = PageRead(rel_path="lib/x.md", title="X", spans=[], source="library")
    wiki = PageRead(rel_path="wiki/x.md", title="X", spans=[], source="wiki")
    assert lib != wiki
