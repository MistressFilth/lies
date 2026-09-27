"""collections_read() exposes live library registry to the librarian LLM."""

from __future__ import annotations

from typing import Any

import pytest


def _fake_registry(
    monkeypatch: pytest.MonkeyPatch, names_to_meta: dict[str, dict[str, Any]]
) -> None:
    """Patch library_collection_metas + library_collection_names to return canned registry."""
    from lies.library.registry import LibraryCollectionMeta

    metas = [
        LibraryCollectionMeta(
            name=name,
            source_url=kw.get("source_url", f"https://example.test/{name}"),
            tags=frozenset(kw.get("tags", [])),
            scope_keywords=frozenset(kw.get("scope_keywords", [])),
        )
        for name, kw in names_to_meta.items()
    ]
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(
        "lies.library.registry.library_collection_names", lambda: frozenset(names_to_meta)
    )


def test_collections_read_list_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """subcommand='list' returns one row per registered collection."""
    from lies.mcp.collections import collections_read

    _fake_registry(
        monkeypatch,
        {
            "alpha": {"tags": ["plugins"], "scope_keywords": ["plugin"]},
            "beta": {"tags": ["plugins"], "scope_keywords": ["manifest"]},
        },
    )
    out = collections_read(subcommand="list")
    assert isinstance(out, list)
    assert {row["name"] for row in out} == {"alpha", "beta"}
    assert all(row["tags"] == ["plugins"] for row in out)


def test_collections_read_tag_list_returns_tag_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """subcommand='tag_list' returns tag -> collections dict."""
    from lies.mcp.collections import collections_read

    _fake_registry(
        monkeypatch,
        {
            "alpha": {"tags": ["plugins", "cli"]},
            "beta": {"tags": ["plugins", "marketplace"]},
            "gamma": {"tags": ["lsp"]},
        },
    )
    out = collections_read(subcommand="tag_list")
    assert out["plugins"] == ["alpha", "beta"]
    assert out["cli"] == ["alpha"]
    assert out["marketplace"] == ["beta"]
    assert out["lsp"] == ["gamma"]


def test_collections_read_info_returns_single_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """subcommand='info' + name returns one row's full metadata."""
    from lies.mcp.collections import collections_read

    _fake_registry(
        monkeypatch,
        {
            "alpha": {
                "tags": ["plugins"],
                "scope_keywords": ["plugin", "cli"],
            },
        },
    )
    out = collections_read(subcommand="info", name="alpha")
    assert isinstance(out, dict)
    assert out["name"] == "alpha"
    assert out["tags"] == ["plugins"]
    # Sorted alphabetically (the implementation sorts for determinism).
    assert out["scope_keywords"] == ["cli", "plugin"]


def test_collections_read_info_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """subcommand='info' with unknown name raises ToolError."""
    from fastmcp.exceptions import ToolError

    from lies.mcp.collections import collections_read

    _fake_registry(monkeypatch, {"alpha": {}})
    with pytest.raises(ToolError, match="collection not registered"):
        collections_read(subcommand="info", name="nope")


def test_collections_read_empty_registry_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty registry returns empty list / empty dict for the right subcommands."""
    from lies.mcp.collections import collections_read

    _fake_registry(monkeypatch, {})
    assert collections_read(subcommand="list") == []
    assert collections_read(subcommand="tag_list") == {}


def test_collections_read_drops_qualifier_prefix_from_tag_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tags with c:/t: prefix are surfaced with the prefix stripped in tag_list.

    Mirrors ask's ``tag_list`` behavior: returns the bare tag, not the
    qualified atom. Callers that need the qualifier infer it from the
    collection row.
    """
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp.collections import collections_read

    metas = [
        LibraryCollectionMeta(
            name="alpha",
            source_url="https://example.test/alpha",
            tags=frozenset({"c:plugins", "t:linux"}),
        ),
        LibraryCollectionMeta(
            name="beta",
            source_url="https://example.test/beta",
            tags=frozenset({"c:plugins"}),
        ),
    ]
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: iter(metas))

    out = collections_read(subcommand="tag_list")
    # Tag-list strips the qualifier prefix to the bare atom.
    assert out["plugins"] == ["alpha", "beta"]
    assert out["linux"] == ["alpha"]
