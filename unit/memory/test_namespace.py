from pathlib import Path

from lies.memory.namespace import WikiIdentity, memory_namespace


def test_namespace_stable_for_same_root(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    a = memory_namespace(target)
    b = memory_namespace(target)
    assert a == b
    assert "/" not in a
    assert not a.startswith("/")


def test_namespace_differs_for_different_roots(tmp_path: Path) -> None:
    a_root = tmp_path / "wiki_a"
    b_root = tmp_path / "wiki_b"
    a_root.mkdir()
    b_root.mkdir()
    assert memory_namespace(a_root) != memory_namespace(b_root)


def test_namespace_rejects_absolute_path_artifact() -> None:
    # Even if wiki_root is "/", the namespace must remain relative.
    ns = memory_namespace(Path("/"))
    assert not ns.startswith("/")
    assert "/" not in ns


def test_identity_carries_root_and_namespace(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    ident = WikiIdentity.from_root(target)
    assert ident.wiki_root == target.resolve()
    assert ident.namespace == memory_namespace(target)


def test_namespace_short_enough_for_prompt() -> None:
    target = Path("/tmp/some-deeply-nested-wiki-root")
    ns = memory_namespace(target)
    assert len(ns) <= 64
