"""Liquid source builder tests."""

from __future__ import annotations

import pytest

from lies.builders.errors import BuilderFetchFailed
from lies.builders.liquid import _resolve_render_cmd


def test_resolve_render_cmd_loads_module_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_render_cmd returns the callable at module:attr."""
    from tests.fixtures import liquid_stub

    fn = _resolve_render_cmd("tests.fixtures.liquid_stub:render")
    assert fn is liquid_stub.render


def test_resolve_render_cmd_rejects_missing_colon() -> None:
    with pytest.raises(BuilderFetchFailed, match="module:attr"):
        _resolve_render_cmd("tests.fixtures.liquid_stub")


def test_resolve_render_cmd_rejects_missing_module() -> None:
    with pytest.raises(BuilderFetchFailed, match="not callable|cannot"):
        _resolve_render_cmd("does_not_exist:render")


def test_resolve_render_cmd_rejects_non_callable_attr() -> None:
    with pytest.raises(BuilderFetchFailed, match="not callable"):
        _resolve_render_cmd("tests.fixtures.liquid_stub:NOT_A_THING")


def test_resolve_render_cmd_rejects_non_callable_value() -> None:
    """Attribute exists but is not callable (e.g. an int)."""
    with pytest.raises(BuilderFetchFailed, match="not callable"):
        _resolve_render_cmd("tests.fixtures.liquid_stub:NON_CALLABLE")
