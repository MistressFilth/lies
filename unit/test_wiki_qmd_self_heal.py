"""Self-heal migration: ``wiki_<name>`` qmd registration on first write/query.

When a wiki is created via ``WikiLayout.init``, the ``wiki_<name>``
collection is registered with qmd. Existing wikis (pre-0.22.0) skip
that step because ``WikiAlreadyExists`` blocks re-init; their wiki
collection is therefore missing from qmd's global index, and the wiki
pass returns zero hits forever. The self-heal hook detects that state
via a per-wiki sentinel file and registers the collection lazily on
the first write or the first MCP query.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lies.wiki.layout import (
    QMD_REGISTRATION_SENTINEL,
    WikiLayout,
    ensure_wiki_qmd_registered,
)
from lies.wiki.wiki import Wiki


def _make_wiki(tmp_path: Path, name: str = "selfheal") -> Wiki:
    """Build a :class:`Wiki` rooted at ``tmp_path/wiki/<name>``.

    Matches the conventions in ``tests/conftest.make_wiki`` but
    inlines here so the test stays self-contained and the wiki lives
    directly under ``tmp_path`` (no ``xdg/`` wrapper).
    """
    root = tmp_path / name
    (root / "raw").mkdir(parents=True)
    (root / "wiki").mkdir(parents=True)
    config_root = tmp_path / "config" / "lies" / name
    cache_root = tmp_path / "cache" / "lies" / name
    state_root = tmp_path / "state" / "lies" / name
    runtime_root = tmp_path / "runtime" / "lies" / name
    for p in (config_root, cache_root, state_root, runtime_root):
        p.mkdir(parents=True, exist_ok=True)
    return Wiki(
        name=name,
        data_root=root,
        config_root=config_root,
        cache_root=cache_root,
        state_root=state_root,
        runtime_root=runtime_root,
    )


def test_sentinel_path_lives_in_data_root_dot_lies(tmp_path: Path) -> None:
    """The sentinel lives at ``<data_root>/.lies/wiki_qmd_registered``.

    Co-locating with the catalog DB keeps all runtime artifacts under
    one gitignored directory, so the sentinel never accidentally gets
    staged.
    """
    w = _make_wiki(tmp_path)
    sentinel = w.data_root / ".lies" / QMD_REGISTRATION_SENTINEL
    assert sentinel.parent.name == ".lies"
    assert sentinel.name == "wiki_qmd_registered"


def test_ensure_wiki_qmd_registered_writes_sentinel_on_first_call(tmp_path: Path) -> None:
    """First call registers with qmd and atomically writes the sentinel.

    ``qmd_collection_add_or_update`` is patched to capture the call
    shape without touching the real qmd global index; the assertion
    pins the wiki name and path it was called with.
    """
    w = _make_wiki(tmp_path)
    assert not (w.data_root / ".lies" / QMD_REGISTRATION_SENTINEL).exists()

    captured: list[dict] = []

    def fake_add_or_update(cwd, path, name, **kwargs):  # type: ignore[no-untyped-def]
        captured.append({"cwd": cwd, "path": path, "name": name, **kwargs})

    with patch(
        "lies.wiki.layout.qmd_collection_add_or_update",
        side_effect=fake_add_or_update,
    ):
        result = ensure_wiki_qmd_registered(w)

    assert result is True
    assert len(captured) == 1
    assert captured[0]["name"] == "wiki_selfheal"
    assert Path(captured[0]["path"]).resolve() == (w.wiki_dir).resolve()
    assert Path(captured[0]["cwd"]).resolve() == w.data_root.resolve()
    assert (w.data_root / ".lies" / QMD_REGISTRATION_SENTINEL).is_file()


def test_ensure_wiki_qmd_registered_is_idempotent_via_sentinel(tmp_path: Path) -> None:
    """A second call after the sentinel exists does NOT re-invoke qmd.

    The sentinel prevents the subprocess cost on every write/query.
    The qmd helper itself is idempotent on path match, so even if the
    sentinel were missing the cost would be bounded — but skipping the
    call entirely is cheaper.
    """
    w = _make_wiki(tmp_path)
    call_count = {"n": 0}

    def fake_add_or_update(cwd, path, name, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1

    with patch(
        "lies.wiki.layout.qmd_collection_add_or_update",
        side_effect=fake_add_or_update,
    ):
        first = ensure_wiki_qmd_registered(w)
        second = ensure_wiki_qmd_registered(w)
        third = ensure_wiki_qmd_registered(w)

    assert first is True
    assert second is True
    assert third is True
    assert call_count["n"] == 1, (
        f"expected sentinel to prevent repeat qmd calls; got {call_count['n']}"
    )


def test_ensure_wiki_qmd_registered_does_not_write_sentinel_on_failure(tmp_path: Path) -> None:
    """When qmd registration raises, no sentinel is written and the
    warning is emitted; the next call retries.

    Failures must be non-fatal — the wiki write / answer must succeed
    regardless. The retry-on-next-call semantics is the contract: a
    transient qmd outage does not leave the wiki in a permanent
    unindexed state.
    """
    w = _make_wiki(tmp_path)

    def boom(cwd, path, name, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("qmd not on PATH")

    with patch(
        "lies.wiki.layout.qmd_collection_add_or_update",
        side_effect=boom,
    ):
        result = ensure_wiki_qmd_registered(w)

    assert result is False
    assert not (w.data_root / ".lies" / QMD_REGISTRATION_SENTINEL).exists(), (
        "sentinel must NOT be written when qmd registration fails; the next call needs to retry"
    )

    # Second call must retry (qmd now succeeds).
    with patch(
        "lies.wiki.layout.qmd_collection_add_or_update",
        side_effect=lambda *_a, **_kw: None,
    ):
        result2 = ensure_wiki_qmd_registered(w)

    assert result2 is True
    assert (w.data_root / ".lies" / QMD_REGISTRATION_SENTINEL).is_file()


def test_wiki_layout_init_writes_sentinel_for_new_wiki(tmp_path: Path) -> None:
    """``WikiLayout.init`` writes the sentinel on a fresh wiki so the
    first write/query skip the (already-done) qmd work.

    New wikis go through ``WikiLayout.init`` directly (no
    ``WikiAlreadyExists`` guard), so the sentinel is written there as
    part of the registration sequence. The two write paths must agree
    on the sentinel's location.
    """
    root = tmp_path / "fresh"
    with patch("lies.wiki.layout.qmd_collection_add_or_update") as add_or_update:
        WikiLayout(root).init()

    add_or_update.assert_called_once()
    assert (root / ".lies" / QMD_REGISTRATION_SENTINEL).is_file()
