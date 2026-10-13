"""AGENTS.md's ``file:line`` references, checked against the tree."""

from __future__ import annotations

import ast
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

# Files AGENTS.md names as the homes of a port-threaded spawn. The pin is
# content-only and the set is asserted exhaustively: a line-number pin
# turns red when a comment is added above the cited site — no behavioural
# change — and it cannot see a new spawn site added *below* the cited one.
CITE_SITES: tuple[tuple[str, Path], ...] = (
    ("capability.py", Path(qmd_capability.__file__)),
    ("access.py", Path(qmd_access.__file__)),
    ("daemon.py", Path(qmd_daemon.__file__)),
)

DAEMON = Path(qmd_daemon.__file__)
GROUNDING = Path(mcp_grounding.__file__)

# ``grounding.py:465`` and ``:535``
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
# ``qmd.js`` is intentionally absent: it resolves through a node package's
# bundled ``dist/``, which is not importable and may be absent on a host
# that has not run qmd.

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
    """Locate an installed module's source file without importing it."""
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


@pytest.mark.parametrize(("cited", "path"), CITE_SITES, ids=lambda v: getattr(v, "name", v))
def test_cited_spawn_sites_still_thread_the_port(cited: str, path: Path) -> None:
    """AGENTS.md names this file as a spawn site; it must still thread the port."""
    assert f"`{cited}`" in _agents_text(), f"AGENTS.md no longer cites {cited}"
    assert SPAWN in path.read_text(encoding="utf-8"), (
        f"AGENTS.md names {cited} as a port-threaded spawn site, but it now "
        f"has no `{SPAWN}` call. Either the code moved or the citation is stale."
    )


def test_the_cited_set_matches_the_files_that_thread_the_port() -> None:
    """The cited set is derived from the code, so a new site inherits the pin."""
    package = Path(qmd_daemon.__file__).parent
    threading = {p.name for p in package.glob("*.py") if SPAWN in p.read_text(encoding="utf-8")}
    assert threading == {name for name, _ in CITE_SITES}, (
        f"AGENTS.md cites {sorted(n for n, _ in CITE_SITES)} as the spawn "
        f"sites, but {sorted(threading)} actually call `{SPAWN}`"
    )


def test_ensure_qmd_daemon_has_the_three_spawn_paths_the_docs_name() -> None:
    """AGENTS.md says "three paths"; the function must still have three."""
    prose = " ".join(_agents_text().split())
    assert re.search(r"three\s+paths\s+in\s+`daemon\.py`'s\s+`ensure_qmd_daemon`", prose), (
        "AGENTS.md no longer names ensure_qmd_daemon's three spawn paths"
    )
    tree = ast.parse(DAEMON.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "ensure_qmd_daemon"
    )
    spawned = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_spawn_qmd_daemon"
    ]
    assert len(spawned) == 3, (
        f"AGENTS.md names three port-threaded spawn paths in ensure_qmd_daemon; "
        f"{DAEMON.name} now has {len(spawned)}"
    )
    assert all(any(kw.arg == "port" for kw in call.keywords) for call in spawned)


def test_grounding_lock_busy_pair_is_current() -> None:
    """The pair AGENTS.md cites is ground()'s two QmdLockBusy arms."""
    match = GROUNDING_PAIR.search(_agents_text())
    assert match, "AGENTS.md no longer cites the grounding QmdLockBusy pair"

    for lineno in match.groups():
        line = _line(GROUNDING, int(lineno))
        assert LOCK_BUSY in line, (
            f"AGENTS.md cites grounding.py:{lineno} as a QmdLockBusy arm; "
            f"that line is now:\n    {line!r}"
        )


def test_the_pair_exhausts_every_lock_busy_arm() -> None:
    """The cited pair must *be* ground()'s QmdLockBusy arms, not a subset."""
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
    """A fastmcp or anyio bump must turn these red, not falsify the doc."""
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
