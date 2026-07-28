"""File system harness capability, scoped to the wiki root."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def file_system(wiki_root: Path) -> Any:
    """Return a file system capability restricted to ``wiki_root``."""
    from pydantic_ai_harness.filesystem import FileSystem

    return FileSystem(root_dir=wiki_root)
