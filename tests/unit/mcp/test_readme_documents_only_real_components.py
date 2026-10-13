"""The prose ratchet reads Python only, so guard the README separately."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
SRC = REPO_ROOT / "src"

# ``PingMiddleware``, `CustomMiddleware`, ...
NAMED_MIDDLEWARE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*Middleware)`")
# A broader "any backticked CamelCase word" rule would trip on stdlib
# names (``PYTHONPATH``), so only the class-name suffix is checked. The
# failure it guards is the one that motivated it: the README documented a
# ``PingMiddleware`` for releases after the class was deleted.


def test_readme_names_no_middleware_that_src_does_not_define() -> None:
    """Every middleware the README names exists in src/."""
    declared = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    named = sorted(set(NAMED_MIDDLEWARE.findall(README.read_text(encoding="utf-8"))))
    assert not named, (
        f"README names middleware that src/ does not define: {named}. "
        f"Either the class is gone (delete the prose) or it is real and "
        f"unregistered (add it, or explain why it must not be)."
    )


def test_the_guard_would_catch_a_deleted_middleware(tmp_path: Path) -> None:
    """Negative control: the scan finds a name in a README-shaped file."""
    victim = tmp_path / "README.md"
    victim.write_text("The server registers `PingMiddleware` at startup.\n", "utf-8")
    assert NAMED_MIDDLEWARE.findall(victim.read_text(encoding="utf-8")) == [
        "PingMiddleware"
    ]