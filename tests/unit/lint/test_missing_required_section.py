"""Unit tests for the generalized ``missing_required_section`` lint check.

Emits one finding per violating page, all missing headings named, ``safe_to_fix=False``
(missing sections are content gaps; the repair agent's HARD RULE forbids ops on these).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.orchestrator import _build_lint_report
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path):
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return make_wiki(name="missing-required-section", data_root=root)


def _write(wiki, rel: str, body: str) -> None:
    data_root: Path = wiki.data_root
    path = data_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=data_root, check=True)


def _missing_required_section_findings(report):
    return [f for f in report.findings if f.category == "missing_required_section"]


def _commit(wiki) -> None:
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)


@pytest.mark.parametrize(
    ("rel_path", "frontmatter", "missing_headings"),
    [
        # concept contract: 3 missing slots.
        (
            "concepts/hooks.md",
            "title: Hooks\ntype: concept",
            ["## Definition", "## Examples", "## Related"],
        ),
        # synthesis contract: different shape, still emits one finding with all gaps named.
        (
            "claude-code/synthesis/what-is-a-hook.md",
            "title: What is a hook\ntype: synthesis\ntags: [synthesis]\n"
            "derived_from:\n  - claude-code/concepts/hooks",
            ["## Thesis", "## Evidence", "## Open Questions"],
        ),
    ],
    ids=["concept", "synthesis"],
)
def test_flags_missing_required_section_per_type(
    wiki, rel_path, frontmatter, missing_headings
) -> None:
    """Body omits every required heading → one finding naming all gaps, safe_to_fix=False."""
    _write(wiki, f"wiki/{rel_path}", f"---\n{frontmatter}\n---\n# body\n\nprose.\n")
    _commit(wiki)
    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 1, f"got {len(findings)}: {[f.message for f in findings]}"
    finding = findings[0]
    assert rel_path in finding.pages
    for heading in missing_headings:
        assert heading in finding.message
    assert finding.safe_to_fix is False


def test_flags_only_missing_sections_not_all_required(wiki) -> None:
    """A page with some required sections present emits one finding naming only the gaps."""
    body = "---\ntitle: Partial\ntype: concept\n---\n## Definition\n\nx\n\n## Examples\n\nx\n"
    _write(wiki, "wiki/concepts/partial.md", body)
    _commit(wiki)
    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.pages == ["concepts/partial.md"]
    assert "## Related" in finding.message
    assert "## Definition" not in finding.message
    assert "## Examples" not in finding.message
    assert finding.safe_to_fix is False


def test_no_finding_when_all_required_sections_present(wiki) -> None:
    """All required sections present → no finding."""
    body = (
        "---\ntitle: Complete\ntype: concept\n---\n"
        "## Definition\n\nx\n\n## Examples\n\nx\n\n## Related\n\nx\n"
    )
    _write(wiki, "wiki/concepts/complete.md", body)
    _commit(wiki)
    assert _missing_required_section_findings(_build_lint_report(wiki)) == []


def test_per_wiki_override_changes_required_sections(wiki) -> None:
    """Override schema replaces the contract — the operator's per-wiki schema is the source of truth.

    Pins that ``_build_lint_report`` reads ``wiki.section_contract``, not a hard-coded default.
    """
    _ = wiki.section_contract  # populate the cached_property slot
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text(
        "## Section contract\n\n- **concept** — `## Override Heading`\n", encoding="utf-8"
    )
    del wiki.__dict__["section_contract"]
    _write(
        wiki,
        "wiki/concepts/override-satisfied.md",
        "---\ntitle: Override\ntype: concept\n---\n# Override\n\n## Override Heading\n\nx\n",
    )
    _write(
        wiki,
        "wiki/concepts/override-violated.md",
        "---\ntitle: Violated\ntype: concept\n---\n"
        "## Definition\n\nx\n\n## Examples\n\nx\n\n## Related\n\nx\n",
    )
    _commit(wiki)
    flagged = {f.pages[0] for f in _missing_required_section_findings(_build_lint_report(wiki))}
    assert "concepts/override-satisfied.md" not in flagged
    assert "concepts/override-violated.md" in flagged


def test_synthesis_page_with_all_sections_emits_no_findings(wiki) -> None:
    """Boundary pin: complete synthesis page emits no findings; the retired category never appears."""
    body = (
        "---\ntitle: Complete\ntype: synthesis\ntags: [synthesis]\n"
        "derived_from:\n  - claude-code/concepts/hooks\n---\n"
        "# Complete\n\nA hook intercepts events.\n\n"
        "## Thesis\n\nx\n\n## Evidence\n\n- [[claude-code/concepts/hooks]]\n\n"
        "## Open Questions\n\nx\n"
    )
    _write(wiki, "wiki/claude-code/synthesis/complete.md", body)
    _commit(wiki)
    report = _build_lint_report(wiki)
    assert _missing_required_section_findings(report) == []
    retired = [f for f in report.findings if f.category == "synthesis_missing_evidence"]
    assert retired == [], f"synthesis_missing_evidence must be retired; got {retired!r}"


def test_findings_are_per_page_not_per_missing_section(wiki) -> None:
    """Three violating pages of different types emit one finding per page (not per missing section)."""
    for rel, type_ in [
        ("wiki/concepts/a.md", "concept"),
        ("wiki/entities/b.md", "entity"),
        ("wiki/sources/c.md", "source"),
    ]:
        _write(wiki, rel, f"---\ntitle: {rel}\ntype: {type_}\n---\n# body\n\nprose.\n")
    _commit(wiki)
    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 3, f"got {len(findings)}: {[f.message for f in findings]}"
    pages_with_findings = {tuple(f.pages) for f in findings}
    assert ("concepts/a.md",) in pages_with_findings
    assert ("entities/b.md",) in pages_with_findings
    assert ("sources/c.md",) in pages_with_findings
