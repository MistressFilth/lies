"""Unit tests for ``WikiSettings.load`` (env > toml > default chain)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from lies.wiki_settings import DEFAULT_LANGUAGE, WikiSettings
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path):
    root = tmp_path / "wiki"
    root.mkdir()
    return make_wiki(name="lang-test", data_root=root)


def _write_toml(wiki, body: str) -> Path:
    p = wiki.config_root / "lies.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestWikiSettingsLoad:
    def test_env_wins_no_toml(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIES_LANG", "ja")
        assert WikiSettings.load(wiki).language == "ja"

    def test_env_wins_over_toml(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIES_LANG", "ja")
        _write_toml(wiki, '[settings]\nlang = "de"\n')
        assert WikiSettings.load(wiki).language == "ja"

    def test_env_empty_treated_as_unset(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIES_LANG", "")
        _write_toml(wiki, '[settings]\nlang = "de"\n')
        assert WikiSettings.load(wiki).language == "de"

    def test_env_whitespace_treated_as_unset(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIES_LANG", "   ")
        _write_toml(wiki, '[settings]\nlang = "de"\n')
        assert WikiSettings.load(wiki).language == "de"

    def test_toml_absent_defaults_silently(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE
        assert caught == []

    def test_toml_settings_lang(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, '[settings]\nlang = "de"\n')
        assert WikiSettings.load(wiki).language == "de"

    def test_toml_lang_stripped(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, '[settings]\nlang = "  de  "\n')
        assert WikiSettings.load(wiki).language == "de"

    def test_toml_missing_settings_table(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, "# toml with no settings table\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE
        assert caught == []

    def test_toml_missing_lang_key(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, "[settings]\nother_key = 1\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE
        assert caught == []

    def test_toml_empty_lang_warns_and_defaults(
        self, wiki, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, '[settings]\nlang = ""\n')
        with pytest.warns(UserWarning, match=r"lang is empty"):
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE

    def test_toml_non_string_lang_warns_and_defaults(
        self, wiki, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, "[settings]\nlang = 42\n")
        with pytest.warns(UserWarning, match=r"must be a string"):
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE

    def test_toml_unparseable_warns_and_defaults(
        self, wiki, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        _write_toml(wiki, "[settings\nlang = broken\n")
        with pytest.warns(UserWarning, match=r"not valid TOML"):
            settings = WikiSettings.load(wiki)
        assert settings.language == DEFAULT_LANGUAGE

    def test_default_fallback(self, wiki, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LIES_LANG", raising=False)
        assert WikiSettings.load(wiki).language == DEFAULT_LANGUAGE
