"""Per-doc quarantine — preserve failed docs for inspection.

Failed docs are copied (not moved) to
``$XDG_STATE_HOME/lies/<wiki>/poison/<collection>/<path>`` along with
a sidecar ``.reason`` file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from lies.wiki.wiki import Wiki


def _poison_root(wiki: Wiki) -> Path:
    return wiki.poison_root


def quarantine(wiki: Wiki, collection: str, path: str, reason: str) -> None:
    src = wiki.data_root / "raw" / collection / path
    if not src.exists():
        return
    dest = _poison_root(wiki) / collection / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    (dest.with_suffix(dest.suffix + ".reason")).write_text(reason, encoding="utf-8")


def list_quarantined(wiki: Wiki, collection: str) -> list[tuple[str, str]]:
    root = _poison_root(wiki) / collection
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith(".reason"):
            reason_path = p.with_suffix(p.suffix + ".reason")
            reason = reason_path.read_text(encoding="utf-8") if reason_path.exists() else ""
            out.append((str(p.relative_to(root)), reason))
    return out
