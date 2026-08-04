"""Liquid source builder tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies.builders.errors import BuilderFetchFailed
from lies.builders.liquid import LiquidBuilder, _resolve_render_cmd
from lies.collections.record import Collection


def _collection(tmp_path: Path, *, config: dict | None = None) -> Collection:
    now = datetime.now(tz=timezone.utc)
    return Collection(
        name="liquid-test",
        path=tmp_path,
        source="",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=now,
        updated_at=now,
        config=config or {},
    )


def test_resolve_render_cmd_loads_module_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_render_cmd returns the callable at module:attr."""
    from tests.fixtures import liquid_stub

    fn = _resolve_render_cmd("tests.fixtures.liquid_stub:render")
    assert fn is liquid_stub.render


def test_path_render_cmd_is_invoked_and_converted(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "liquid_path_stub.py"
    template = b"{{ product.title }}"
    context = {"product": {"title": "Hat"}}
    (tmp_path / "source.liquid").write_bytes(template)
    collection = _collection(
        tmp_path,
        config={"render_cmd": f"{fixture}:render", "context": context},
    )

    fn = _resolve_render_cmd(f"{fixture}:render")
    loaded_module = sys.modules[fn.__module__]
    assert fn is loaded_module.render

    with mock.patch(
        "lies.builders.liquid.PandocDaemon.convert",
        return_value=b"rendered markdown",
    ) as convert:
        docs = LiquidBuilder().build(tmp_path, collection=collection)

    loaded_module = sys.modules[fn.__module__]
    assert loaded_module.calls == [(template, context)]
    convert.assert_called_once_with(b"<html><body>rendered from path</body></html>", "html")
    assert docs[0].content == b"rendered markdown"


def test_resolve_render_cmd_rejects_missing_colon() -> None:
    with pytest.raises(BuilderFetchFailed, match="module:attr"):
        _resolve_render_cmd("tests.fixtures.liquid_stub")


def test_resolve_render_cmd_rejects_bare_module() -> None:
    with pytest.raises(BuilderFetchFailed, match="must be dotted or path"):
        _resolve_render_cmd("does_not_exist:render")


def test_resolve_render_cmd_rejects_missing_module() -> None:
    with pytest.raises(BuilderFetchFailed, match="cannot import"):
        _resolve_render_cmd("does.not.exist:render")


def test_resolve_render_cmd_rejects_non_callable_attr() -> None:
    with pytest.raises(BuilderFetchFailed, match="not callable"):
        _resolve_render_cmd("tests.fixtures.liquid_stub:NOT_A_THING")


def test_resolve_render_cmd_rejects_non_callable_value() -> None:
    """Attribute exists but is not callable (e.g. an int)."""
    with pytest.raises(BuilderFetchFailed, match="not callable"):
        _resolve_render_cmd("tests.fixtures.liquid_stub:NON_CALLABLE")


def test_source_read_oserror_is_wrapped(tmp_path: Path) -> None:
    source = tmp_path / "source.liquid"
    source.touch()
    with (
        mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
        pytest.raises(BuilderFetchFailed, match="cannot read"),
    ):
        LiquidBuilder().build(tmp_path, collection=_collection(tmp_path))
