import pytest
from pathlib import Path
from lies.library.slug import derive_slug, validate_slug


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Getting_Started.md", "getting-started"),
        ("API-ref.rst", "api-ref"),
        ("page.html", "page"),
        ("multi.dot.name.md", "multi-dot-name"),
        ("UPPER.html", "upper"),
        ("trailing_.md", "trailing"),
    ],
)
def test_derive_slug_from_stem(tmp_path: Path, filename: str, expected: str) -> None:
    src = tmp_path / filename
    src.write_text("")
    assert derive_slug(src) == expected


def test_derive_slug_rejects_invalid_derived_stems(tmp_path: Path) -> None:
    """Minor 39: post-validation catches derived stems that don't match ``_VALID_RE``.

    The previous implementation passed the derived string straight through
    without re-validating, so a source named ``-leading.md`` (stem
    ``-leading``, starts with dash) or ``.dotfile`` (stem is the empty
    string after the leading dot) silently produced an invalid slug
    that would later crash ``write_mirror``. Re-validate after the
    derivation.
    """
    from lies.library.slug import SlugError

    bad_path = tmp_path / "-leading.md"
    bad_path.write_text("")
    with pytest.raises(SlugError, match="invalid slug"):
        derive_slug(bad_path)

    empty_path = Path(".")  # Path('.') has stem '' → derived is '' → invalid
    with pytest.raises(SlugError, match="invalid slug"):
        derive_slug(empty_path)


def test_derive_slug_with_override(tmp_path: Path) -> None:
    src = tmp_path / "Original.html"
    src.write_text("")
    assert derive_slug(src, override="custom-name") == "custom-name"


@pytest.mark.parametrize("bad", ["", "../etc", "with space", "with/slash", "with.dot"])
def test_validate_slug_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(bad)


def test_validate_slug_accepts_normal() -> None:
    assert validate_slug("normal-slug") == "normal-slug"
    assert validate_slug("with_under") == "with_under"
