"""Every production ``raise QmdLockBusy`` carries ``lock_path`` and ``max_s``."""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src"

#: Keywords every raise site must pass. Both are load-bearing facts about
#: the contention rather than decoration; see the module docstring.
REQUIRED_KEYWORDS = frozenset({"max_s", "lock_path"})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


@functools.lru_cache(maxsize=1)
def _raise_sites() -> tuple[tuple[str, int, frozenset[str]], ...]:
    """Every ``raise QmdLockBusy(...)`` under ``src/``."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                if _dotted(node.exc.func) == "QmdLockBusy":
                    keywords = frozenset(kw.arg for kw in node.exc.keywords if kw.arg)
                    found.append((path.relative_to(SRC).as_posix(), node.lineno, keywords))
    return tuple(found)


def test_the_raise_sites_are_actually_found() -> None:
    """Zero matches would make every assertion below vacuously true."""
    sites = _raise_sites()
    assert len(sites) >= 3, (
        f"expected at least 3 raise QmdLockBusy sites under src/, found "
        f"{len(sites)}: {[s[0] for s in sites]}. Either the raisers moved "
        f"or this file is no longer guarding them."
    )
    # Also named, so the failure says which sites exist rather than just
    # how many.
    for rel, lineno, _ in sites:
        assert rel.endswith(".py"), f"unexpected site path {rel!r} at line {lineno}"


@pytest.mark.parametrize(
    ("rel", "lineno", "keywords"),
    _raise_sites(),
    ids=[f"{rel}:{lineno}" for rel, lineno, _ in _raise_sites()],
)
def test_every_raise_site_passes_both_keywords(
    rel: str, lineno: int, keywords: frozenset[str]
) -> None:
    """``max_s=`` and ``lock_path=`` are explicit at every production raise."""
    missing = sorted(REQUIRED_KEYWORDS - keywords)

    assert not missing, (
        f"{rel}:{lineno} raises QmdLockBusy without {missing}. `max_s` "
        f"defaults to 30.0, which matches no production budget (the CLI "
        f"waits 30 s, the query path 120 s), so omitting it reports the "
        f"wrong one. `lock_path` is the only record of which inode was "
        f"contended, and LIES_QMD_LOCK_PATH can override it, so a message "
        f"without it cannot be resolved afterwards."
    )


def test_no_production_raise_site_relies_on_the_default_max_s() -> None:
    """The ``30.0`` default in ``__init__`` matches no production raiser."""
    import lies.lock_errors as le

    exc = le.QmdLockBusy()
    assert exc.max_s == 30.0, (
        "QmdLockBusy's default max_s changed; re-derive whether any raise "
        "site can rely on it, and update this test's premise"
    )
    # No production site may produce an instance carrying the default by
    # omission: every raiser passes its own budget explicitly, and the
    # two budgets differ, so an instance with 30.0 from the query path
    # would be a lie the operator reads.
    assert exc.lock_path is None, (
        "a hand-built QmdLockBusy has no lock_path by design; if this now "
        "resolves one, the raise sites no longer need to pass it and this "
        "whole file is guarding nothing"
    )
