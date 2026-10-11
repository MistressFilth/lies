"""Every production ``raise QmdLockBusy`` carries ``lock_path`` and ``max_s``.

Two facts about this exception are easy to get wrong at a raise site,
and both have been got wrong here:

- **``max_s`` belongs to the raiser, not to the class.** The CLI
  decorator polls for 30 s and the daemon query path for 120 s. The
  ``30.0`` default in ``__init__`` matches *no* production site, so a
  raiser that omits ``max_s`` silently reports the wrong budget — which
  is the error this branch's own 120 s query raiser exposed.

- **``lock_path`` is the only record of *which* inode was contended.**
  ``LIES_QMD_LOCK_PATH`` can override the path, so a message that omits
  it cannot be resolved by the operator afterwards.

``writer.py`` omitted ``lock_path`` while being the only raiser that
re-wraps one it caught, so the value was already in hand and thrown
away. That is the defect this file pins: a *structural* assertion over
the AST, so a fifth raise site inherits neither gap. It reads every
``raise QmdLockBusy`` under ``src/`` and requires both keywords. A
string search would not do — the class name also appears in the
definition, in docstrings, and in ``except`` clauses.

``tests/unit/qmd/test_qmd_lockbusy_writer.py`` pins the behaviour of the
re-raise itself, which a structural check cannot reach.
"""

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
    """Every ``raise QmdLockBusy(...)`` under ``src/``.

    Yields ``(relative path, lineno, keyword names)``. Cached: parsing
    every module in ``src/`` costs ~0.2 s, which is over the 0.15 s unit
    budget on its own, and the answer cannot change within a session.
    The caching is why the per-site test below takes keyword sets rather
    than AST nodes — an ``ast.Raise`` is not hashable.
    """
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
    """Zero matches would make every assertion below vacuously true.

    The same shape as a deleted test: a guard that cannot see its own
    subject reports success forever. So the count is asserted, and as a
    floor rather than an equality — adding a fifth raiser is progress
    and should not fail here, while deleting one should.
    """
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
    """The ``30.0`` default in ``__init__`` matches no production raiser.

    Asserted separately from the keyword check above so that raising or
    lowering the default is caught even if every raise site is corrected:
    a class whose default is a plausible-looking 30 s is the reason the
    omission went unnoticed in the first place.
    """
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
