"""The read tool's bodies against the live qmd daemon, not against a stub.

Every other test for this module mocks the seam. That is the right default
— it is fast and it pins the contract — but it cannot tell you the body
the tool returns is *quotable*, which is the entire reason the library
branch moved off the CLI. A stub returns whatever the test author typed
into it, so a stub test passes just as happily against the line-numbered
CLI output this change removed.

So the assertions here are on properties of the real corpus:

- no ``N: `` line-number prefixes anywhere in the body,
- no ``qmd://…`` provenance header in front of it,
- a sentence quoted from the live document appears in the returned body
  byte-for-byte,
- and the largest documents come back whole, not truncated.

It lives under ``tests/integration/`` for two reasons, both recorded in
the repo's budget-gate rubric: it touches the live daemon, and a real
``get`` on a 320KB document costs more than the 0.15s unit budget.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration

# A sentence from ``claude_code/hooks.md`` as it stands in the live corpus
# (verified against qmd 2.5.3, index updated 2026-09-30). The plan quoted
# "hooks run in the order they are declared" for this assertion; that
# sentence is not in the document, so a test pinning it would have failed
# for a reason unrelated to what it was checking. This one is present and
# spans a heading-adjacent paragraph, so it also proves the markdown
# around it survives — line numbers would break it in two places.
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

    Asserted against the document **on disk**, not against a byte count
    or a remembered heading. Two earlier versions of this test each had a
    coupling that was not about truncation:

    - ``len(body) > 300_000`` fails the day anyone trims ``hooks.md``.
    - ``non_empty[-1].startswith("For troubleshooting…")`` fails the day
      anyone appends a section — the same defect wearing a different
      hat, and it was mine.

    The invariant that actually matters is that the body qmd returns is
    the file, end to end. Reading the file and comparing the returned
    body's head *and* tail against it proves non-truncation directly and
    survives any edit to the document that leaves it in the index. The
    >10_000 floor stays because it is the one number that is about qmd's
    cap rather than about the corpus: below it, ``multi_get`` would not
    have skipped this document at all, so the assertion would prove
    nothing about why ``get`` is used.
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
    """Read the indexed source file for a library page off disk.

    Library pages live at ``<library>/collections/<collection>/<page>``.
    Going to the filesystem rather than pinning literal text is what
    keeps the truncation assertion independent of the document's current
    contents — and, necessarily, resolves to whatever the live library
    root is at run time.

    Under ``tests/integration/`` the autouse XDG redirect points
    ``library_git_root()`` at a per-test fixture copy, which does not
    contain the corpus. This test therefore reads the *real* root rather
    than the redirected one.

    A missing document is a **failure, not a skip.** A skip would mean a
    host without a populated corpus reports this file green while the
    truncation invariant is never checked — the same "green for a reason
    other than the one it names" failure the tag-filter loops had. The
    whole point of comparing against the file on disk is that both sides
    must exist; if the corpus is gone, that is a real finding about the
    host, and it should say so rather than go quiet.
    """
    import os
    from pathlib import Path

    # Deliberately not ``library_git_root()``: under ``tests/integration/``
    # the autouse XDG redirect points that at a per-test fixture copy,
    # which does not contain the corpus. This assertion is about the live
    # document qmd indexed, so it must read that same document.
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
