"""Compare two revisions' ``src/`` and ``tests/`` trees with all prose removed.

Prose here is exactly two things: ``#`` comments and docstrings.  Comments
never reach the AST, so comparing ASTs already ignores them; docstrings do
(they are ``Expr`` nodes holding a string constant), so they are dropped
explicitly.  Every position attribute is cleared, so a comment that moved a
line does not register as a difference.  What is left is the behaviour.

Usage::

    python tools/ast_identity.py <revision-a> <revision-b>

``<revision>`` is either a git rev (``46021a7``) or a directory.  Files are
matched by path relative to ``src/`` and ``tests/``.
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TREES = ("src", "tests")
_POSITION_FIELDS = (
    "lineno",
    "col_offset",
    "end_lineno",
    "end_col_offset",
    "type_comment",
)


class _DropDocstrings(ast.NodeTransformer):
    """Remove module/class/function docstrings, leave every other string."""

    def _strip(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return self.generic_visit(node)

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def _normalise(source: str) -> str:
    """AST dump with docstrings gone and every position cleared."""
    tree = ast.parse(source)
    tree = _DropDocstrings().visit(tree)
    ast.fix_missing_locations(tree)
    for node in ast.walk(tree):
        for field in _POSITION_FIELDS:
            if hasattr(node, field):
                setattr(node, field, None)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _files_in(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for tree in TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            found[path.relative_to(root).as_posix()] = path
    return found


def _materialise(rev: str) -> Path:
    """Check ``rev`` out into a temp dir, or return it if it is a directory."""
    candidate = Path(rev)
    if candidate.is_dir():
        return candidate
    tmp = Path(tempfile.mkdtemp(prefix="ast-identity-"))
    blob = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", rev, "--", *TREES],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(tmp)
    return tmp


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    a, b = _materialise(argv[1]), _materialise(argv[2])
    files_a, files_b = _files_in(a), _files_in(b)

    only_a = sorted(set(files_a) - set(files_b))
    only_b = sorted(set(files_b) - set(files_a))
    differing: list[str] = []
    for rel in sorted(set(files_a) & set(files_b)):
        if _normalise(files_a[rel].read_text(encoding="utf-8")) != _normalise(
            files_b[rel].read_text(encoding="utf-8")
        ):
            differing.append(rel)

    print(f"A = {argv[1]}   B = {argv[2]}")
    print(f"files compared : {len(set(files_a) & set(files_b))}")
    print(f"only in A      : {len(only_a)} {only_a or ''}")
    print(f"only in B      : {len(only_b)} {only_b or ''}")
    print(f"AST differences: {len(differing)} {differing or ''}")
    if only_a or only_b or differing:
        print("\nRESULT: DIFFERENT")
        return 1
    print("\nRESULT: IDENTICAL — no behavioural difference between the revisions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
