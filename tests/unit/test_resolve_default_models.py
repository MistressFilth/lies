"""Tests for src/lies/orchestrator.py:_resolve_default_models.

Pins the no-default-model contract: the resolver must raise
``ModelNotConfigured`` when no providers.toml exists AND no env
override is set, instead of silently falling back to a vendor
default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.errors import ModelNotConfigured
from lies.orchestrator import _resolve_default_models
from lies.wiki.wiki import Wiki


@pytest.fixture
def stub_wiki(tmp_path: Path) -> Wiki:
    """A wiki whose ``providers_path`` is under a tmp dir with no providers.toml."""
    return Wiki(
        name="resolve-default-models-test",
        data_root=tmp_path,
        config_root=tmp_path,
        cache_root=tmp_path,
        state_root=tmp_path,
        runtime_root=tmp_path,
    )


@pytest.fixture(autouse=True)
def _strip_providers_toml(stub_wiki: Wiki) -> None:
    """Remove the autouse-fixture-written providers.toml so these tests see ``load_providers_config -> None``.

    The shared ``_isolated_xdg`` autouse writes a stub providers.toml
    so the rest of the suite can build an Orchestrator. These tests
    intentionally exercise the unconfigured path — strip the file
    before each test runs.
    """
    pp = stub_wiki.providers_path
    if pp.exists():
        pp.unlink()


def test_raises_when_no_toml_and_no_env(monkeypatch: pytest.MonkeyPatch, stub_wiki: Wiki) -> None:
    """No providers.toml + no LIES_<AGENT>_MODEL → ModelNotConfigured, listing every slot."""
    from lies.providers.agents import AGENT_ROSTER
    from lies.providers.env import env_override

    # Ensure no env override survives from the shared conftest autouse.
    for name in AGENT_ROSTER:
        monkeypatch.delenv(f"LIES_{name.upper()}_MODEL", raising=False)

    # Confirm env_override respects the cleared env for every slot.
    for name in AGENT_ROSTER:
        assert env_override(name) is None

    with pytest.raises(ModelNotConfigured) as exc_info:
        _resolve_default_models(stub_wiki)

    msg = str(exc_info.value)
    # Every AGENT_ROSTER slot is named in the error so the operator
    # can see exactly which slots need configuration.
    for name in AGENT_ROSTER:
        assert name in msg, f"slot {name!r} missing from {msg!r}"


def test_resolves_from_env_overrides(monkeypatch: pytest.MonkeyPatch, stub_wiki: Wiki) -> None:
    """No providers.toml + LIES_<AGENT>_MODEL env → resolver returns those values verbatim."""
    from lies.providers.agents import AGENT_ROSTER

    for name in AGENT_ROSTER:
        monkeypatch.setenv(f"LIES_{name.upper()}_MODEL", f"stub:{name}")

    resolved = _resolve_default_models(stub_wiki)
    assert resolved == {name: f"stub:{name}" for name in AGENT_ROSTER}


def test_librarian_factory_requires_explicit_model() -> None:
    """Regression: ``librarian_agent()`` raises ``ModelNotConfigured`` when no model passed.

    Pins the no-default-model contract. Before this contract, the
    factory silently fell back to ``anthropic:claude-opus-4-7``,
    hiding misconfiguration from operators.
    """
    from lies.agents.librarian import librarian_agent
    from lies.errors import ModelNotConfigured

    with pytest.raises(ModelNotConfigured):
        librarian_agent()
