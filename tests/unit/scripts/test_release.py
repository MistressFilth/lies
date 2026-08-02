"""Unit tests for scripts/release.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from release import (  # noqa: I001
    detect_bump,
    parse_version,
    rewrite_version,
    split_changelog,
)


# ---------- detect_bump ----------


def test_detect_bump_major_from_breaking_footer() -> None:
    commits = ["feat(api)!: drop /v1 endpoint", "", "BREAKING CHANGE: /v1 removed"]
    assert detect_bump(commits) == "major"


def test_detect_bump_major_from_bang_in_subject() -> None:
    commits = ["feat(api)!: drop /v1 endpoint"]
    assert detect_bump(commits) == "major"


def test_detect_bump_minor_from_feat() -> None:
    commits = ["feat: add new endpoint", "feat(etl): add new builder"]
    assert detect_bump(commits) == "minor"


def test_detect_bump_patch_from_fix() -> None:
    commits = ["fix: handle edge case", "refactor: clean up"]
    assert detect_bump(commits) == "patch"


def test_detect_bump_noop_when_no_qualifying() -> None:
    commits = ["docs: update readme", "chore: bump deps", "ci: fix workflow"]
    assert detect_bump(commits) == "noop"


def test_detect_bump_major_wins_over_minor_and_patch() -> None:
    commits = ["feat: new", "fix: bug", "feat(api)!: breaking"]
    assert detect_bump(commits) == "major"


# ---------- parse_version ----------


def test_parse_version_returns_current_versions() -> None:
    pyproject = '[project]\nname = "lies"\nversion = "0.4.0"\n'
    init_file = '__version__ = "0.4.0"\n'
    py_v, init_v = parse_version(pyproject, init_file)
    assert py_v == "0.4.0"
    assert init_v == "0.4.0"


# ---------- rewrite_version ----------


def test_rewrite_version_updates_both_surfaces() -> None:
    pyproject = '[project]\nversion = "0.4.0"\n'
    init_file = '__version__ = "0.4.0"\n'
    new_py, new_init = rewrite_version(pyproject, init_file, "0.5.0")
    assert '"0.5.0"' in new_py
    assert '"0.5.0"' in new_init


# ---------- split_changelog ----------


def test_split_changelog_moves_entries_under_dated_heading(tmp_path: Path) -> None:
    original = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "### Added\n"
        "- Feature X\n"
        "\n"
        "## [0.2.0] - 2026-07-29\n"
        "\n"
        "### Added\n"
        "- Earlier\n"
    )
    result = split_changelog(original, "0.4.0", "2026-08-02")
    # Unreleased header preserved but empty
    assert "## [Unreleased]\n" in result
    # New dated heading inserted
    assert "## [0.4.0] - 2026-08-02" in result
    # Feature X moved under the new heading
    new_heading_idx = result.index("## [0.4.0]")
    unreleased_idx = result.index("## [Unreleased]")
    feature_x_idx = result.index("- Feature X")
    assert unreleased_idx < new_heading_idx < feature_x_idx
    # Old [0.2.0] still present
    assert "## [0.2.0] - 2026-07-29" in result


def test_split_changelog_idempotent_when_no_unreleased_entries(tmp_path: Path) -> None:
    original = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-07-29\n"
    result = split_changelog(original, "0.4.0", "2026-08-02")
    # Idempotent: just append the new heading below the empty Unreleased
    assert "## [0.4.0] - 2026-08-02" in result
