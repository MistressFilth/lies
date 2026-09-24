from dataclasses import is_dataclass

from lies.mcp.server import _CollisionVerdict


def test_collision_verdict_is_dataclass() -> None:
    assert is_dataclass(_CollisionVerdict)


def test_collision_verdict_overwrite() -> None:
    v = _CollisionVerdict(action="overwrite")
    assert v.action == "overwrite"
    assert v.new_slug is None


def test_collision_verdict_rename_with_slug() -> None:
    v = _CollisionVerdict(action="rename", new_slug="foo-v2")
    assert v.action == "rename"
    assert v.new_slug == "foo-v2"
