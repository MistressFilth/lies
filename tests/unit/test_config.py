from __future__ import annotations

import pytest

from lies.config import get_qmd_transport, get_qmd_url


def test_qmd_transport_defaults_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIES_QMD_TRANSPORT", raising=False)
    assert get_qmd_transport() == "http"


def test_qmd_transport_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_QMD_TRANSPORT", "stdio")
    assert get_qmd_transport() == "stdio"


def test_qmd_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIES_QMD_URL", raising=False)
    assert get_qmd_url() == "http://127.0.0.1:8181"


def test_qmd_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_QMD_URL", "http://127.0.0.1:9999")
    assert get_qmd_url() == "http://127.0.0.1:9999"
