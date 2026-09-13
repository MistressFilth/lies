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

    root = tmp_path / "wiki"
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
