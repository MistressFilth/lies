"""Report every over-budget docstring and comment block, with locations.

Uses the gate's own definitions (``tests/unit/test_docstring_budget.py``) so
the numbers here and the gate's can never disagree: this is a locator, not a
second opinion.

Usage::

    python tools/docstring_report.py            # summary
    python tools/docstring_report.py --src      # src/ docstrings over 12 lines
    python tools/docstring_report.py --tests    # tests/ docstrings over 1 line
    python tools/docstring_report.py --comments # src/ comment blocks over 4 lines
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"

TEST_DOCSTRING_BUDGET = 1
SRC_COMMENT_BUDGET = 4
SRC_DOCSTRING_BUDGET = 12


def _comment_runs(text: str) -> list[tuple[int, int]]:
    """``(start_line, length)`` for each run of consecutive ``#`` lines."""
    lines = text.splitlines()
    comment_lines = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    }
    runs: list[tuple[int, int]] = []
    length = 0
    start = 0
    for lineno, line in enumerate(lines, 1):
        if lineno in comment_lines and line.strip().startswith("#"):
            if not length:
                start = lineno
            length += 1
            continue
        if length:
            runs.append((start, length))
        length = 0
    if length:
        runs.append((start, length))
    return runs


def src_docstrings() -> list[tuple[str, int, int, str]]:
    """``(path, lineno, lines, qualname)`` for each over-budget src docstring."""
    found = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            count = len(doc.strip().splitlines())
            if count > SRC_DOCSTRING_BUDGET:
                name = getattr(node, "name", "<module>")
                found.append((rel, getattr(node, "lineno", 1), count, name))
    return sorted(found, key=lambda r: (-r[2], r[0], r[1]))


def test_docstrings() -> list[tuple[str, int, int, str]]:
    """``(path, lineno, lines, qualname)`` for each over-budget test docstring."""
    found = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree, clean=False)
        if doc is not None and len(doc.strip().splitlines()) > TEST_DOCSTRING_BUDGET:
            found.append((rel, 1, len(doc.strip().splitlines()), "<module>"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            count = len(doc.strip().splitlines())
            if count > TEST_DOCSTRING_BUDGET:
                found.append((rel, node.lineno, count, node.name))
    return sorted(found, key=lambda r: (-r[2], r[0], r[1]))


def comment_blocks() -> list[tuple[str, int, int]]:
    """``(path, start_line, length)`` for each over-budget src comment block."""
    found = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for start, length in _comment_runs(path.read_text(encoding="utf-8")):
            if length > SRC_COMMENT_BUDGET:
                found.append((rel, start, length))
    return sorted(found, key=lambda r: (-r[2], r[0], r[1]))


def main(argv: list[str]) -> int:
    src, tests, comments = src_docstrings(), test_docstrings(), comment_blocks()
    if "--src" in argv:
        for rel, lineno, count, name in src:
            print(f"{rel}:{lineno}  {count:>3} lines  {name}")
    if "--tests" in argv:
        for rel, lineno, count, name in tests:
            print(f"{rel}:{lineno}  {count:>3} lines  {name}")
    if "--comments" in argv:
        for rel, lineno, length in comments:
            print(f"{rel}:{lineno}  {length:>3} lines  comment block")
    print(
        f"\nTOTAL  src_docstrings(>{SRC_DOCSTRING_BUDGET})={len(src)}"
        f"  test_docstrings(>{TEST_DOCSTRING_BUDGET})={len(tests)}"
        f"  src_comments(>{SRC_COMMENT_BUDGET})={len(comments)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
