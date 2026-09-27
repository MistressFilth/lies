"""read() dispatches wiki page IDs to memory_service.read, library paths to qmd_get."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_read_dispatches_library_paths_to_qmd_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """library paths route to qmd_get against library_git_root()."""
    from lies.mcp.read import read

    calls: list[tuple[Path, str]] = []

    def fake_qmd_get(cwd: Path, qmd_path: str, timeout: int = 60) -> str:
        calls.append((cwd, qmd_path))
        return f"<body of {qmd_path}>"

    monkeypatch.setattr("lies.mcp.read._qmd_get", fake_qmd_get)

    out = read.fn(paths=["alpha/cli-plugin.md", "beta/manifest.md"])
    assert out == {
        "alpha/cli-plugin.md": "<body of qmd://alpha/cli-plugin.md>",
        "beta/manifest.md": "<body of qmd://beta/manifest.md>",
    }
    assert len(calls) == 2
    assert all(qmd_path.startswith("qmd://") for _, qmd_path in calls)


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

    library_calls: list[str] = []

    def fake_qmd_get(cwd: Path, qmd_path: str, timeout: int = 60) -> str:
        library_calls.append(qmd_path)
        return f"<lib {qmd_path}>"

    class _Mem:
        def read(self, ids: list[str]) -> dict[str, str]:
            return {pid: f"<wiki {pid}>" for pid in ids}

    monkeypatch.setattr("lies.mcp.read._qmd_get", fake_qmd_get)
    # Callable wrapper preserves the `() -> Any` contract.
    monkeypatch.setattr("lies.mcp.read._memory_service", lambda: _Mem())

    out = read.fn(paths=["page-abc123", "alpha/page.md", "page-def456", "beta/page.md"])
    assert out == {
        "page-abc123": "<wiki page-abc123>",
        "alpha/page.md": "<lib qmd://alpha/page.md>",
        "page-def456": "<wiki page-def456>",
        "beta/page.md": "<lib qmd://beta/page.md>",
    }
    assert library_calls == ["qmd://alpha/page.md", "qmd://beta/page.md"]


def test_read_skips_failed_paths_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed read is logged + dropped; other paths still returned."""
    from lies.mcp.read import read

    def fake_qmd_get(cwd: Path, qmd_path: str, timeout: int = 60) -> str:
        if "missing" in qmd_path:
            raise FileNotFoundError("nope")
        return f"<body {qmd_path}>"

    monkeypatch.setattr("lies.mcp.read._qmd_get", fake_qmd_get)

    out = read.fn(paths=["alpha/ok.md", "alpha/missing.md"])
    assert "alpha/ok.md" in out
    assert "alpha/missing.md" not in out


def test_read_empty_input_returns_empty_dict() -> None:
    """Empty paths list → empty dict, no calls."""
    from lies.mcp.read import read

    out = read.fn(paths=[])
    assert out == {}


def test_read_all_failures_raises_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """All paths failing raises ToolError."""
    from fastmcp.exceptions import ToolError

    from lies.mcp.read import read

    def fake_qmd_get(cwd: Path, qmd_path: str, timeout: int = 60) -> str:
        raise FileNotFoundError(qmd_path)

    monkeypatch.setattr("lies.mcp.read._qmd_get", fake_qmd_get)

    with pytest.raises(ToolError, match="all reads failed"):
        read.fn(paths=["alpha/a.md", "beta/b.md"])
