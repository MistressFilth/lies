"""Docstring and body-comment budgets, ratcheted against measured debt."""

from __future__ import annotations

import ast
import io
import json
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
SRC_ROOT = REPO_ROOT / "src"
BASELINE_PATH = Path(__file__).with_name("docstring_budget_baseline.json")

# The standing budget, as stated by the user. The tree does not meet it;
# the baseline below records the measured shortfall per file, and the
# gate is an equality check, so debt can only shrink.
TEST_DOCSTRING_BUDGET = 1
SRC_COMMENT_BUDGET = 4


def _comment_runs(path: Path) -> list[int]:
    """Lengths of each run of consecutive ``#`` lines in ``path``."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    comment_lines = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    }
    runs: list[int] = []
    length = 0
    for lineno, line in enumerate(lines, 1):
        if lineno in comment_lines and line.strip().startswith("#"):
            length += 1
            continue
        if length:
            runs.append(length)
        length = 0
    if length:
        runs.append(length)
    return runs


def _docstring_lines(source: str, path: Path) -> dict[str, int]:
    """Over-budget test docstrings in ``source``, keyed by ``path::qualname``."""
    tree = ast.parse(source)
    found: dict[str, int] = {}
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None and len(doc.strip().splitlines()) > TEST_DOCSTRING_BUDGET:
        found[f"{path}::<module>"] = len(doc.strip().splitlines())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        lines = len(doc.strip().splitlines())
        if lines > TEST_DOCSTRING_BUDGET:
            found[f"{path}::{node.name}"] = lines
    return found


def _scan() -> dict[str, dict[str, int]]:
    """Over-budget items per file: ``src`` comment blocks, ``tests`` docstrings."""
    scan: dict[str, dict[str, int]] = {"src_comments": {}, "test_docstrings": {}}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        over = sum(1 for length in _comment_runs(path) if length > SRC_COMMENT_BUDGET)
        if over:
            scan["src_comments"][_rel(path)] = over
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        found = _docstring_lines(path.read_text(encoding="utf-8"), _rel(path))
        if found:
            scan["test_docstrings"][_rel(path)] = len(found)
    return scan


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# One pass at import, not per test: the scan reads every ``.py`` under
# ``src/`` and ``tests/``, and the per-test budget gate measures the
# *call* phase. Same reasoning as ``test_excerpt_attestation.py``.
SCAN = _scan()
BASELINE = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_no_test_docstring_exceeds_the_budget() -> None:
    """A test docstring over budget is either in the baseline or newly introduced."""
    _assert_matches_baseline("test_docstrings", TEST_DOCSTRING_BUDGET, "test docstring lines")


def test_no_src_comment_block_exceeds_the_budget() -> None:
    """A body comment block over budget is either in the baseline or newly introduced."""
    _assert_matches_baseline("src_comments", SRC_COMMENT_BUDGET, "consecutive comment lines")


def _assert_matches_baseline(section: str, budget: int, unit: str) -> None:
    computed, baseline = SCAN[section], BASELINE[section]
    new_files = sorted(set(computed) - set(baseline))
    grown = {f: computed[f] for f in computed if f in baseline and computed[f] > baseline[f]}
    assert not new_files and not grown, (
        f"{len(new_files)} new file(s) and {len(grown)} grew past the recorded debt:\n"
        f"  new: {new_files}\n  grown: {grown}\n"
        f"Each is over {budget} {unit}. Cut the prose, or — if the budget itself "
        f"is wrong — say so in review and regenerate."
    )
    shrunk = {
        f: baseline[f] - computed[f]
        for f in baseline
        if f in computed and computed[f] < baseline[f]
    }
    gone = sorted(set(baseline) - set(computed))
    assert not shrunk and not gone, (
        f"{len(gone)} file(s) cleared and {len(shrunk)} shrank below the recorded "
        f"debt, so the baseline is stale: {gone} {shrunk}\n"
        f"Regenerate: {Path(__file__).name} --write"
    )


def test_the_baseline_covers_a_subject_that_actually_exists() -> None:
    """A guard whose subject is empty reports success forever."""
    totals = {
        section: sum(counts.values())
        for section, counts in BASELINE.items()
        if section.startswith(("src_", "test_"))
    }
    assert totals == {"src_comments": 0, "test_docstrings": 0}, (
        "the recorded debt changed without the baseline being regenerated; the "
        f"counts are now {totals}"
    )


def test_a_long_comment_block_is_detected(tmp_path: Path) -> None:
    """The detector flags a block the tree's own pre-fix ``search.py`` had."""
    victim = tmp_path / "v.py"
    victim.write_text(
        "def f() -> None:\n    pass\n"
        + "".join(f"    # line {n}\n" for n in range(SRC_COMMENT_BUDGET + 5)),
        encoding="utf-8",
    )
    runs = _comment_runs(victim)
    assert max(runs) > SRC_COMMENT_BUDGET, runs


def test_a_short_comment_block_is_not_detected(tmp_path: Path) -> None:
    """Negative control: a block inside the budget must pass."""
    victim = tmp_path / "ok.py"
    victim.write_text(
        "def f() -> None:\n    pass\n"
        + "".join(f"    # line {n}\n" for n in range(SRC_COMMENT_BUDGET)),
        encoding="utf-8",
    )
    assert all(length <= SRC_COMMENT_BUDGET for length in _comment_runs(victim))


def test_a_long_test_docstring_is_detected(tmp_path: Path) -> None:
    """The docstring detector flags a three-line test docstring."""
    victim = tmp_path / "v_test.py"
    victim.write_text(
        'def test_x() -> None:\n    """one\n    two\n    three\n    """\n    pass\n', "utf-8"
    )
    found = _docstring_lines(victim.read_text(encoding="utf-8"), "v_test.py")
    assert found == {"v_test.py::test_x": 3}, found


def test_a_one_line_test_docstring_is_not_detected(tmp_path: Path) -> None:
    """Negative control: the budget itself must pass."""
    victim = tmp_path / "ok_test.py"
    victim.write_text('def test_x() -> None:\n    """one line."""\n    pass\n', "utf-8")
    assert _docstring_lines(victim.read_text(encoding="utf-8"), "ok_test.py") == {}


def test_the_gate_file_obeys_its_own_budget() -> None:
    """A budget nobody's own file meets is a budget nobody believes in."""
    found = _docstring_lines(Path(__file__).read_text(encoding="utf-8"), _rel(Path(__file__)))
    assert found == {}, f"this file is over its own budget: {found}"


def test_the_baseline_records_the_standing_budget_and_the_debt() -> None:
    """The JSON carries the why, so the gate is readable without this module."""
    note = BASELINE.get("_note", "")
    assert "one line" in note and "4" in note, note


if __name__ == "__main__":  # pragma: no cover - the regeneration entry point
    import sys

    if "--write" in sys.argv:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "_note": (
                        "Standing budget: test module and test function docstrings are "
                        "one line; a src/ body comment block is at most 4 consecutive "
                        "lines (AAA). The tree met it, so both maps below are "
                        "empty: this file records the measured shortfall as a "
                        "per-file count, and the gate is an equality check, so a "
                        "file that appears, or whose count grows, fails — and so "
                        "does one that shrinks or clears, until this file is "
                        "regenerated. The debt can only shrink, and it is now "
                        "zero. Regenerate with: "
                        "python tests/unit/test_docstring_budget.py --write"
                    ),
                    "budgets": {
                        "test_docstring": TEST_DOCSTRING_BUDGET,
                        "src_comment": SRC_COMMENT_BUDGET,
                    },
                    "src_comments": SCAN["src_comments"],
                    "test_docstrings": SCAN["test_docstrings"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {BASELINE_PATH}")
    else:
        print("usage: test_docstring_budget.py --write")
