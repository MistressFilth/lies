"""Unit tests for the merge_lint_reports module function."""

from __future__ import annotations

from lies.agents.linter import LintFinding, LintReport, LintSeverity
from lies.orchestrator import merge_lint_reports


def _finding(
    category: str,
    pages: list[str],
    message: str,
    *,
    safe_to_fix: bool = False,
) -> LintFinding:
    return LintFinding(
        severity=LintSeverity.MEDIUM,
        category=category,
        message=message,
        pages=pages,
        safe_to_fix=safe_to_fix,
    )


def _empty() -> LintReport:
    return LintReport(findings=[], report_markdown="")


def test_merge_shell_only_when_llm_empty() -> None:
    shell = LintReport(
        findings=[_finding("orphan", ["wiki/a.md"], "a has no inbound links", safe_to_fix=True)],
        report_markdown="",
    )
    merged, fallback = merge_lint_reports(shell, _empty())
    assert merged.findings == shell.findings
    assert fallback is None


def test_merge_union_when_disjoint() -> None:
    shell = LintReport(
        findings=[_finding("orphan", ["wiki/a.md"], "a orphan", safe_to_fix=True)],
        report_markdown="",
    )
    llm = LintReport(
        findings=[
            _finding(
                "contradiction",
                ["wiki/a.md", "wiki/b.md"],
                "a vs b",
                safe_to_fix=False,
            )
        ],
        report_markdown="",
    )
    merged, _ = merge_lint_reports(shell, llm)
    assert [f.category for f in merged.findings] == ["orphan", "contradiction"]


def test_merge_dedup_same_key() -> None:
    """Same (category, pages, message) collapses; shell wins on safe_to_fix."""
    key_finding = _finding("orphan", ["wiki/a.md"], "a orphan", safe_to_fix=True)
    shell = LintReport(findings=[key_finding], report_markdown="")
    llm = LintReport(
        findings=[_finding("orphan", ["wiki/a.md"], "a orphan", safe_to_fix=False)],
        report_markdown="",
    )
    merged, _ = merge_lint_reports(shell, llm)
    assert len(merged.findings) == 1
    assert merged.findings[0].safe_to_fix is True


def test_merge_safe_to_fix_disagreement_keeps_shell_for_mechanical() -> None:
    shell_entry = _finding("orphan", ["wiki/a.md"], "x", safe_to_fix=True)
    llm_entry = _finding("orphan", ["wiki/a.md"], "x", safe_to_fix=False)
    merged, _ = merge_lint_reports(
        LintReport(findings=[shell_entry], report_markdown=""),
        LintReport(findings=[llm_entry], report_markdown=""),
    )
    assert merged.findings[0].safe_to_fix is True


def test_merge_safe_to_fix_keeps_llm_for_llm_only_categories() -> None:
    llm_entry = _finding(
        "contradiction",
        ["wiki/a.md", "wiki/b.md"],
        "a vs b",
        safe_to_fix=False,
    )
    merged, _ = merge_lint_reports(_empty(), LintReport(findings=[llm_entry], report_markdown=""))
    assert merged.findings == [llm_entry]


def test_merge_passes_through_fallback_reason() -> None:
    _, fallback = merge_lint_reports(_empty(), _empty(), llm_fallback_reason="ModelAPIError")
    assert fallback == "ModelAPIError"
