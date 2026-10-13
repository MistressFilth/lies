"""Replace a docstring by ``path::qualname``, verifying the line count.

The docstring budget counts *every* line including blanks, and the repo's
line length is 100 (``pyproject.toml``), so a prose edit is easy to get
subtly wrong by hand. This rewrites the span, re-parses, and prints the
resulting count. Idempotent: a docstring already equal to the new text is
a no-op.

Usage::

    python tools/set_docstring.py <path> <qualname> <file-with-new-body>
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _find(tree: ast.AST, qualname: str) -> ast.AST:
    """Locate the node by dotted name, or by bare name as a fallback."""
    parts = qualname.split(".")
    nodes = [n for n in ast.walk(tree) if getattr(n, "name", None) == parts[-1]]
    if len(parts) == 1:
        if not nodes:
            raise SystemExit(f"no node named {qualname}")
        return nodes[0]
    for node in nodes:
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if isinstance(body, list) and node in body:
                cur = getattr(parent, "name", None)
                if cur and cur == ".".join(parts[:-1]):
                    return node
    if not nodes:
        raise SystemExit(f"no node named {qualname}")
    return nodes[0]


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    path, qualname, body_file = Path(argv[1]), argv[2], Path(argv[3])
    new_body = body_file.read_text(encoding="utf-8").strip("\n")
    if "\n" not in new_body:
        new_doc = f'"""{new_body}"""'
    else:
        new_doc = f'"""{new_body}\n"""'

    source = path.read_text(encoding="utf-8")
    node = _find(ast.parse(source), qualname)
    stmt = node.body[0]
    assert isinstance(stmt, ast.Expr), "first statement is not the docstring"
    assert isinstance(stmt.value, ast.Constant)

    current = ast.get_docstring(node, clean=False) or ""
    if current.strip() == new_body.strip():
        print(f"{path}::{qualname}: unchanged")
        return 0

    indent = " " * (stmt.col_offset)
    rendered = "\n".join((indent + line if line else "") for line in new_doc.splitlines())
    lines = source.splitlines(keepends=True)
    lines[stmt.lineno - 1 : stmt.end_lineno] = [rendered + "\n"]
    path.write_text("".join(lines), encoding="utf-8")

    node = _find(ast.parse(path.read_text(encoding="utf-8")), qualname)
    count = len((ast.get_docstring(node, clean=False) or "").strip().splitlines())
    print(f"{path}::{qualname}: {count} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
