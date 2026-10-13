"""The prose ratchet reads Python only, so guard the README separately."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
SRC = REPO_ROOT / "src"

#: A backticked CamelCase name ending in ``Middleware``.
#:
#: The suffix-only rule keeps stdlib names (``PYTHONPATH``) out. The
#: failure it guards is the one that motivated it: the README documented a
#: ``PingMiddleware`` for releases after the class was deleted.
NAMED_MIDDLEWARE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*Middleware)`")


def undefined_middleware(declared: str, readme: str) -> list[str]:
    """Returns the middleware the README names that ``declared`` does not define.

    Args:
        declared: the concatenated source text searched for class
            declarations.
        readme: the README text searched for backticked middleware names.

    Returns:
        The named-and-undefined names, sorted and de-duplicated.
    """
    named = set(NAMED_MIDDLEWARE.findall(readme))
    return sorted(n for n in named if f"class {n}" not in declared)


def test_readme_names_no_middleware_that_src_does_not_define() -> None:
    """Every middleware the README names exists in src/."""
    declared = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    missing = undefined_middleware(declared, README.read_text(encoding="utf-8"))
    assert not missing, (
        f"README names middleware src/ does not define: {missing}. Either "
        f"the class is gone (delete the prose) or it is real and "
        f"unregistered (register it, or say why it must not be)."
    )


def test_the_guard_rejects_only_the_name_src_lacks() -> None:
    """A documented name with no matching class is flagged; a defined one is not."""
    declared = "class AbandonedCallMiddleware:\n    pass\n"
    readme = "Registers `AbandonedCallMiddleware` and `PingMiddleware`.\n"
    assert undefined_middleware(declared, readme) == ["PingMiddleware"]
