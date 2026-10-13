"""The qmd fixture's cleanup must cover its setup, not just its yield."""

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
    """A seeding failure must not leave a collection registered."""
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
    """The original exception is what the reader needs."""
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
