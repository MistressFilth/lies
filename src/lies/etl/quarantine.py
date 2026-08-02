"""Per-doc quarantine — preserve failed docs for inspection.

Failed docs are copied (not moved) to
``<wiki>/.lies/poison/<collection>/<path>`` along with a sidecar
``.reason`` file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_POISON_DIR = ".lies/poison"


def _poison_root(wiki_root: Path, collection: str) -> Path:
    return wiki_root / _POISON_DIR / collection


def quarantine(wiki_root: Path, collection: str, path: str, reason: str) -> None:
    src = wiki_root / "raw" / collection / path
    if not src.exists():
        return
    dest = _poison_root(wiki_root, collection) / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    (dest.with_suffix(dest.suffix + ".reason")).write_text(reason, encoding="utf-8")


def list_quarantined(wiki_root: Path, collection: str) -> list[tuple[str, str]]:
    root = _poison_root(wiki_root, collection)
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith(".reason"):
            reason_path = p.with_suffix(p.suffix + ".reason")
            reason = reason_path.read_text(encoding="utf-8") if reason_path.exists() else ""
            out.append((str(p.relative_to(root)), reason))
    return out
