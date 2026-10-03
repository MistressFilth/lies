"""read() dispatches wiki page IDs to memory_service.read, library paths to the qmd daemon.

The library branch is where the F19 citation contract lives. A citation is
``[[slug]]: "verbatim quote from the cited span"``, so the body this tool
returns has to be the document and nothing else — no ``N: `` line-number
prefix, no ``qmd://path  #docid`` header. The CLI's ``qmd get`` cannot supply
that (``--no-line-numbers`` still leaves the header), which is why the branch
goes through the daemon's ``get`` with ``lineNumbers: false``.

The daemon answers with an EmbeddedResource *content block*, not a string:
``result.data`` is ``None`` and the text lives in
``result.content[].resource.text``. ``_Result`` below reproduces that exactly,
because reading ``.data`` is how a caller silently stores an empty body — the
failure this test suite exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest


@dataclass
class _Resource:
    """The ``resource`` payload of an EmbeddedResource block."""

    uri: str
    text: str


@dataclass
class _Embedded:
    """One EmbeddedResource content block, as the daemon really sends it.

    ``text`` on the block itself is absent and the body is one hop down at
    ``resource.text`` — while ``data`` on the result is ``None``. Modelling
    the real shape (rather than a convenient string-returning stub) is what
    makes these tests able to catch a ``.data`` read.
    """

    resource: _Resource


@dataclass
class _Text:
    """One TextContent block — qmd's notices, never a document body."""

    text: str


@dataclass
class _Result:
    """A ``CallToolResult``: ``data`` is None, the payload is in content."""

    content: list[Any] = field(default_factory=list)
    data: None = None
    is_error: bool = False


def _get_result(uri: str, text: str) -> _Result:
    """The shape ``daemon_tool('get', …)`` returns for one document."""
    return _Result(content=[_Embedded(resource=_Resource(uri=uri, text=text))])


def _fake_access(monkeypatch: pytest.MonkeyPatch, bodies: dict[str, str]) -> list[tuple]:
    """Patch the seam; record every ``(name, arguments)`` call.

    Keys are the ``file`` argument, so a body keyed by an unrequested path
    shows up as a lookup miss rather than as a silently wrong body.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        calls.append((name, dict(arguments)))
        if name != "get":
            raise AssertionError(f"read must not call {name!r}")
        text = bodies[arguments["file"]]
        return _get_result(f"qmd://{arguments['file']}", text)

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))
    return calls


# --- the library branch -------------------------------------------------


def test_library_read_returns_the_body_with_no_header_or_line_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library body is the document verbatim — nothing prepended."""
    from lies.mcp.read import read

    calls = _fake_access(monkeypatch, {"fastmcp/routing.md": "FastAPI routing reference"})

    out = read.fn(paths=["fastmcp/routing.md"])

    assert out["fastmcp/routing.md"] == "FastAPI routing reference"
    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "get"
    assert arguments["lineNumbers"] is False, "line numbers corrupt verbatim quotes"
    assert arguments["file"] == "fastmcp/routing.md"


def test_library_read_takes_the_text_from_the_content_block_not_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.data`` is None on this result; only the resource block has the body.

    Pinned because the two readings differ by exactly the whole document:
    a ``.data`` read stores ``""``, which reaches the synthesizer as an
    empty page and cites nothing.
    """
    from lies.mcp.read import read

    _fake_access(monkeypatch, {"typer/options.md": "typer option reference"})

    out = read.fn(paths=["typer/options.md"])

    assert out["typer/options.md"] != "", "a .data read stores an empty body"
    assert out["typer/options.md"] == "typer option reference"


def test_a_verbatim_quote_survives_the_read_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """F19's contract: a quoted span appears in the body byte-for-byte."""
    from lies.mcp.read import read

    body = (
        '---\ntitle: "Hooks"\n---\n\n'
        "The `if` field holds exactly one permission rule. There is no `&&`, `||`, "
        "or list syntax for combining rules.\n"
    )
    _fake_access(monkeypatch, {"claude_code/hooks.md": body})

    out = read.fn(paths=["claude_code/hooks.md"])
    quoted = out["claude_code/hooks.md"]

    assert "The `if` field holds exactly one permission rule." in quoted
    assert not re.search(r"^\d+: ", quoted, re.M), "no line-number prefixes"
    assert not quoted.startswith("qmd://"), "no provenance header"


def test_library_read_dispatches_one_get_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each requested path gets its own ``get``, keyed by that path."""
    from lies.mcp.read import read

    calls = _fake_access(
        monkeypatch,
        {
            "alpha/cli-plugin.md": "<body alpha>",
            "beta/manifest.md": "<body beta>",
        },
    )

    out = read.fn(paths=["alpha/cli-plugin.md", "beta/manifest.md"])

    assert out == {"alpha/cli-plugin.md": "<body alpha>", "beta/manifest.md": "<body beta>"}
    assert [c[1]["file"] for c in calls] == ["alpha/cli-plugin.md", "beta/manifest.md"]


# --- the daemon's own failures -------------------------------------------


def test_a_daemon_that_is_down_raises_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QmdDaemonUnavailable propagates; it is never logged and skipped.

    The skip-and-log path is right for a document qmd cannot find. It is
    wrong for a daemon that is not serving: the batch would come back empty
    and surface as ``ToolError("all reads failed")`` — a statement about
    the corpus that is really a statement about the process, and one the
    operator is the only person who can act on.
    """
    from lies.qmd.access import QmdDaemonUnavailable

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise QmdDaemonUnavailable("qmd daemon is not serving at http://127.0.0.1:8181")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(QmdDaemonUnavailable, match="not serving"):
        read.fn(paths=["alpha/a.md"])


def test_a_wedged_daemon_raises_with_its_log_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """QmdDaemonWedged propagates, carrying ``last_output``."""
    from lies.qmd.access import QmdDaemonWedged

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise QmdDaemonWedged("qmd daemon wedged on call to 'get'", last_output="phase 3")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(QmdDaemonWedged) as excinfo:
        read.fn(paths=["alpha/a.md"])
    assert excinfo.value.last_output == "phase 3"


def test_a_down_daemon_fails_a_mixed_batch_instead_of_returning_the_wiki_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wiki body beside a library body is not a partial success.

    Returning the wiki half would be the silent failure this branch exists
    to prevent: the caller asked for two pages and received one, with
    nothing in the response saying the other never arrived.
    """
    from lies.qmd.access import QmdDaemonUnavailable

    from lies.mcp.read import read

    class _Mem:
        def read(self, ids: list[str]) -> dict[str, str]:
            return {pid: f"<wiki {pid}>" for pid in ids}

    monkeypatch.setattr("lies.mcp.read._memory_service", lambda: _Mem())

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise QmdDaemonUnavailable("qmd daemon is not serving")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(QmdDaemonUnavailable):
        read.fn(paths=["page-abc123", "alpha/a.md"])


# --- the sync→async bridge ----------------------------------------------


def test_read_bridges_the_daemon_call_from_inside_a_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read.fn`` works when the caller already has a loop running.

    ``daemon_tool`` is async and ``_read_impl`` is sync. FastMCP runs sync
    handlers in a threadpool and pydantic-ai runs sync tools in an executor,
    so both real call sites have no loop — but ``lib_ask`` is async, and
    the ``asyncio.run``-inside-a-running-loop RuntimeError that shipped with
    ``ground()`` (#106) is exactly the bug this pins shut.
    """
    from lies.mcp.read import read

    _fake_access(monkeypatch, {"alpha/a.md": "<body alpha>"})

    out = _call_from_a_running_loop(lambda: read.fn(paths=["alpha/a.md"]))

    assert out == {"alpha/a.md": "<body alpha>"}


def test_a_down_daemon_is_not_swallowed_by_the_running_loop_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge must carry the daemon's failure back, not absorb it.

    Both daemon exceptions subclass ``RuntimeError``, and ``_run_blocking``
    contains an ``except RuntimeError`` — for ``asyncio.get_running_loop()``,
    not for the coroutine. That is a one-line change away from silently
    turning "the daemon is down" into a skipped page, so it is pinned
    here on the thread-and-loop path, which is the one that could plausibly
    lose it: the exception has to cross a thread boundary and come back
    out of ``Future.result()`` as the same type.
    """
    from lies.qmd.access import QmdDaemonUnavailable

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise QmdDaemonUnavailable("qmd daemon is not serving")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(QmdDaemonUnavailable, match="not serving"):
        _call_from_a_running_loop(lambda: read.fn(paths=["alpha/a.md"]))


def test_a_wedged_daemon_survives_the_running_loop_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same crossing, for the wedge type and its ``last_output`` tail.

    ``last_output`` is the only evidence of where the wedge happened, so
    a bridge that reconstructed the exception would destroy the one thing
    the operator needs.
    """
    from lies.qmd.access import QmdDaemonWedged

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise QmdDaemonWedged("qmd daemon wedged on call to 'get'", last_output="phase 3")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(QmdDaemonWedged) as excinfo:
        _call_from_a_running_loop(lambda: read.fn(paths=["alpha/a.md"]))

    assert excinfo.value.last_output == "phase 3", "the log tail was lost crossing the bridge"


# --- multi_get's two traps (Review Focus #1 and #4) ---------------------


def test_multi_get_resource_blocks_and_notice_blocks_are_distinguished() -> None:
    """A ``multi_get`` result carries notices alongside bodies; only the latter is a body.

    ``multi_get`` reports per-file problems as TextContent blocks — a
    ``[SKIPPED: …]`` line for a file over the 10KB cap, an ``Errors:`` block
    for an unresolvable entry. A caller that concatenates every block's text
    stores the notice as if it were the page.
    """
    from lies.mcp.read import _notices, _resource_texts

    result = _Result(
        content=[
            _Text(text="Errors:\nFile not found: claude_code/nope-zzz.md"),
            _Embedded(
                resource=_Resource(
                    uri="qmd://claude_code/claude-tag.md",
                    text="# Claude Tag\n",
                )
            ),
            _Text(
                text="[SKIPPED: claude_code/hooks.md - File too large (315KB > 10KB). "
                "Use 'qmd_get' with file=\"claude_code/hooks.md\" to retrieve.]"
            ),
        ]
    )

    assert _resource_texts(result) == ["# Claude Tag\n"]
    assert len(_notices(result)) == 2
    assert all("SKIPPED" in n or "not found" in n for n in _notices(result))


def test_read_takes_the_body_from_a_mixed_result_and_drops_the_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same discrimination, exercised through ``read`` rather than the helpers.

    ``test_multi_get_resource_blocks_and_notice_blocks_are_distinguished``
    calls the two private helpers directly, so nothing stopped a future
    ``read`` from bypassing them and concatenating every block's text —
    which is the exact defect the helpers exist to prevent. This goes
    through the public entry point with the mixed shape qmd actually
    returns, and asserts the notice text never reaches the body.
    """
    from lies.mcp.read import read

    notice = (
        "[SKIPPED: claude_code/hooks.md - File too large (315KB > 10KB). "
        "Use 'qmd_get' with file=\"claude_code/hooks.md\" to retrieve.]"
    )

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        return _Result(
            content=[
                _Text(text="Errors:\nFile not found: claude_code/nope-zzz.md"),
                _Embedded(
                    resource=_Resource(
                        uri="qmd://claude_code/claude-tag.md",
                        text="# Claude Tag\n\nbody that must survive verbatim",
                    )
                ),
                _Text(text=notice),
            ]
        )

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    out = read.fn(paths=["claude_code/claude-tag.md"])

    body = out["claude_code/claude-tag.md"]
    assert body == "# Claude Tag\n\nbody that must survive verbatim"
    assert "SKIPPED" not in body, "a notice was stored as the page body"
    assert "File not found" not in body, "a notice was stored as the page body"


def test_a_result_with_no_resource_block_is_skipped_not_stored_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notice-only results are skipped, and never stored as ``""``.

    The 10KB-cap skip is the case this exists for: a caller that reads
    text blocks instead of resource blocks sees nothing for a 315KB
    document and concludes the document is empty.

    Skipped, *not* raised — see
    ``test_a_notice_only_result_does_not_cancel_siblings_that_succeeded``.
    """
    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        if arguments["file"] == "alpha/ok.md":
            return _get_result(f"qmd://{arguments['file']}", "<body ok>")
        return _Result(
            content=[_Text(text="[SKIPPED: claude_code/hooks.md - File too large (315KB > 10KB).]")]
        )

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    out = read.fn(paths=["claude_code/hooks.md", "alpha/ok.md"])

    # The skipped path is absent rather than present-and-empty, and the
    # sibling that did produce a body survives — which is the whole point:
    # a notice-only result does not cancel the rest of the batch. The
    # skipped path moves to ``_missing`` so the caller can react to the
    # silent drop machine-readably.
    assert out == {
        "alpha/ok.md": "<body ok>",
        "_missing": ["claude_code/hooks.md"],
    }
    assert "claude_code/hooks.md" not in {k for k in out if k != "_missing"}


def test_a_notice_only_result_does_not_cancel_siblings_that_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty result must not discard the bodies already collected.

    The defect this pins: extraction sat *outside* the per-path ``try``,
    so a notice-only result raised after earlier paths had been written
    into ``out`` — and because the exception propagated, ``out`` was
    discarded entirely. One anomalous document silently cost the caller
    every good body in the batch.

    qmd signals "no body for this path" two ways (it raises for a
    missing document, and returns notices for a skipped one), and which
    one it uses is an implementation detail of its error signalling. The
    contract is one contract: per-path failures are skipped, whatever
    channel they arrive on.
    """
    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        if "notice" in arguments["file"]:
            return _Result(content=[_Text(text="[SKIPPED: too large]")])
        if "raises" in arguments["file"]:
            raise RuntimeError("Document not found")
        return _get_result(f"qmd://{arguments['file']}", f"<body {arguments['file']}>")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    out = read.fn(
        paths=[
            "alpha/good.md",
            "beta/notice-only.md",  # succeeded call, no body
            "gamma/raises.md",  # failed call
            "delta/also-good.md",
        ]
    )

    assert out == {
        "alpha/good.md": "<body alpha/good.md>",
        "delta/also-good.md": "<body delta/also-good.md>",
        "_missing": ["beta/notice-only.md", "gamma/raises.md"],
    }
    assert "beta/notice-only.md" not in {k for k in out if k != "_missing"}
    assert "gamma/raises.md" not in {k for k in out if k != "_missing"}


def test_a_page_over_the_multi_get_cap_comes_back_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """``read`` never batches through ``multi_get``.

    ``multi_get`` *skips* — does not truncate — any file over its 10KB
    default, and 1854 of this corpus's 5987 documents are over it, so a
    batched read would drop nearly a third of what a reader can ask for.

    This unit test only pins the tool choice; the size claim is measured
    against the live corpus by
    ``tests/integration/mcp/test_read_daemon.py::test_a_large_document_comes_back_whole``,
    which reads a 320KB document through the real daemon.
    """
    from lies.mcp.read import read

    big = "x" * 33_372
    calls = _fake_access(monkeypatch, {"claude_code/plugins.md": big})

    out = read.fn(paths=["claude_code/plugins.md"])

    assert out["claude_code/plugins.md"] == big
    assert [c[0] for c in calls] == ["get"], "read must not batch through multi_get"


# --- per-path failure handling (unchanged contract) ----------------------


def test_read_dispatches_wiki_page_ids_to_memory_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """wiki page IDs (page-…) route to memory_service.read()."""
    from lies.mcp.read import read

    seen: list[list[str]] = []

    class _Mem:
        def read(self, ids: list[str]) -> dict[str, str]:
            seen.append(list(ids))
            return {pid: f"<wiki body for {pid}>" for pid in ids}

    # `_memory_service` is a callable `() -> Any` per Task 9 contract; wrap
    # the stub instance in a lambda so the callable form is preserved.
    monkeypatch.setattr("lies.mcp.read._memory_service", lambda: _Mem())

    out = read.fn(paths=["page-abc123def456"])
    assert out == {"page-abc123def456": "<wiki body for page-abc123def456>"}
    assert seen == [["page-abc123def456"]]


def test_read_dispatches_mixed_wiki_and_library_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed paths split into wiki + library groups; both backends called."""
    from lies.mcp.read import read

    class _Mem:
        def read(self, ids: list[str]) -> dict[str, str]:
            return {pid: f"<wiki {pid}>" for pid in ids}

    monkeypatch.setattr("lies.mcp.read._memory_service", lambda: _Mem())
    _fake_access(
        monkeypatch,
        {"alpha/page.md": "<lib alpha>", "beta/page.md": "<lib beta>"},
    )

    out = read.fn(paths=["page-abc123", "alpha/page.md", "page-def456", "beta/page.md"])
    assert out == {
        "page-abc123": "<wiki page-abc123>",
        "alpha/page.md": "<lib alpha>",
        "page-def456": "<wiki page-def456>",
        "beta/page.md": "<lib beta>",
    }


def test_read_skips_failed_paths_and_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A document qmd cannot resolve is logged + dropped; siblings survive.

    The ``caplog`` assertion is the point of the test's name. Silently
    dropping a path is indistinguishable to the caller from the corpus
    not having it, which is how a retrieval gap becomes an unfalsifiable
    claim; the log line is what makes the drop visible to an operator.
    The path moves to ``out["_missing"]`` so the caller can react
    machine-readably, not just via log scraping.
    """
    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        if "missing" in arguments["file"]:
            raise RuntimeError("Document not found: alpha/missing.md")
        return _get_result(f"qmd://{arguments['file']}", f"<body {arguments['file']}>")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with caplog.at_level("WARNING", logger="lies.mcp.read"):
        out = read.fn(paths=["alpha/ok.md", "alpha/missing.md"])

    assert "alpha/ok.md" in out
    assert "alpha/missing.md" not in out
    assert out.get("_missing") == ["alpha/missing.md"], (
        "an unresolvable path must surface on the wire so the calling "
        "agent can see it, not just in the operator log"
    )
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("alpha/missing.md" in r.getMessage() for r in warnings), (
        f"the skipped path must be logged; got {[r.getMessage() for r in warnings]}"
    )


def test_read_partial_batch_surfaces_unresolved_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 20-path read with 3 unresolved returns 17 bodies + ``_missing=[3]``.

    The previous shape returned a 17-key dict and three
    ``log.warning`` lines that did not reach the agent. The
    missing list closes the silent-drop class — the caller
    asked for 20 documents and can now see exactly which 3
    were unresolvable, ordered, in input order.
    """
    from lies.mcp.read import read

    bodies = {f"alpha/p{i:02d}.md": f"<body {i}>" for i in range(20)}
    unresolved = {"alpha/p03.md", "alpha/p07.md", "alpha/p15.md"}

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        path = arguments["file"]
        if path in unresolved:
            raise RuntimeError(f"Document not found: {path}")
        return _get_result(f"qmd://{path}", bodies[path])

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    paths = sorted(bodies)
    out = read.fn(paths=paths)

    assert len(out) == 18, f"expected 17 bodies + 1 _missing key; got {len(out)}"
    body_keys = [k for k in out if k != "_missing"]
    assert sorted(body_keys) == sorted(p for p in paths if p not in unresolved)
    assert out["_missing"] == ["alpha/p03.md", "alpha/p07.md", "alpha/p15.md"], (
        "missing paths must appear in input order so a reader scanning "
        "the response can match them against the request"
    )


def test_read_full_failure_still_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch that yields *no* bodies still raises — the loud failure.

    ``ToolError("all reads failed")`` is the I-8 review's load-bearing
    requirement: the partial case is a soft signal; the all-fail case
    stays loud. A regression here would mean a partial failure was
    quietly turned into a successful empty read.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise RuntimeError(f"Document not found: {arguments['file']}")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(ToolError) as excinfo:
        read.fn(paths=["alpha/missing1.md", "alpha/missing2.md"])
    assert "all reads failed" in str(excinfo.value)


def test_read_empty_input_returns_empty_dict() -> None:
    """Empty paths list → empty dict, no calls."""
    from lies.mcp.read import read

    out = read.fn(paths=[])
    assert out == {}


def test_read_all_failures_raises_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every document failing raises ToolError."""
    from fastmcp.exceptions import ToolError

    from lies.mcp.read import read

    async def daemon_tool(name: str, arguments: dict[str, Any]) -> _Result:
        raise RuntimeError(f"Document not found: {arguments['file']}")

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=daemon_tool))

    with pytest.raises(ToolError, match="all reads failed"):
        read.fn(paths=["alpha/a.md", "beta/b.md"])


# --- helpers -------------------------------------------------------------


def _call_from_a_running_loop(fn: Any) -> Any:
    """Call ``fn()`` from a thread that already has an event loop running."""
    import asyncio
    import threading

    box: dict[str, Any] = {}

    async def main() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    def run() -> None:
        asyncio.run(main())

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
