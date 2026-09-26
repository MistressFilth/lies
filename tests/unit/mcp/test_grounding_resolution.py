"""Unit tests for the MCP ``ground()`` path's librarian model resolution.

The MCP ``ground`` tool (``mcp_ground``) resolves the librarian model
eagerly via :func:`lies.mcp.server._resolve_librarian_model` before
invoking the async :func:`lies.mcp.grounding.ground` Python function.
The Python ``ground()`` function itself does NOT resolve — it accepts
a pre-resolved ``librarian_model=`` kwarg and routes through either the
unscoped/tagged fan-out fast-paths (which bypass the LLM round-trip
entirely) or the legacy F18 librarian dispatch. These tests therefore
exercise ``_resolve_librarian_model`` (the actual resolver) rather than
the Python ``ground()`` function — calling ``ground()`` no longer
exercises the resolver path because unscoped/tagged calls short-circuit
through the qmd fan-out.

The two tests below are marked ``@pytest.mark.slow`` because
``_resolve_librarian_model`` transitively imports ``anthropic`` +
``openai`` + ``pydantic_ai.models.*`` at module load (~0.8s wall-clock
on first call). Per the project budget-gate rubric (option 5: MARK
``@pytest.mark.slow``), they run only with
``uv run pytest --runslow``; the default ``make unit-test`` skips
them. The pre-commit ``test`` hook inherits ``make unit-test`` and
therefore never runs these locally. CI *does* run them via
``.github/workflows/ci.yml`` line 39:

    uv run pytest tests/ --runslow --tb=short -q -p no:cacheprovider

so a regression here surfaces in the CI check job (not in the local
pre-commit gate).

**Gate coverage for the lookup itself** lives in
``tests/unit/providers/test_resolver.py`` — that file's
``test_librarian_lookup_returns_anthropic_compatible_spec`` and
``test_lookup_env_var_overrides_toml`` exercise the same
``librarian`` → provider-spec path through
``lies.providers.config.resolve_agent_to_provider`` *without*
importing the full provider stack, so they stay below the 0.15s
hard limit and fail the local gate on a regression. The slow tests
here add integration coverage (``_resolve_librarian_model`` →
resolver → ``providers.toml``) but are not load-bearing for the
resolver contract — they are belt-and-suspenders.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME at ``tmp_path`` and return the providers.toml path.

    The conftest's autouse ``_isolated_xdg`` fixture seeds providers.toml
    under its own XDG root, but the integration conftest's
    ``_seed_librarian_model`` is scoped to ``tests/integration/`` and
    doesn't run here. We need to (a) re-pin XDG at this test's tmp_path
    so the resolver reads our fixture's TOML, (b) drop any leaked
    ``LIES_LIBRARIAN_MODEL`` env override (defensive — no autouse sets
    it under ``tests/unit/``, but a future autouse shouldn't quietly
    break this resolver test).
    """
    cfg_dir = tmp_path / "lies"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "providers.toml"
    monkeypatch.delenv("LIES_LIBRARIAN_MODEL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return cfg_file


@pytest.mark.slow
def test_resolve_librarian_model_reads_providers_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_librarian_model`` returns the resolved librarian model from TOML.

    Mirrors the resolver pattern in ``src/lies/orchestrator.py:101-154``.
    The MCP ``ground()`` path runs wiki-less; it resolves the librarian
    model directly from ``$XDG_CONFIG_HOME/lies/providers.toml`` via
    :func:`lies.mcp.server._resolve_librarian_model`.
    """
    cfg_file = _isolated_xdg(tmp_path, monkeypatch)
    cfg_file.write_text(
        'default_model = "minimax:MiniMax-M3[1m]"\n'
        "\n"
        "[providers.minimax]\n"
        'type = "anthropic_compatible"\n'
        'api_key_env = "MINIMAX_API_KEY"\n'
        'base_url = "https://api.minimax.io/anthropic"\n'
        "\n"
        "[agents]\n"
        'orchestrator = "minimax:MiniMax-M3[1m]"\n'
        'source_reader = "minimax:MiniMax-M3[1m]"\n'
        'page_writer = "minimax:MiniMax-M3[1m]"\n'
        'linter = "minimax:MiniMax-M3[1m]"\n'
        'query_synthesizer = "minimax:MiniMax-M3[1m]"\n'
        'enricher = "minimax:MiniMax-M3[1m]"\n'
        'repair = "minimax:MiniMax-M3[1m]"\n'
        'librarian = "minimax:MiniMax-M3[1m]"\n'
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key-not-real")

    from lies.mcp.server import _resolve_librarian_model

    resolved = _resolve_librarian_model()
    # ``resolve_model`` returns either a ``Model`` instance (for
    # ``anthropic_compatible`` / ``openai_compatible`` providers) or a
    # bare string (for the built-in ``anthropic:`` prefix). Check both
    # shapes: ``model_name`` attribute on a Model, or substring on a
    # string.
    if isinstance(resolved, str):
        assert "MiniMax-M3" in resolved
    else:
        assert "MiniMax-M3" in str(getattr(resolved, "model_name", ""))


@pytest.mark.slow
def test_resolve_librarian_model_raises_when_providers_toml_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_librarian_model`` raises ``ModelNotConfigured`` when TOML is missing.

    The conftest autouse ``_isolated_xdg`` seeds a providers.toml under
    its own XDG root; this fixture re-pins ``XDG_CONFIG_HOME`` at the
    test's empty tmp_path, so the resolver sees a genuinely absent
    providers.toml and surfaces ``ModelNotConfigured`` at the MCP
    boundary instead of mid-dispatch.
    """
    _isolated_xdg(tmp_path, monkeypatch)  # creates the dir, no providers.toml
    from lies.errors import ModelNotConfigured
    from lies.mcp.server import _resolve_librarian_model

    with pytest.raises(ModelNotConfigured):
        _resolve_librarian_model()
