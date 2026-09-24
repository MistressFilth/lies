"""Tests for ``lies.orchestrator._lint_log_title``.

The helper synthesizes the title written to ``wiki/log.md`` for a lint
pass. It dedupes + sorts the finding categories so the entry reads as
``lint | N findings (<cat1>, <cat2>, ...)`` (or ``lint | N findings``
when the report is empty).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from lies.orchestrator import _lint_log_title


def _finding(category: str) -> MagicMock:
    f = MagicMock()
    f.category = category
    return f


def test_lint_log_title_with_categories() -> None:
    report = MagicMock()
    report.findings = [
        _finding("missing_xref"),
        _finding("orphan"),
        _finding("missing_xref"),
    ]
    title = _lint_log_title(report)
    assert title == "lint | 3 findings (missing_xref, orphan)"


def test_lint_log_title_no_findings() -> None:
    report = MagicMock()
    report.findings = []
    title = _lint_log_title(report)
    assert title == "lint | 0 findings"
