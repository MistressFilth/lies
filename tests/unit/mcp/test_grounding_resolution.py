"""Unit tests for grounding.py's librarian model resolution.

Mirrors the resolver pattern in src/lies/orchestrator.py:101-154.
The MCP ground() path runs wiki-less; it resolves the librarian
model directly from $XDG_CONFIG_HOME/lies/providers.toml.

The two tests below are marked ``@pytest.mark.slow`` because
``grounding.ground()`` reaches ``resolve_model`` (via
``lies.providers.resolver``), which transitively imports
``anthropic`` + ``openai`` + ``pydantic_ai.models.*`` at module load
(~0.8s wall-clock on first call). Per the project budget-gate
rubric (option 5: MARK ``@pytest.mark.slow``), they run only with
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
here add integration coverage (``ground()`` → resolver →
``librarian_agent(model=...)``) but are not load-bearing for the
resolver contract — they are belt-and-suspenders.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_dir = tmp_path / "lies"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "providers.toml"
    monkeypatch.delenv("LIES_LIBRARIAN_MODEL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return cfg_file


@pytest.mark.slow
def test_ground_resolves_librarian_model_from_providers_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    from lies.mcp import grounding

    captured: dict[str, object] = {}

    def fake_librarian_agent(model=None):  # type: None
        captured["model"] = model

        # Return a sentinel that aborts run_sync if invoked.
        class _Sentinel:
            def run_sync(self, *args, **kwargs):
                raise AssertionError("run_sync should not run in this test")

        return _Sentinel()

    monkeypatch.setattr(grounding, "librarian_agent", fake_librarian_agent)

    from lies.mcp.grounding import ArchivistDigest

    digest = asyncio.run(
        grounding.ground(question="any", tag_expr=None, exclude_expr=None, top_k=1)
    )

    assert isinstance(digest, ArchivistDigest)
    assert captured.get("model") is not None
    # Model string passes through to pydantic-ai's Model factory.
    # ``resolve_model`` returns either a ``Model`` instance (for
    # ``anthropic_compatible`` / ``openai_compatible`` providers — the
    # ``AnthropicModel.__str__`` is just ``"AnthropicModel()"``) or a
    # bare string (for the built-in ``anthropic:`` prefix). Check both
    # shapes: ``model_name`` attribute on a Model, or substring on a
    # string.
    resolved_model = captured["model"]
    if isinstance(resolved_model, str):
        assert "MiniMax-M3" in resolved_model
    else:
        assert "MiniMax-M3" in str(getattr(resolved_model, "model_name", ""))


@pytest.mark.slow
def test_ground_raises_model_not_configured_when_providers_toml_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(tmp_path, monkeypatch)  # creates the dir, no providers.toml
    from lies.errors import ModelNotConfigured
    from lies.mcp import grounding

    with pytest.raises(ModelNotConfigured):
        asyncio.run(grounding.ground(question="any", tag_expr=None, exclude_expr=None, top_k=1))
