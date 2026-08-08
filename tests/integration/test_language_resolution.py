"""Integration tests for the language resolution chain.

Gated on ``INTEGRATION=1``. No network required.

Run: ``INTEGRATION=1 uv run pytest tests/integration/test_language_resolution.py -v``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTEGRATION") != "1",
        reason="set INTEGRATION=1 to run integration tests",
    ),
]


def test_env_short_circuits_toml(tmp_path: Path) -> None:
    """LIES_LANG=ja wins over lies.toml [settings].lang = 'de'."""
    from lies.wiki_settings import WikiSettings
    from tests.conftest import make_wiki

    root = tmp_path / "wiki"
    root.mkdir()
    wiki = make_wiki(name="lang-int", data_root=root)
    toml_path = wiki.config_root / "lies.toml"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text('[settings]\nlang = "de"\n', encoding="utf-8")

    os.environ["LIES_LANG"] = "ja"
    try:
        settings = WikiSettings.load(wiki)
        assert settings.language == "ja"
    finally:
        del os.environ["LIES_LANG"]
