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

The vendored (third-party) citations are pinned the same way, from
:data:`VENDORED_SITES` below. Those live outside the repo — in the
installed ``fastmcp`` and ``anyio`` distributions — and an earlier
version of this file declined to pin them on the grounds that they are
"version-specific" and that "AGENTS.md already says so at the reference
site". That reasoning does not survive the ruling this branch was held
to: a note saying "check the installed file" is a deferral with no due
date, and it left six line numbers to decay silently. They are pinned by
the same contract as the in-repo ones — *the cited line must still
contain what the claim says it contains* — so a fastmcp or anyio bump
that moves the code turns this red instead of leaving ``AGENTS.md``
asserting something about a file nobody re-read. ``qmd.js`` is cited
too (``qmd.js:4008-4013``) and is deliberately still unpinned; it is
resolved through a node package's bundled ``dist/``, which is not
importable and may be absent on a host that has not run qmd, so there is
no robust way to locate it. That one is stated here rather than left
implicit.

A bump that leaves the content in place stays green, and that is
correct: the claim is about the code, not about the version string. The
installed versions are printed in the failure message so a red pin can
be triaged against the bump that caused it.

Two of the six spawn references are spelled as one span rather than
three backticked citations (``daemon.py:426/437/445``, and
``grounding.py:468`` and ``:545``), so the "is it still cited at all"
assertion is scoped to the singly-cited ones and the spans get their
own check.
"""

from __future__ import annotations

import importlib.util
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

from lies.mcp import grounding as mcp_grounding
from lies.qmd import access as qmd_access
from lies.qmd import capability as qmd_capability
from lies.qmd import daemon as qmd_daemon

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "AGENTS.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DOCS = (AGENTS, CHANGELOG)

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

# ---------------------------------------------------------------------------
# Vendored citations — lines inside the INSTALLED third-party distributions.
# ---------------------------------------------------------------------------
#
# Each row is ``(doc-spelling, distribution, module, first line, last line,
# required substring)``. The substring is what the claim in AGENTS.md says
# is on that line, so the pin survives any edit that does not move the code
# and turns red on any bump that does.
#
# Every one of these was read out of the installed files at the version
# named in ``_installed_versions`` below. The versions are *not* asserted:
# a bump that leaves the content in place is not a broken citation.
#
# `qmd.js` is intentionally absent — see the module docstring.

VENDORED_SITES: tuple[tuple[str, str, str, int, int, str], ...] = (
    # "an ``anyio.Lock`` binds its backend at construction": the field
    # declaration whose default_factory constructs the lock.
    (
        "fastmcp/client/client.py:228",
        "fastmcp",
        "fastmcp.client.client",
        228,
        228,
        "lock: anyio.Lock = field(default_factory=anyio.Lock)",
    ),
    # ...via ``:587``, where the state object holding that field is built.
    (
        ":587",
        "fastmcp",
        "fastmcp.client.client",
        587,
        587,
        "self._session_state = ClientSessionState()",
    ),
    # "wraps anything that is not ``httpx2.HTTPStatusError | MCPError``
    # into a bare ``RuntimeError``" — the wrap itself ...
    (
        ":248",
        "fastmcp",
        "fastmcp.client.client",
        248,
        248,
        'RuntimeError(f"Client failed to connect: {exception}")',
    ),
    # ... reached through ``_connection_failure`` on the connect path.
    (
        "fastmcp/client/client.py:1045-1048",
        "fastmcp",
        "fastmcp.client.client",
        1045,
        1048,
        "_connection_failure(exception)",
    ),
    # "``Client._disconnect`` decrements ``nesting_counter`` and returns
    # while it is still positive".
    (
        "fastmcp/client/client.py:1080-1084",
        "fastmcp",
        "fastmcp.client.client",
        1080,
        1084,
        "self._session_state.nesting_counter",
    ),
    # "an ``anyio.Lock`` binds its backend at construction".
    (
        "anyio/_core/_synchronization.py:164-168",
        "anyio",
        "anyio._core._synchronization",
        164,
        168,
        "get_async_backend().create_lock(fast_acquire=fast_acquire)",
    ),
)


def _vendored_source(module: str) -> Path:
    """Locate an installed module's source file without importing it.

    ``find_spec`` on a dotted name imports the *parent* package, which is
    all anyio needs (``anyio._core`` is lazy) and is cheap; the leaf is
    read as text and never executed here.
    """
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, (
        f"{module} is installed but has no readable source file, so its "
        "line-number citations cannot be pinned"
    )
    return Path(spec.origin)


def _installed_versions(distributions: tuple[str, ...]) -> str:
    parts = []
    for name in distributions:
        try:
            parts.append(f"{name} {version(name)}")
        except PackageNotFoundError:  # pragma: no cover - both are hard deps
            parts.append(f"{name} (not installed)")
    return ", ".join(parts)


def _docs_text() -> str:
    return "\n".join(doc.read_text(encoding="utf-8") for doc in DOCS)


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


@pytest.mark.parametrize(
    ("cited", "dist", "module", "first", "last", "required"),
    VENDORED_SITES,
    ids=[site[0] for site in VENDORED_SITES],
)
def test_vendored_citations_are_current(
    cited: str, dist: str, module: str, first: int, last: int, required: str
) -> None:
    """A fastmcp or anyio bump must turn these red, not falsify the doc.

    Same contract as the in-repo pins: the cited line still has to
    contain what the claim says it contains. Resolved from the installed
    distribution rather than a vendored copy, because there is no
    vendored copy — these citations are about whatever is on the host.
    """
    source = _vendored_source(module)
    lines = source.read_text(encoding="utf-8").splitlines()
    assert 1 <= first <= last <= len(lines), (
        f"{source} has {len(lines)} lines; the citation claims {first}-{last}. "
        f"Installed: {_installed_versions((dist,))}. Either the dependency was "
        "bumped and the citation is now stale, or the file on this host is "
        "not the one the claim was read from."
    )
    span = "\n".join(lines[first - 1 : last])
    assert required in span, (
        f"AGENTS.md cites {cited} for {required!r}, but {source.name}:{first}-{last} "
        f"is now:\n    {span!r}\nInstalled: {_installed_versions((dist,))}"
    )
    assert cited in _docs_text(), (
        f"no doc still cites {cited}; the pin above is testing a claim nobody makes any more"
    )
