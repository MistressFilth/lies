from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lies.wiki.layout import WikiLayout


def test_wiki_layout_init_registers_wiki_collection(tmp_path: Path) -> None:
    """WikiLayout.init registers ``wiki_<name>`` with qmd pointing at
    ``wiki.wiki_dir``. Idempotent on subsequent calls."""
    captured: list[dict] = []

    def fake_add_or_update(cwd, path, name, **kwargs):
        captured.append({"cwd": cwd, "path": path, "name": name, **kwargs})

    # Root basename drives the registered qmd collection name
    # (``Wiki.data_root_for(name) = .../<LIES_DATA_SUBDIR>/<name>``);
    # pick a root whose basename matches the assertion below so the
    # test stays valid under the derivation semantics introduced after
    # commit 1e6631f.
    root = tmp_path / "default"
    # Patch BEFORE bare init() so the unpatched helper does not fire
    # against the real qmd global index. The autouse _isolated_xdg
    # fixture scopes XDG to tmp_path, but qmd reads ~/.config/qmd/
    # globally for its index — patching isolates the test from that.
    with patch(
        "lies.wiki.layout.qmd_collection_add_or_update",
        side_effect=fake_add_or_update,
    ):
        WikiLayout(root).init()
        WikiLayout(root).init()  # idempotent: helper itself is idempotent

    assert any(
        c["name"] == "wiki_default" and Path(c["path"]).resolve() == (root / "wiki").resolve()
        for c in captured
    ), f"expected wiki_default registration at {(root / 'wiki').resolve()}; got {captured!r}"


def test_wiki_layout_derives_name_from_root(tmp_path: Path) -> None:
    """WikiLayout(name) is derived from root.name — no hard-coded default."""
    from lies.wiki.layout import WikiLayout

    layout = WikiLayout(tmp_path / "smoke_wiki")
    assert layout.name == "smoke_wiki"

    layout2 = WikiLayout(tmp_path / "any-name-123")
    assert layout2.name == "any-name-123"
