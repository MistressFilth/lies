"""The read tool's bodies against the live qmd daemon, not against a stub.

A stub returns whatever the test author typed into it, so a stub test
passes against the line-numbered CLI output just as happily as against a
clean body. These assertions are on properties of the real corpus: no
``N: `` line-number prefixes, no ``qmd://…`` header, a sentence quoted
from the live document present byte-for-byte, and large documents whole.

Integration because it touches the live daemon and a real ``get`` on a
320KB document costs more than the 0.15s unit budget.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration

# Present in ``claude_code/hooks.md`` in the live corpus. Spans a
# heading-adjacent paragraph, so line numbers would break it in two places.
LIVE_QUOTE = "The `if` field holds exactly one permission rule."

_NUMBERED_LINE = re.compile(r"^\d+: ", re.M)


def test_a_quote_from_the_live_document_appears_in_the_returned_body() -> None:
    """F19's contract, checked against a body nobody typed into a test."""
    from lies.mcp.read import read

    out = read.fn(paths=["claude_code/hooks.md"])

    body = out["claude_code/hooks.md"]
    assert LIVE_QUOTE in body, "the live document no longer contains the quoted sentence"
    assert not _NUMBERED_LINE.search(body), "body carries line-number prefixes"
    assert not body.startswith("qmd://"), "body carries a qmd:// provenance header"
    assert body.lstrip().startswith("---"), "the document's own frontmatter should lead"


def test_a_large_document_comes_back_whole() -> None:
    """A body far past multi_get's 10KB cap is returned intact.

    Asserted against the file on disk, head and tail, so the assertion is
    about non-truncation rather than about this document's current size or
    last heading. The >10_000 floor is the one number about qmd's cap
    rather than the corpus: below it ``multi_get`` would not have skipped
    the document, so the test would prove nothing about why ``get`` is used.
    """
    from lies.mcp.read import read

    body = read.fn(paths=["claude_code/hooks.md"])["claude_code/hooks.md"]

    assert len(body) > 10_000, f"body must clear multi_get's 10KB cap; got {len(body)} chars"
    assert LIVE_QUOTE in body
    assert not _NUMBERED_LINE.search(body)

    on_disk = _live_document("claude_code/hooks.md")
    head = 2_000
    tail = 2_000
    assert body[:head] == on_disk[:head], (
        "the head of the returned body differs from the file on disk"
    )
    assert body[-tail:] == on_disk[-tail:], (
        "the tail of the returned body differs from the file on disk; "
        "the body was truncated or the index is stale"
    )


def _live_document(collection_and_page: str) -> str:
    """Read a library page off disk: ``<library>/collections/<collection>/<page>``.

    Reads the real root, not ``library_git_root()``: the autouse XDG
    redirect points that at a per-test fixture copy without the corpus,
    and this compares the body of the document qmd actually indexed.

    A missing document fails rather than skips — a skip would report this
    file green on a host with no corpus while the truncation invariant is
    never checked.
    """
    import os
    from pathlib import Path

    root = Path(os.path.expanduser("~")) / ".local" / "share" / "lies" / "library"
    page = root / "collections" / collection_and_page
    if not page.exists():
        pytest.fail(
            f"live library document not found: {page}\n"
            f"This test compares the daemon's body against the file on disk to "
            f"prove non-truncation. Both sides must exist: without the file "
            f"there is nothing to compare against, and skipping would leave the "
            f"truncation invariant silently untested on this host."
        )
    return page.read_text(encoding="utf-8")


def test_a_second_document_needs_no_special_handling() -> None:
    """The common case is unremarkable, which is the point."""
    from lies.mcp.read import read

    out = read.fn(paths=["claude_code/plugins.md"])

    body = out["claude_code/plugins.md"]
    assert body.startswith("---")
    assert len(body) > 25_000
    assert not _NUMBERED_LINE.search(body)


def test_a_batch_returns_one_body_per_path() -> None:
    """Several paths in one call: every one comes back keyed by its path."""
    from lies.mcp.read import read

    paths = ["claude_code/plugins.md", "claude_code/hooks.md"]
    out = read.fn(paths=paths)

    assert set(out) == set(paths)
    assert all(body.strip() for body in out.values()), "no body may come back empty"


def test_an_unresolvable_path_is_skipped_without_taking_its_siblings() -> None:
    """One bad path does not empty the batch — the documented per-path contract."""
    from lies.mcp.read import read

    out = read.fn(paths=["claude_code/plugins.md", "claude_code/no-such-doc-zzz.md"])

    assert "claude_code/plugins.md" in out
    assert "claude_code/no-such-doc-zzz.md" not in out
