import frontmatter

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
    assert 'title: "Getting Started"\n' in fm
    assert 'source_url: "https://example.com/start.html"\n' in fm
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


# --- YAML-escaping tests for user-supplied string fields -----------------
#
# Regression: build_frontmatter previously interpolated `title` (and the
# other user-supplied strings) directly into YAML, which broke the moment
# a real title contained a `:` or a newline (e.g. an HTML <title> lifted
# verbatim). These tests pin the quoting + escape + newline-stripping
# behavior so the regression does not come back.


def test_title_with_colon_parses_as_single_scalar() -> None:
    """Title containing ``:`` must round-trip through ``frontmatter.loads``."""
    fm = build_frontmatter(
        title="Claude Code: Getting Started",
        source_url="https://example.com/x",
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    parsed = frontmatter.loads(fm)
    assert parsed["title"] == "Claude Code: Getting Started"
    assert parsed["source_url"] == "https://example.com/x"


def test_title_with_newline_is_stripped_not_injected() -> None:
    """A newline in the title must be stripped so it cannot terminate the
    frontmatter block and inject a second top-level key. The injected text
    is folded into the title value, not parsed as a separate YAML key.
    """
    fm = build_frontmatter(
        title="Intro\nsource_url: https://evil",
        source_url="https://real.com/x",
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    # Frontmatter block must not contain a *bare* `source_url:` key
    # anywhere; only the quoted title may carry the literal text.
    assert "\nsource_url: https://evil\n" not in fm
    parsed = frontmatter.loads(fm)
    assert parsed["source_url"] == "https://real.com/x"
    # Newline is stripped (replaced with a space) and the rest stays.
    assert "\n" not in parsed["title"]
    assert "Intro source_url: https://evil" in parsed["title"]


def test_source_url_with_colon_parses_correctly() -> None:
    """A URL containing ``:`` (in the port or query) must parse cleanly."""
    fm = build_frontmatter(
        title="OK",
        source_url="https://example.com/page?q=1:2&r=3",
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    parsed = frontmatter.loads(fm)
    assert parsed["source_url"] == "https://example.com/page?q=1:2&r=3"


def test_title_with_quote_and_backslash_round_trips() -> None:
    """Embedded ``"`` and ``\\`` are escaped and round-trip."""
    fm = build_frontmatter(
        title='He said "hi" and used \\ backslashes',
        source_url=None,
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    parsed = frontmatter.loads(fm)
    assert parsed["title"] == 'He said "hi" and used \\ backslashes'


def test_default_title_from_slug_still_parses() -> None:
    """The slug → title default (used by ``render_mirror``) must still
    produce parseable YAML even when the slug is plain ASCII.
    """
    # The slug-to-title derivation lives in ``render_mirror``; here we
    # assert the frontmatter side of that path is well-formed for the
    # typical slug shape ``getting-started``.
    derived_title = "getting-started".replace("-", " ").title()
    fm = build_frontmatter(
        title=derived_title,
        source_url=None,
        source_path=None,
        source_hash="a3f0c8d4b1",
        fetched_via="web",
    )
    parsed = frontmatter.loads(fm)
    assert parsed["title"] == "Getting Started"
