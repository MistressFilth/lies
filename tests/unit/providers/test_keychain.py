"""Tests for src/lies/providers/keychain.py."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from lies.providers import keychain
from lies.providers.config import ProviderConfigError


@dataclass(frozen=True)
class _Spec:
    name: str
    api_key_envs: tuple[str, ...]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars set by default; tests opt in per-var."""
    for var in ("FAKE_KEY_A", "FAKE_KEY_B", "FAKE_KEY_C"):
        monkeypatch.delenv(var, raising=False)


def test_first_present_resolves(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "value-a"
    os.environ["FAKE_KEY_B"] = "value-b"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"))
    assert keychain.resolve_api_key(spec) == "value-a"


def test_first_missing_second_present(env: None) -> None:
    os.environ["FAKE_KEY_B"] = "value-b"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"))
    assert keychain.resolve_api_key(spec) == "value-b"
    assert keychain.chosen_env_name(spec) == "FAKE_KEY_B"


def test_empty_string_skipped(env: None) -> None:
    os.environ["FAKE_KEY_A"] = ""
    os.environ["FAKE_KEY_B"] = "value-b"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"))
    assert keychain.resolve_api_key(spec) == "value-b"


def test_whitespace_only_skipped(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "   "
    os.environ["FAKE_KEY_B"] = "value-b"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"))
    assert keychain.resolve_api_key(spec) == "value-b"


def test_all_unset_raises(env: None) -> None:
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"))
    with pytest.raises(ProviderConfigError) as exc:
        keychain.resolve_api_key(spec)
    msg = str(exc.value)
    assert "FAKE_KEY_A" in msg
    assert "FAKE_KEY_B" in msg


def test_chosen_name_after_resolve(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "value-a"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A",))
    keychain.resolve_api_key(spec)
    assert keychain.chosen_env_name(spec) == "FAKE_KEY_A"


def test_chosen_name_before_resolve_raises(env: None) -> None:
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A",))
    with pytest.raises(RuntimeError, match="before resolve_api_key"):
        keychain.chosen_env_name(spec)


def test_invalidate_clears_memo(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "value-a"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A",))
    keychain.resolve_api_key(spec)
    assert keychain.chosen_env_name(spec) == "FAKE_KEY_A"
    keychain.invalidate(spec)
    with pytest.raises(RuntimeError):
        keychain.chosen_env_name(spec)


def test_token_rotation_picks_new_value(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "old"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A",))
    assert keychain.resolve_api_key(spec) == "old"
    os.environ["FAKE_KEY_A"] = "new"
    assert keychain.resolve_api_key(spec) == "new"


def test_two_specs_independent_memo(env: None) -> None:
    os.environ["FAKE_KEY_A"] = "value-a"
    os.environ["FAKE_KEY_B"] = "value-b"
    spec_a = _Spec(name="alpha", api_key_envs=("FAKE_KEY_A",))
    spec_b = _Spec(name="beta", api_key_envs=("FAKE_KEY_B",))
    keychain.resolve_api_key(spec_a)
    keychain.resolve_api_key(spec_b)
    assert keychain.chosen_env_name(spec_a) == "FAKE_KEY_A"
    assert keychain.chosen_env_name(spec_b) == "FAKE_KEY_B"


def test_debug_log_emitted_on_each_resolve(env: None, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    os.environ["FAKE_KEY_A"] = "value-a"
    spec = _Spec(name="x", api_key_envs=("FAKE_KEY_A",))
    with caplog.at_level(logging.DEBUG, logger="lies.providers.keychain"):
        keychain.resolve_api_key(spec)
        keychain.resolve_api_key(spec)
        keychain.resolve_api_key(spec)
    records = [r for r in caplog.records if "resolved via" in r.getMessage()]
    assert len(records) == 3
    assert all("'x'" in r.getMessage() or "x" in r.getMessage() for r in records)
