"""Server-shape tests for the library-mode MCP server surface.

Asserts the wire-shape contract after the read-side rewrite:
the retired wiki-shaped tools (query, answer, wiki_search,
wiki_read, wiki_changes, file_knowledge) are gone, replaced by
``ground`` (already shipped) and ``synthesize`` (Task 3 / commit 2).
The remaining tools (init_wiki, lint, ask_question, ask_ground_question,
reindex) keep their slots.

Each test introspects the live ``mcp`` object via FastMCP's internal
``_list_tools`` rather than spinning up a ``Client`` — the Client
path costs ~0.19s on this machine (well over the 0.15s pre-commit
hard limit) even though the introspection itself is sub-millisecond.
``_list_tools`` returns the same tool records the wire sees, so the
assertions exercise the same surface.

Note on imports: ``lies.providers`` transitively imports
``pydantic_ai.models.{openai,anthropic}`` — a one-shot ~0.8s cost.
The heavy modules are imported at module level (pytest collection
phase) so the cost is paid once for the whole file, NOT during any
test's call phase. Tests that need a heavier touch (FastMCP
``Client``) import their deps inside the function, which is cheap
because everything is already in ``sys.modules``.
"""

from __future__ import annotations

import pytest

from lies.mcp.server import mcp

# Heavy imports — paid at collection time, NOT during call phase.
# The resolver tests below need ``lies.providers`` and its
# re-exported ``load_providers_config`` to monkeypatch the
# package-level binding ``_resolve_librarian_model`` reads.
from lies import providers as _providers_pkg  # noqa: F401
from lies.providers.config import ProvidersConfig, ProviderSpec


async def test_server_registers_synthesize_tool() -> None:
    """The synthesize tool is registered after the rewrite.

    The retired wiki-shaped tools (query, answer, wiki_search,
    wiki_read, wiki_changes, file_knowledge) MUST NOT appear — the
    library-mode read surface replaces them with ground +
    synthesize + library:// resources.
    """
    tools = await mcp._list_tools()
    names = {t.name for t in tools}
    assert "synthesize" in names
    assert "query" not in names
    assert "answer" not in names
    assert "wiki_search" not in names
    assert "wiki_read" not in names
    assert "wiki_changes" not in names
    assert "file_knowledge" not in names


def test_resolve_librarian_model_reads_providers_toml(monkeypatch) -> None:
    """``_resolve_librarian_model`` resolves the librarian slot from providers.toml.

    Regression pin for the live-corpus ground-tool crash: when the
    daemon's user-level ``providers.toml`` carries a ``[agents]``
    section with a ``librarian = "minimax:MiniMax-M3[1m]"`` entry,
    ``_resolve_librarian_model`` must surface the configured model
    (or its resolved ``Model`` form) instead of raising
    ``ModelNotConfigured``. Mirrors ``_resolve_synthesizer_model`` —
    the same TOML-resolve flow that ships in ``src/lies/mcp/synth.py``.
    """
    from lies.mcp import server as server_mod

    fake_config = ProvidersConfig(
        providers={
            "minimax": ProviderSpec(
                name="minimax",
                type="anthropic_compatible",
                api_key_env="MINIMAX_API_KEY",
                base_url="https://api.minimax.io/anthropic",
            )
        },
        default_model="minimax:MiniMax-M3[1m]",
        agents={"librarian": "minimax:MiniMax-M3[1m]"},
    )

    # Patch ``lies.providers.load_providers_config`` (re-exported
    # name bound by ``lies.providers.__init__``) because
    # ``_resolve_librarian_model`` does
    # ``from lies.providers import load_providers_config`` —
    # that binding is captured at ``lies.providers`` package import
    # time. Patch ``lies.providers.resolver.resolve_model`` (source
    # module) because the function does
    # ``from lies.providers.resolver import resolve_model`` directly.
    monkeypatch.setattr(_providers_pkg, "load_providers_config", lambda _p: fake_config)
    monkeypatch.setattr(
        "lies.providers.resolver.resolve_model",
        lambda name, _cfg: f"resolved:{name}",
    )
    monkeypatch.delenv("LIES_LIBRARIAN_MODEL", raising=False)

    resolved = server_mod._resolve_librarian_model()
    assert resolved == "resolved:librarian"


def test_resolve_librarian_model_prefers_env_override(monkeypatch) -> None:
    """``LIES_LIBRARIAN_MODEL`` env var wins over providers.toml.

    Mirrors the synth path: ``env_override`` first (cheapest),
    providers.toml second. Pinned so an operator who sets the env
    var explicitly does not get silently overridden by a stale TOML.
    """
    from lies.mcp import server as server_mod

    monkeypatch.setenv("LIES_LIBRARIAN_MODEL", "openai:gpt-5")

    # If env_override wins, these should never be touched.
    def _must_not_call(*_args, **_kwargs):  # pragma: no cover — defensive
        raise AssertionError("providers.toml should not be read when env_override is set")

    monkeypatch.setattr("lies.providers.load_providers_config", _must_not_call)
    monkeypatch.setattr("lies.providers.resolver.resolve_model", _must_not_call)

    resolved = server_mod._resolve_librarian_model()
    assert resolved == "openai:gpt-5"


def test_resolve_librarian_model_raises_when_unconfigured(monkeypatch) -> None:
    """Missing providers.toml + missing env var raises ``ModelNotConfigured``.

    LIES does not silently fall back to a vendor-default model.
    The MCP boundary surfaces this as a clean ``ModelNotConfigured``
    rather than letting the bare ``librarian_agent()`` factory
    raise mid-dispatch.
    """
    from lies.errors import ModelNotConfigured
    from lies.mcp import server as server_mod

    monkeypatch.delenv("LIES_LIBRARIAN_MODEL", raising=False)
    monkeypatch.setattr(_providers_pkg, "load_providers_config", lambda _p: None)

    with pytest.raises(ModelNotConfigured, match="librarian model"):
        server_mod._resolve_librarian_model()


def test_mcp_ground_threads_resolved_model_into_ground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mcp_ground`` resolves the librarian model and threads it into ``ground()``.

    Regression pin for the live-corpus crash: the MCP wrapper was
    calling ``ground()`` without ever resolving the librarian model,
    so the bare ``librarian_agent()`` factory raised
    ``ModelNotConfigured`` on any tagged call. The fix is for the
    MCP wrapper to resolve the model first (same shape as
    ``_resolve_synthesizer_model``) and thread it through the new
    ``librarian_model=`` kwarg on :func:`ground`.

    The test patches both ``_resolve_librarian_model`` (returns a
    sentinel model string) and ``ground`` (captures the kwargs it
    was called with) so the wiring assertion is decoupled from any
    real provider resolution or F18 librarian dispatch.

    Implementation note: the tool function is called directly rather
    than via a FastMCP ``Client`` so the test stays well under the
    0.15s pre-commit hard limit. ``@mcp.tool`` registers the
    function without wrapping it in a coroutine, so calling
    ``mcp_ground(...)`` directly executes the same code path a real
    MCP wire call would land on.

    Sync (not ``async def``): ``mcp_ground`` is a sync tool handler
    that bridges to the now-async ``ground()`` via
    ``asyncio.run(...)``. Running this test inside an event loop
    would trip ``RuntimeError: asyncio.run() cannot be called from
    a running event loop`` — the test must therefore be sync.
    """
    from lies.mcp import server as server_mod

    sentinel_model = "minimax:MiniMax-M3[1m]"
    monkeypatch.setattr(
        server_mod,
        "_resolve_librarian_model",
        lambda: sentinel_model,
    )

    captured: dict[str, object] = {}

    async def fake_ground(**kwargs):
        captured.update(kwargs)
        from lies.mcp.grounding import ArchivistDigest

        return ArchivistDigest(
            question=kwargs["question"],
            tag_expr=kwargs["tag_expr"],
            exclude_expr=kwargs["exclude_expr"],
            citations=[],
            no_coverage=False,
            distinct_pages=0,
            searched_scope=[],
            no_library=False,
        )

    monkeypatch.setattr(server_mod, "ground", fake_ground)

    result = server_mod.mcp_ground(
        question="any question",
        tag_expr="wiki",
        top_k=3,
    )

    # The resolved model must reach ``ground()`` via the new kwarg.
    assert captured.get("librarian_model") == sentinel_model
    assert captured.get("question") == "any question"
    assert captured.get("tag_expr") == "wiki"
    assert captured.get("top_k") == 3
    # The MCP wire envelope round-trips the digest as a dict.
    assert isinstance(result, dict)
    assert result["question"] == "any question"


def test_mcp_ground_surfaces_model_not_configured(monkeypatch) -> None:
    """``mcp_ground`` propagates a missing-model error at the wrapper boundary.

    When the operator has neither ``providers.toml`` nor
    ``LIES_LIBRARIAN_MODEL`` configured, the MCP wrapper's resolver
    raises ``ModelNotConfigured``. The wrapper-level raise moves the
    failure to the MCP boundary rather than letting the bare
    ``librarian_agent()`` factory raise mid-dispatch (confusing and
    out of position).

    Implementation note: the tool function is called directly rather
    than via a FastMCP ``Client`` so the test stays well under the
    0.15s pre-commit hard limit. The function call surfaces the
    exception verbatim — FastMCP's wire layer would wrap it in a
    ``ToolError`` (verified by the integration test in
    ``test_ground_tool.py``); that wrapping is FastMCP's concern,
    not ours.
    """
    from lies.errors import ModelNotConfigured
    from lies.mcp import server as server_mod

    def raise_unconfigured():
        raise ModelNotConfigured("test: no librarian model configured")

    monkeypatch.setattr(server_mod, "_resolve_librarian_model", raise_unconfigured)

    with pytest.raises(ModelNotConfigured, match="no librarian model"):
        server_mod.mcp_ground(
            question="any question",
            tag_expr="wiki",
        )
