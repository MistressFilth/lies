"""The embed retry against a real qmd child, not a fake one.

The unit tests build their own ``CompletedProcess`` fake, so a fake whose
``stderr`` was the wrong type passed while every real embed raised
``TypeError: 'in <string>' requires string as left operand, not bytes``.
This pins the shape ``_run`` actually returns, against a real process.
"""

from __future__ import annotations

import pytest

from lies.qmd.cli import qmd_collection_add_if_missing, qmd_embed

pytestmark = pytest.mark.integration


def test_a_real_embed_uses_the_same_stderr_type_the_matcher_expects(
    tmp_path: object,
) -> None:
    """A real ``qmd embed`` round-trip must not raise on the marker match."""
    from pathlib import Path

    from lies.qmd.cli import _is_cuda_reservation_failure, _run

    root = Path(tmp_path)
    coll = root / "embedretry"
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "page.md").write_text("# Embed retry\n\nProse to embed.\n")
    qmd_collection_add_if_missing(root, coll, "embedretry")

    # Whatever the child does, `_run` decodes stderr to str, and the
    # matcher must accept that type.
    probe = _run(["embed", "-c", "no-such-collection-zzz"], cwd=root, timeout=120)
    assert isinstance(probe.stderr, str), (
        f"_run returned stderr as {type(probe.stderr).__name__}; the marker "
        f"match would raise TypeError on a real failure"
    )
    assert _is_cuda_reservation_failure(probe.stderr) is False, (
        "a missing collection must not be mistaken for the CUDA reservation abort"
    )

    # And the happy path still works end to end.
    qmd_embed(root, "embedretry", timeout=600)
