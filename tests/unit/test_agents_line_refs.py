"""AGENTS.md's ``file:line`` references, checked against the tree.

The branch shipped two stale in-repo references, and both times the
commit that *grew* the file left its own citation behind:
``daemon.py:658`` moved to ``686`` under ``bc3f3da``, and writing this
file's own fix moved ``access.py:441`` to ``442`` — the pin caught that
on its first run, and the edit was then rewritten to be line-neutral so
the citation never needed moving. A doc that cites
line numbers is a claim about a file, and it decays silently — nothing
in ``make check`` and nothing in CI reads a markdown backtick.

So the references are pinned the only way they can be: by what the
cited line must *contain*. The spawn-site claims are exact
(``_spawn_qmd_daemon(port=``), the lock-busy claims are exact
(``except QmdLockBusy``), and both survive any edit that does not move
the thing being cited — which is the point. A commit that grows
``daemon.py`` and forgets its citation now fails here.

Deliberately *not* pinned: the fastmcp and anyio citations. Those live
outside the repo, are version-specific, and AGENTS.md already says so
at the reference site. Pinning them would manufacture a second,
quieter kind of drift.

Two of the six spawn references are spelled as one span rather than
three backticked citations (``daemon.py:426/437/445``, and
``grounding.py:468`` and ``:545``), so the "is it still cited at all"
assertion is scoped to the singly-cited ones and the spans get their
own check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lies.mcp import grounding as mcp_grounding
from lies.qmd import access as qmd_access
from lies.qmd import capability as qmd_capability
from lies.qmd import daemon as qmd_daemon

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "AGENTS.md"

SPAWN = "_spawn_qmd_daemon(port="
LOCK_BUSY = "except QmdLockBusy"

# Citations AGENTS.md spells as one backticked reference each.
SINGLE_SITES: tuple[tuple[str, Path, str], ...] = (
    ("capability.py:105", Path(qmd_capability.__file__), SPAWN),
    ("access.py:441", Path(qmd_access.__file__), SPAWN),
    ("daemon.py:686", Path(qmd_daemon.__file__), SPAWN),
)

DAEMON = Path(qmd_daemon.__file__)
GROUNDING = Path(mcp_grounding.__file__)

# ``three paths (`daemon.py:426/437/445`)``
SLASH_TRIPLE = re.compile(r"three paths \(`daemon\.py:(\d+)/(\d+)/(\d+)`\)")
# ```grounding.py:468` and `:545```
GROUNDING_PAIR = re.compile(r"`grounding\.py:(\d+)` and `:(\d+)`")


def _agents_text() -> str:
    return AGENTS.read_text(encoding="utf-8")


def _line(path: Path, lineno: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert 1 <= lineno <= len(lines), f"{path} has {len(lines)} lines; cited {lineno}"
    return lines[lineno - 1]


@pytest.mark.parametrize(
    ("cited", "path", "required"), SINGLE_SITES, ids=lambda v: getattr(v, "name", v)
)
def test_single_cited_references_are_current(cited: str, path: Path, required: str) -> None:
    assert f"`{cited}`" in _agents_text(), f"AGENTS.md no longer cites {cited}"

    name, _, lineno = cited.rpartition(":")
    line = _line(path, int(lineno))
    assert required in line, (
        f"AGENTS.md cites {cited} for a port-threaded spawn, but that line "
        f"is now:\n    {line!r}\n"
        f"expected to contain {required!r}. Either the code moved or the "
        f"citation is stale — a commit that grew {name} left its own "
        f"reference behind, which is how this pin earns its keep."
    )


def test_slash_triple_spawn_reference_is_current() -> None:
    """``daemon.py:426/437/445`` is three citations in one backtick span."""
    match = SLASH_TRIPLE.search(_agents_text())
    assert match, "AGENTS.md no longer spells the ensure_qmd_daemon paths as one span"

    for lineno in match.groups():
        line = _line(DAEMON, int(lineno))
        assert SPAWN in line, (
            f"AGENTS.md cites daemon.py:{lineno} as a port-threaded spawn; "
            f"that line is now:\n    {line!r}"
        )


def test_grounding_lock_busy_pair_is_current() -> None:
    """``grounding.py:468`` and ``:545`` are ground()'s two QmdLockBusy arms."""
    match = GROUNDING_PAIR.search(_agents_text())
    assert match, "AGENTS.md no longer cites the grounding QmdLockBusy pair"

    for lineno in match.groups():
        line = _line(GROUNDING, int(lineno))
        assert LOCK_BUSY in line, (
            f"AGENTS.md cites grounding.py:{lineno} as a QmdLockBusy arm; "
            f"that line is now:\n    {line!r}"
        )


def test_the_pair_exhausts_every_lock_busy_arm() -> None:
    """The cited pair must *be* ground()'s QmdLockBusy arms, not a subset.

    AGENTS.md's claim is "Two mechanisms, two shapes, both live". Two
    arms are named, so if a third appears the claim is wrong and the
    doc has to say so. Checking the text instead of the line number
    would not work: both arms read ``except QmdLockBusy as exc:``
    identically, and that identity is exactly why a line-number
    citation is the only thing that can tell them apart.
    """
    match = GROUNDING_PAIR.search(_agents_text())
    assert match

    actual = {
        str(lineno)
        for lineno, text in enumerate(GROUNDING.read_text(encoding="utf-8").splitlines(), 1)
        if LOCK_BUSY in text
    }
    assert set(match.groups()) == actual, (
        f"AGENTS.md cites grounding.py:{match.group(1)} and :{match.group(2)}, "
        f"but grounding.py has QmdLockBusy arms at lines {sorted(actual)}"
    )
