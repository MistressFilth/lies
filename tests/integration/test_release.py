"""The CHANGELOG's release headers are a contract, not a formatting habit.

Three separate releases shipped as `## [0.43.0]` in one file, two
other released versions had no section at all, `Unreleased` sat
below two released sections, and `0.45.1` sat above `0.46.0`. Every
one of those is invisible to a reader skimming for their own
version, and none of them is caught by anything else in the repo.

The three duplicate headers were not duplicates: they were two
stray `## [Unreleased]` blocks, stranded mid-document by earlier
edits, that a later pass relabelled `0.43.0` rather than filing
under the release their content actually shipped in. So the two
properties checked here are the ones that would have caught it --
one header per version, and descending order.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_HEADER = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>[\d-]+))?$", re.M)


def _sections(text: str) -> list[tuple[str, str | None]]:
    return [(m.group("version"), m.group("date")) for m in _HEADER.finditer(text)]


def _sort_key(version: str) -> tuple[int, ...]:
    """Numeric parts, so 0.37.11 sorts above 0.37.4.

    A string compare puts 0.37.11 below 0.37.4, which is the order a
    reader would call wrong.
    """
    return tuple(int(p) for p in re.findall(r"\d+", version))


def test_every_release_version_appears_exactly_once() -> None:
    """No two sections share a version.

    Three `## [0.43.0]` headers is what a relabelled stray
    `## [Unreleased]` looks like, and it is the failure this file
    existed to allow.
    """
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    versions = [v for v, _ in _sections(text) if v != "Unreleased"]

    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    assert not duplicates, f"CHANGELOG.md repeats a release header: {duplicates}"


def test_releases_are_in_descending_version_order() -> None:
    """Newest first, so the top of the file is the newest release.

    ``Unreleased`` is the staging area and belongs at the very top,
    above every dated section.
    """
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    sections = _sections(text)

    assert sections, "no release headers found; the parse is wrong, not the file"
    assert sections[0][0] == "Unreleased", (
        f"`Unreleased` must be the first section; found {sections[0][0]!r}"
    )

    released = [v for v, _ in sections if v != "Unreleased"]
    for first, second in zip(released, released[1:]):
        assert _sort_key(first) >= _sort_key(second), (
            f"{second} is listed before {first}; releases run newest first"
        )


@pytest.mark.parametrize("version", ["0.38.0", "0.43.2", "0.45.0"])
def test_the_versions_the_branch_shipped_have_sections(version: str) -> None:
    """A version set in `pyproject.toml` gets a CHANGELOG section.

    Three releases on this branch set a version and wrote no section
    for it, so a reader upgrading through them sees nothing. Named
    explicitly rather than derived: deriving it would need the git
    history the test exists to stand in for.
    """
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    versions = [v for v, _ in _sections(text)]
    assert version in versions, f"{version} shipped without a CHANGELOG section"


def test_the_declared_version_has_a_section() -> None:
    """`pyproject.toml`'s version appears in the CHANGELOG.

    The two drift independently -- a version bump is one file and a
    changelog entry is another, and nothing connected them.
    """
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    current = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert current is not None, "could not read the version out of pyproject.toml"

    version = current.group(1)
    versions = [v for v, _ in _sections(text)]
    # Either it has its own released section, or it is still staged
    # under Unreleased. Both are correct; a bare bump with neither is
    # not.
    assert version in versions or _unreleased_mentions(text, version), (
        f"pyproject.toml says {version}; the CHANGELOG has no section for it "
        f"and Unreleased does not mention it"
    )


def _unreleased_mentions(text: str, version: str) -> bool:
    """True when the ``Unreleased`` body names ``version``."""
    match = _HEADER.search(text)
    assert match is not None
    start = match.end()
    nxt = _HEADER.search(text, start)
    body = text[start : nxt.start() if nxt else len(text)]
    return version in body
