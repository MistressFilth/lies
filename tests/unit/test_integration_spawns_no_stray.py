"""The integration suite must not leave a qmd daemon listening."""

from __future__ import annotations

import ast
from pathlib import Path

LIFECYCLE = Path(__file__).resolve().parents[1] / "integration" / "test_mcp_daemon_lifecycle.py"

# The one test in that file that must not touch qmd's daemon.
NO_QMD_TESTS = frozenset({"test_full_lifecycle"})


def _mcp_up_argvs(tree: ast.Module, func_name: str) -> list[list[str]]:
    """Every literal argv list starting ``["mcp", "up", ...]`` in one function."""
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == func_name
    )
    argvs: list[list[str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.List):
            continue
        parts: list[str] = []
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                parts.append(element.value)
            else:
                # ``wiki.name`` / ``str(port)`` are expressions; keep a
                # marker so the list still reads as one argv.
                parts.append("<expr>")
        if parts[:2] == ["mcp", "up"]:
            argvs.append(parts)
    return argvs


def test_the_lifecycle_test_file_is_where_we_think_it_is() -> None:
    assert LIFECYCLE.is_file(), f"{LIFECYCLE} vanished; re-derive this pin"


def test_lifecycle_up_invocation_carries_no_qmd() -> None:
    tree = ast.parse(LIFECYCLE.read_text(encoding="utf-8"))

    for name in sorted(NO_QMD_TESTS):
        argvs = _mcp_up_argvs(tree, name)
        assert argvs, (
            f"{name} no longer invokes `lies mcp up`; this pin is guarding "
            "a spawn that no longer happens, and the stray guard has to be "
            "re-derived rather than left passing"
        )
        for argv in argvs:
            assert "--no-qmd" in argv, (
                f"{name} invokes `lies mcp up` without --no-qmd: {argv!r}. "
                "That reaches ensure_qmd_daemon on the default 8181 and "
                "leaves a stray qmd daemon listening after the suite exits."
            )


def test_the_only_test_allowed_to_touch_qmd_names_why() -> None:
    """The excluded test must exist and must document its own privilege."""
    tree = ast.parse(LIFECYCLE.read_text(encoding="utf-8"))
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "test_down_leaves_the_qmd_daemon_running"
        ),
        None,
    )
    assert fn is not None, (
        "test_down_leaves_the_qmd_daemon_running is gone; NO_QMD_TESTS in "
        "this file exists to exempt exactly that one test"
    )
    assert (ast.get_docstring(fn) or "").strip(), (
        "the qmd-touching lifecycle test lost its docstring, which is what "
        "distinguishes it from the ones that must pass --no-qmd"
    )


def test_up_really_does_spawn_unless_no_qmd_is_passed() -> None:
    """Anchor the other end: ``--no-qmd`` is a real early return."""
    from lies.cli import operator

    source = Path(operator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "up"
    )
    guard = next(
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "no_qmd"
    )
    returned = [
        node
        for stmt in guard.body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Return) and node.value is None
    ]
    assert len(returned) == 1, (
        "operator.up's no_qmd guard does not return early; --no-qmd would "
        "still fall through to ensure_qmd_daemon and spawn the stray this "
        "pin exists to prevent"
    )

    # And it is the *last* thing before the ensure call, not a branch
    # that skips one arm of it.
    seg = ast.unparse(fn)
    guard_at = seg.index("if no_qmd:")
    ensure_at = seg.index("ensure_qmd_daemon(")
    assert guard_at < ensure_at, "operator.up no longer guards the ensure call with no_qmd"
