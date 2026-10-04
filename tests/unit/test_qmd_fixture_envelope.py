"""The qmd fixture's cleanup must cover its setup, not just its yield.

``_seed_qmd`` registers a collection with qmd and *then* embeds it.
When the embed raises — and it does, on the CUDA VMM reservation
flake — the exception propagates out of the seeding loop, which on
this host is what left ``wiki_tag-filter-lib`` registered in the
live index.

The teardown was wrapped around the ``yield``, so it ran only on the
success path. Registration is a side effect that happens *before*
the yield, and nothing undid it when the yield was never reached.

The invariant is therefore an ordering property: cleanup covers setup.
That is worth its own name and its own test, because the version that
only had a ``try/finally`` around the yield looked correct and leaked.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest


class _Wiki:
    """The fixture's ``Wiki`` stand-in: the context manager only reads ``.name``."""

    def __init__(self, name: str = "tag-filter-lib") -> None:
        self.name = name
        self.data_root = Path("/nonexistent")
        self.wiki_dir = Path("/nonexistent")


def test_cleanup_runs_when_seeding_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seeding failure must not leave a collection registered.

    The exact shape of the CUDA flake: the collection is registered,
    the embed aborts, and the teardown that would have removed it is
    the code that never ran.
    """
    from tests.integration import test_tag_filter_end_to_end as mod

    removed: list[str] = []

    def _seed(wiki: Any) -> None:
        # Registered, then the embed died.
        raise RuntimeError("CUDA error: out of memory")

    def _remove(_cwd: Path, coll: str) -> None:
        removed.append(coll)

    monkeypatch.setattr(mod, "_seed_qmd", _seed)
    monkeypatch.setattr(mod, "qmd_collection_remove", _remove)
    # The removal-assertion reads the live collection list, which
    # would shell out to qmd. Nothing is registered here.
    monkeypatch.setattr(mod, "_registered_collections", lambda cwd: set())

    wiki = _Wiki()
    with contextlib.suppress(RuntimeError), mod._seeded_qmd_context(wiki):
        pytest.fail("the body must not run; seeding raised")

    assert "wiki_tag-filter-lib" in removed, (
        f"the collection was registered before the embed failed and must be "
        f"removed anyway; removals attempted: {removed!r}"
    )


def test_cleanup_runs_on_the_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success path still cleans up — the fix is not "always unseed"."""
    from tests.integration import test_tag_filter_end_to_end as mod

    removed: list[str] = []
    monkeypatch.setattr(mod, "_seed_qmd", lambda wiki: None)
    monkeypatch.setattr(mod, "qmd_collection_remove", lambda _cwd, c: removed.append(c))
    monkeypatch.setattr(mod, "_registered_collections", lambda cwd: set())

    with mod._seeded_qmd_context(_Wiki()) as wiki:
        assert wiki.name == "tag-filter-lib"

    assert "wiki_tag-filter-lib" in removed


def test_a_cleanup_failure_does_not_mask_the_seeding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original exception is what the reader needs.

    Teardown that raises over a seeding failure replaces the one
    diagnostic worth having — "the embed aborted on the CUDA
    reservation" — with "some collection is still registered".
    """
    from tests.integration import test_tag_filter_end_to_end as mod

    monkeypatch.setattr(
        mod, "_seed_qmd", lambda wiki: (_ for _ in ()).throw(RuntimeError("embed aborted"))
    )

    def _remove(_cwd: Path, coll: str) -> None:
        raise RuntimeError("removal failed too")

    monkeypatch.setattr(mod, "qmd_collection_remove", _remove)
    monkeypatch.setattr(mod, "_registered_collections", lambda cwd: set())

    with pytest.raises(RuntimeError, match="embed aborted"):
        with mod._seeded_qmd_context(_Wiki()):
            pytest.fail("the body must not run")
