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

    ``claude_code/hooks.md`` is ~320KB — well over the cap, and a
    document ``multi_get`` would skip outright.

    Asserted as *both ends* rather than a byte count. A floor of
    ``> 10_000`` alone is satisfied by a truncated read, and a floor of
    ``> 300_000`` (the document's size today) fails the moment anyone
    edits or splits ``hooks.md``, for a reason that has nothing to do
    with truncation. So: a modest floor proving the body is past the
    cap, plus the document's own last section, which is only present if
    the tail actually arrived.
    """
    from lies.mcp.read import read

    body = read.fn(paths=["claude_code/hooks.md"])["claude_code/hooks.md"]

    assert len(body) > 10_000, f"body must clear multi_get's 10KB cap; got {len(body)} chars"
    assert LIVE_QUOTE in body
    assert not _NUMBERED_LINE.search(body)
    # The final section of the live document. Proves the tail arrived,
    # which is what truncation would remove.
    assert "## Debug hooks" in body, "the document's last section is missing; body was truncated"
    non_empty = [line for line in body.splitlines() if line.strip()]
    assert non_empty[-1].startswith("For troubleshooting common issues"), (
        f"expected the document's closing prose; got {non_empty[-1][:80]!r}"
    )


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
