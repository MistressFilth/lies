"""Unit tests for the generalized ``missing_required_section`` lint check.

Task 6 of F17: replace the synthesis-only ``synthesis_missing_evidence``
finding with a per-page generalized check that fires for every page type
whose required ``## <Heading>`` lines (declared in
``default_schema.md`` → ``## Section contract``) are absent from the body.

Coverage matrix:

| page type    | required headings                                |
|--------------|--------------------------------------------------|
| overview     | Scope, Page types, Conventions                    |
| entity       | Overview, Description, References                 |
| concept      | Definition, Examples, Related                     |
| comparison   | Compared, Differences, When to use which         |
| source       | Source, Summary, Pages informed                   |
| synthesis    | Thesis, Evidence, Open Questions                  |

The deterministic shell emits one ``missing_required_section`` finding per
violating page, with all missing headings named in the message and
``safe_to_fix=False`` (a missing section is a content gap the operator
must fill — the repair agent's HARD RULE forbids ops on these).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.orchestrator import _build_lint_report
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path):
    """A Wiki dataclass rooted at ``tmp_path/wiki`` with a git-initialised
    data root containing ``wiki/`` (markdown), ``raw/``, and a seeded index.

    Mirrors the fixture in ``tests/unit/test_build_lint_report.py`` so the
    tests below exercise the real ``Wiki.section_contract`` cached property
    (which falls back to the shipped ``default_schema.md`` because the
    per-wiki override at ``<config_root>/schema.md`` does not exist).
    """
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
    """Write ``body`` at ``wiki.data_root / rel`` and ``git add`` it."""
    data_root: Path = wiki.data_root
    path = data_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=data_root, check=True)


def _missing_required_section_findings(report):
    return [f for f in report.findings if f.category == "missing_required_section"]


def _commit(wiki) -> None:
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)


# ---------- per-type coverage (parametrized over the contract matrix) ----------


@pytest.mark.parametrize(
    ("page_type", "rel_path", "frontmatter", "missing_headings"),
    [
        (
            "concept",
            "concepts/hooks.md",
            "title: Hooks\ntype: concept",
            ["## Definition", "## Examples", "## Related"],
        ),
        (
            "entity",
            "entities/postgres.md",
            "title: Postgres\ntype: entity",
            ["## Overview", "## Description", "## References"],
        ),
        (
            "comparison",
            "comparisons/postgres-vs-mysql.md",
            "title: Postgres vs MySQL\ntype: comparison",
            ["## Compared", "## Differences", "## When to use which"],
        ),
        (
            "source",
            "sources/karpathy.md",
            "title: Karpathy\ntype: source",
            ["## Source", "## Summary", "## Pages informed"],
        ),
        (
            "synthesis",
            "claude-code/synthesis/what-is-a-hook.md",
            "title: What is a hook\ntype: synthesis\ntags: [synthesis]\nderived_from:\n  - claude-code/concepts/hooks",
            ["## Thesis", "## Evidence", "## Open Questions"],
        ),
        (
            "overview",
            "overview.md",
            "title: Overview\ntype: overview",
            ["## Scope", "## Page types", "## Conventions"],
        ),
    ],
    ids=["concept", "entity", "comparison", "source", "synthesis", "overview"],
)
def test_flags_missing_required_section_per_type(
    wiki, page_type, rel_path, frontmatter, missing_headings
) -> None:
    """A page whose ``type:`` has a contract but whose body omits every
    required heading emits one ``missing_required_section`` finding naming
    every missing heading. ``safe_to_fix=False`` — a contract violation is
    a content gap the operator must write; the repair agent's HARD RULE
    forbids ops on these.
    """
    _write(
        wiki,
        f"wiki/{rel_path}",
        f"---\n{frontmatter}\n---\n# body\n\nprose.\n",
    )
    _commit(wiki)

    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 1, (
        f"expected exactly one missing_required_section finding, got {len(findings)}: "
        f"{[f.message for f in findings]}"
    )
    finding = findings[0]
    assert rel_path in finding.pages
    for heading in missing_headings:
        assert heading in finding.message, (
            f"missing heading {heading!r} must appear in finding.message"
        )
    assert finding.safe_to_fix is False


# ---------- subset / happy-path coverage ----------


def test_flags_only_missing_sections_not_all_required(wiki) -> None:
    """A page with some required sections present emits one finding naming the gaps.

    The contract is a checklist, not a binary presence check. A page
    that has ``## Definition`` and ``## Examples`` but lacks
    ``## Related`` must produce a single finding that names
    ``## Related`` — not three findings, and not zero.
    """
    _write(
        wiki,
        "wiki/concepts/partial.md",
        "---\ntitle: Partial\ntype: concept\n---\n## Definition\n\nx\n\n## Examples\n\nx\n",
    )
    _commit(wiki)

    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.pages == ["concepts/partial.md"]
    assert "## Related" in finding.message
    # Already-present sections must NOT appear in the message — that
    # would create noise for the operator.
    assert "## Definition" not in finding.message
    assert "## Examples" not in finding.message
    assert finding.safe_to_fix is False


def test_no_finding_when_all_required_sections_present(wiki) -> None:
    """A concept page with all required sections emits no finding."""
    _write(
        wiki,
        "wiki/concepts/complete.md",
        "---\ntitle: Complete\ntype: concept\n---\n"
        "## Definition\n\nx\n\n"
        "## Examples\n\nx\n\n"
        "## Related\n\nx\n",
    )
    _commit(wiki)

    report = _build_lint_report(wiki)
    assert _missing_required_section_findings(report) == []


# ---------- type-less / unknown-type / empty-contract edges ----------


@pytest.mark.parametrize(
    ("rel_path", "frontmatter"),
    [
        # No ``type:`` field — the lint shell has nothing to look up
        # in the contract. The writer-level refusal seam
        # (``build_author_plan``) raises ``WikiPlanInvalid`` for
        # typeless writes before they ever reach disk in production.
        ("wiki/concepts/anonymous.md", "---\ntitle: Anonymous\n---\n# Anonymous\n\nprose.\n"),
        # ``type: widget`` is not in the six-page-type contract. The
        # shell yields an empty required list and emits no finding —
        # other categories like ``orphan`` still apply.
        ("wiki/concepts/exotic.md", "---\ntitle: Exotic\ntype: widget\n---\n# Exotic\n\nprose.\n"),
    ],
    ids=["typeless_page", "unknown_type"],
)
def test_out_of_band_type_is_skipped(wiki, rel_path, frontmatter) -> None:
    """A page with no resolvable ``type:`` (missing, or pointing at a
    value outside the six-page-type contract) is silently skipped by
    the lint shell. Surfacing it here would be noise — the writer
    refusal seam is the production gate for these cases.
    """
    _write(wiki, rel_path, frontmatter)
    _commit(wiki)

    assert _missing_required_section_findings(_build_lint_report(wiki)) == []


def test_per_wiki_override_changes_required_sections(wiki) -> None:
    """The lint shell honours a per-wiki ``schema.md`` override that declares
    a different ``## Section contract`` than the shipped default.

    Sanity check that ``_build_lint_report`` reads
    ``wiki.section_contract`` (not a hard-coded default) so the same
    shell can drive both default and overridden wikis. Uses an
    override that requires ONLY ``## Override Heading`` for concept
    pages — pages that lack this heading but include the default
    contract's ``## Definition`` / ``## Examples`` / ``## Related``
    must NOT be flagged, because the operator's local contract is
    the source of truth.
    """
    # Force the cached_property to evaluate against the default schema
    # so the cache slot is created; we then drop the override file in
    # and bust the cache.
    _ = wiki.section_contract
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    override = (
        "## Section contract\n\n"
        "- **overview** — (none)\n"
        "- **entity** — (none)\n"
        "- **concept** — `## Override Heading`\n"
        "- **comparison** — (none)\n"
        "- **source** — (none)\n"
        "- **synthesis** — (none)\n"
    )
    (wiki.config_root / "schema.md").write_text(override, encoding="utf-8")
    # Bust the cached_property so the override takes effect.
    del wiki.__dict__["section_contract"]

    # Page that satisfies the override but lacks every default contract
    # heading for concept — must NOT trigger a finding because the
    # override is the source of truth.
    _write(
        wiki,
        "wiki/concepts/override-satisfied.md",
        "---\ntitle: Override\ntype: concept\n---\n# Override\n\n## Override Heading\n\nx\n",
    )
    # Page that lacks even the override heading — must trigger a finding.
    _write(
        wiki,
        "wiki/concepts/override-violated.md",
        "---\ntitle: Violated\ntype: concept\n---\n# Violated\n\n"
        "## Definition\n\nx\n\n## Examples\n\nx\n\n## Related\n\nx\n",
    )
    _commit(wiki)

    # Sanity: the override resolved to a contract whose ``concept``
    # field is ``['## Override Heading']`` — NOT the default's three
    # headings. The default's three headings are deliberately missing
    # in the satisfied page above; if the lint shell were reading the
    # default instead of the override, the satisfied page would be
    # flagged and this test would fail.
    from lies.schema.sections import SectionContract

    expected = SectionContract(
        overview=[],
        entity=[],
        concept=["## Override Heading"],
        comparison=[],
        source=[],
        synthesis=[],
    )
    assert wiki.section_contract == expected, (
        f"override should produce concept=['## Override Heading'], got {wiki.section_contract!r}"
    )

    findings = _missing_required_section_findings(_build_lint_report(wiki))
    flagged_pages = {f.pages[0] for f in findings}
    # ``concepts/override-satisfied.md`` has the override heading —
    # NOT flagged.
    assert "concepts/override-satisfied.md" not in flagged_pages, (
        f"override-satisfied page must not be flagged under override "
        f"contract; flagged pages: {flagged_pages!r}"
    )
    # ``concepts/override-violated.md`` lacks the override heading —
    # flagged.
    assert "concepts/override-violated.md" in flagged_pages, (
        f"override-violated page must be flagged under override contract; "
        f"flagged pages: {flagged_pages!r}"
    )


# ---------- coexistence with the surviving synthesis-page check ----------


def test_synthesis_page_with_all_sections_emits_no_findings(wiki) -> None:
    """A complete synthesis page emits no ``missing_required_section`` finding
    AND no ``synthesis_missing_evidence`` finding (the latter category is
    retired — only ``missing_required_section`` is emitted for any page
    type, including synthesis).
    """
    _write(
        wiki,
        "wiki/claude-code/synthesis/complete.md",
        "---\ntitle: Complete\ntype: synthesis\ntags: [synthesis]\n"
        "derived_from:\n  - claude-code/concepts/hooks\n---\n"
        "# Complete\n\nA hook intercepts events.\n\n"
        "## Thesis\n\nx\n\n"
        "## Evidence\n\n- [[claude-code/concepts/hooks]]\n\n"
        "## Open Questions\n\nx\n",
    )
    _write(
        wiki,
        "wiki/claude-code/concepts/hooks.md",
        "---\ntitle: Hooks\ntype: concept\n---\n"
        "## Definition\n\nx\n\n"
        "## Examples\n\nx\n\n"
        "## Related\n\nx\n",
    )
    _commit(wiki)

    report = _build_lint_report(wiki)
    assert _missing_required_section_findings(report) == []
    # The retired category must never be emitted.
    retired = [f for f in report.findings if f.category == "synthesis_missing_evidence"]
    assert retired == [], f"synthesis_missing_evidence category must be retired; got {retired!r}"


def test_dangling_derived_from_survives_generalized_check(wiki) -> None:
    """``dangling_derived_from`` still fires on synthesis pages with a resolved
    contract violation. The generalized ``missing_required_section`` and
    the synthesis-only ``dangling_derived_from`` are independent
    findings; both must surface together when both are violated.
    """
    _write(
        wiki,
        "wiki/claude-code/synthesis/dangling.md",
        "---\ntitle: Dangling\ntype: synthesis\ntags: [synthesis]\n"
        "derived_from:\n  - claude-code/concepts/does-not-exist\n---\n"
        "# Dangling\n\nA hook intercepts events.\n\n"
        "## Thesis\n\nx\n\n"
        "## Evidence\n\n- [[claude-code/concepts/does-not-exist]]\n\n"
        "## Open Questions\n\nx\n",
    )
    _commit(wiki)

    report = _build_lint_report(wiki)
    missing = _missing_required_section_findings(report)
    dangling = [f for f in report.findings if f.category == "dangling_derived_from"]
    assert missing == [], (
        f"all required synthesis sections present; expected no missing_required_section "
        f"finding, got {[f.message for f in missing]}"
    )
    assert len(dangling) == 1
    assert "claude-code/concepts/does-not-exist" in dangling[0].message
    # dangling_derived_from keeps its safe_to_fix=True (mechanical slug
    # removal) — that's the surviving synthesis-only repair and it
    # continues to flow through the repair agent.
    assert dangling[0].safe_to_fix is True


# ---------- multi-page sanity ----------


def test_findings_are_per_page_not_per_missing_section(wiki) -> None:
    """A wiki with three violating pages of different types emits one finding per page.

    The message aggregates the missing sections within a single page,
    so the per-page count is the operator-facing unit. Three pages
    with three missing sections each = three findings (not nine).
    """
    _write(
        wiki,
        "wiki/concepts/a.md",
        "---\ntitle: A\ntype: concept\n---\n# A\n\nprose.\n",
    )
    _write(
        wiki,
        "wiki/entities/b.md",
        "---\ntitle: B\ntype: entity\n---\n# B\n\nprose.\n",
    )
    _write(
        wiki,
        "wiki/sources/c.md",
        "---\ntitle: C\ntype: source\n---\n# C\n\nprose.\n",
    )
    _commit(wiki)

    findings = _missing_required_section_findings(_build_lint_report(wiki))
    assert len(findings) == 3, (
        f"expected one finding per violating page (3 pages), got {len(findings)}: "
        f"{[f.message for f in findings]}"
    )
    pages_with_findings = {tuple(f.pages) for f in findings}
    assert ("concepts/a.md",) in pages_with_findings
    assert ("entities/b.md",) in pages_with_findings
    assert ("sources/c.md",) in pages_with_findings
