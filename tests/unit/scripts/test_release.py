"""Unit tests for scripts/release.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from release import (  # noqa: I001
    _preflight,
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


def test_parse_version_finds_indented_definition() -> None:
    """A leading-indent (not line-start) ``__version__`` still matches."""
    pyproject = '[project]\nversion = "0.4.0"\n'
    init_file = 'class _Meta:\n    __version__ = "0.4.0"\n'
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


def test_rewrite_version_raises_when_no_change() -> None:
    """Passing the current version raises ValueError (no-op rejected)."""
    pyproject = '[project]\nversion = "0.4.0"\n'
    init_file = '__version__ = "0.4.0"\n'
    with pytest.raises(ValueError, match="failed to rewrite version"):
        rewrite_version(pyproject, init_file, "0.4.0")


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


# ---------- _preflight (upstream sync) ----------


def _stub_check_output(
    mapping: dict[tuple[str, ...], str | Exception],
) -> object:
    """Return a check_output stub that maps argv tuples to stdout strings.

    Values in ``mapping`` are either a string to return as stdout bytes,
    or an ``Exception`` instance to raise when the argv matches.
    Unmodeled argv tuples return empty stdout (no-op).
    """

    def stub(args: list[str], *args_: object, **kwargs: object) -> bytes:
        key = tuple(args)
        if key in mapping:
            value = mapping[key]
            if isinstance(value, Exception):
                raise value
            return value.encode("utf-8")
        # Allow unmodeled calls to be ignored.
        return b""

    return stub


def _stub_check_call(calls: list[list[str]], raises: bool = False) -> object:
    """Return a check_call stub that records calls and optionally raises."""

    def stub(args: list[str], *args_: object, **kwargs: object) -> int:
        calls.append(list(args))
        if raises:
            raise subprocess.CalledProcessError(1, args)
        return 0

    return stub


def test_preflight_passes_when_local_matches_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local HEAD == origin/main: preflight exits cleanly."""
    import release as _release

    sha = "a" * 40
    monkeypatch.setattr(
        _release.subprocess,
        "check_output",
        _stub_check_output(
            {
                ("git", "status", "--porcelain"): "",
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main",
                ("git", "ls-remote", "origin", "main"): f"{sha}\trefs/heads/main\n",
                ("git", "rev-parse", "HEAD"): sha,
            }
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(_release.subprocess, "check_call", _stub_check_call(calls))
    _preflight()  # must not raise
    # Preflight must NOT call `git fetch` (would fail when main is checked
    # out under a direct fetch refspec). It uses `git ls-remote` instead.
    assert ["git", "fetch", "origin", "main"] not in calls
    assert calls == []


def test_preflight_exits_5_when_local_diverges_from_remote(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local HEAD != origin/main: preflight exits with code 5."""
    import release as _release

    monkeypatch.setattr(
        _release.subprocess,
        "check_output",
        _stub_check_output(
            {
                ("git", "status", "--porcelain"): "",
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main",
                ("git", "ls-remote", "origin", "main"): (f"{'b' * 40}\trefs/heads/main\n"),
                ("git", "rev-parse", "HEAD"): "a" * 40,
            }
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(_release.subprocess, "check_call", _stub_check_call(calls))
    with pytest.raises(SystemExit) as excinfo:
        _preflight()
    assert excinfo.value.code == 5
    captured = capsys.readouterr()
    assert "not in sync" in captured.err


def test_preflight_exits_4_when_ls_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing `git ls-remote` exits with code 4."""
    import release as _release

    monkeypatch.setattr(
        _release.subprocess,
        "check_output",
        _stub_check_output(
            {
                ("git", "status", "--porcelain"): "",
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main",
                (
                    "git",
                    "ls-remote",
                    "origin",
                    "main",
                ): subprocess.CalledProcessError(128, ["git", "ls-remote", "origin", "main"]),
            }
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(_release.subprocess, "check_call", _stub_check_call(calls))
    with pytest.raises(SystemExit) as excinfo:
        _preflight()
    assert excinfo.value.code == 4
    captured = capsys.readouterr()
    assert "git ls-remote failed" in captured.err
    # check_call must never be invoked for fetch (avoids the checked-out
    # branch refspec conflict entirely).
    assert ["git", "fetch", "origin", "main"] not in calls


# ---------- main() — idempotent pre-staged release ----------


def test_main_tags_without_rewrite_when_surfaces_pre_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operator pre-staged 0.4.0 in pyproject, __init__, and CHANGELOG.

    `main()` should detect this and only commit/tag/push v0.4.0 — not
    rewrite any version surface or split the CHANGELOG a second time.
    """
    import release as _release

    # Lay out a fake repo root in tmp_path with surfaces already at 0.4.0
    # and a CHANGELOG that already has the dated [0.4.0] heading.
    pyproject_text = '[project]\nname = "lies"\nversion = "0.4.0"\n'
    init_text = '__version__ = "0.4.0"\n'
    changelog_text = (
        "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-08-02\n\n## [0.2.0] - 2026-07-29\n"
    )
    (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    (tmp_path / "src" / "lies").mkdir(parents=True)
    (tmp_path / "src" / "lies" / "__init__.py").write_text(init_text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")

    sha = "a" * 40

    def fake_check_output(args: list[str], *args_: object, **kwargs: object) -> bytes:
        key = tuple(args)
        mapping: dict[tuple[str, ...], bytes | Exception] = {
            ("git", "status", "--porcelain"): b"",
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): b"main",
            ("git", "rev-parse", "HEAD"): sha.encode("utf-8"),
            ("git", "ls-remote", "origin", "main"): (f"{sha}\trefs/heads/main\n".encode()),
            ("git", "rev-parse", "--show-toplevel"): str(tmp_path).encode("utf-8"),
            # One minor-bump commit since no tags exist; surfaces are pre-staged.
            ("git", "log", "HEAD", "-z", "--pretty=format:%s%n%b%x00"): (
                b"feat: pre-staged release\x00"
            ),
        }
        if key in mapping:
            value = mapping[key]
            if isinstance(value, Exception):
                raise value
            return value
        return b""

    # `git describe --tags --abbrev=0` must raise so `_last_tag` returns "".
    def fake_check_output_with_describe_raise(
        args: list[str], *args_: object, **kwargs: object
    ) -> bytes:
        if tuple(args) == ("git", "describe", "--tags", "--abbrev=0"):
            raise subprocess.CalledProcessError(128, args)
        return fake_check_output(args, *args_, **kwargs)

    calls: list[list[str]] = []

    def fake_check_call(args: list[str], *args_: object, **kwargs: object) -> int:
        calls.append(list(args))
        return 0

    monkeypatch.setattr(_release.subprocess, "check_output", fake_check_output_with_describe_raise)
    monkeypatch.setattr(_release.subprocess, "check_call", fake_check_call)
    # argparse in main() reads sys.argv; default to a bare invocation.
    monkeypatch.setattr(sys, "argv", ["release.py"])

    rc = _release.main()

    captured = capsys.readouterr()
    assert rc == 0, f"main() returned {rc}; stdout={captured.out!r} stderr={captured.err!r}"
    # Surfaces are untouched: pyproject, init, and CHANGELOG all unchanged.
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == pyproject_text
    assert (tmp_path / "src" / "lies" / "__init__.py").read_text(encoding="utf-8") == init_text
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == changelog_text
    # No rewrites were applied — the file-modifying `git add -A` still runs
    # but it stages the same content (no diff to record).
    assert ["git", "commit", "--allow-empty", "-m", "chore(release): v0.4.0"] in calls
    assert ["git", "tag", "-a", "v0.4.0", "-m", "Release v0.4.0"] in calls
    assert ["git", "push", "origin", "main", "v0.4.0"] in calls
    # Operator-facing message reflects the idempotent path.
    assert "already staged" in captured.out or "tagging only" in captured.out
    # --allow-empty is gated on the idempotent pre-staged path; assert it
    # is present here and not present in the auto-bump path (next test).
    assert ["git", "commit", "--allow-empty", "-m", "chore(release): v0.4.0"] in calls


def test_main_omits_allow_empty_on_auto_bump_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-bump (non-idempotent) path: --allow-empty must NOT be present.

    Without the pre-staged CHANGELOG heading, the script rewrites
    surfaces and splits the CHANGELOG. The resulting commit must have
    a real diff; --allow-empty would mask a future bug that silently
    produces no diff.
    """
    import release as _release

    # Surfaces still at 0.3.0; CHANGELOG has only [Unreleased] and a
    # prior [0.2.0] heading — no pre-staged [0.3.0] entry.
    pyproject_text = '[project]\nname = "lies"\nversion = "0.3.0"\n'
    init_text = '__version__ = "0.3.0"\n'
    changelog_text = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- New feature\n\n## [0.2.0] - 2026-07-29\n"
    )
    (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    (tmp_path / "src" / "lies").mkdir(parents=True)
    (tmp_path / "src" / "lies" / "__init__.py").write_text(init_text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")

    sha = "a" * 40

    def fake_check_output(args: list[str], *args_: object, **kwargs: object) -> bytes:
        key = tuple(args)
        mapping: dict[tuple[str, ...], bytes | Exception] = {
            ("git", "status", "--porcelain"): b"",
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): b"main",
            ("git", "rev-parse", "HEAD"): sha.encode("utf-8"),
            ("git", "ls-remote", "origin", "main"): (f"{sha}\trefs/heads/main\n".encode()),
            ("git", "rev-parse", "--show-toplevel"): str(tmp_path).encode("utf-8"),
            ("git", "log", "HEAD", "-z", "--pretty=format:%s%n%b%x00"): (b"feat: new endpoint\x00"),
        }
        if key in mapping:
            value = mapping[key]
            if isinstance(value, Exception):
                raise value
            return value
        return b""

    def fake_check_output_with_describe_raise(
        args: list[str], *args_: object, **kwargs: object
    ) -> bytes:
        if tuple(args) == ("git", "describe", "--tags", "--abbrev=0"):
            raise subprocess.CalledProcessError(128, args)
        return fake_check_output(args, *args_, **kwargs)

    calls: list[list[str]] = []

    def fake_check_call(args: list[str], *args_: object, **kwargs: object) -> int:
        calls.append(list(args))
        return 0

    monkeypatch.setattr(_release.subprocess, "check_output", fake_check_output_with_describe_raise)
    monkeypatch.setattr(_release.subprocess, "check_call", fake_check_call)
    monkeypatch.setattr(sys, "argv", ["release.py"])

    rc = _release.main()
    assert rc == 0

    commit_calls = [c for c in calls if c and c[0] == "git" and c[1] == "commit"]
    assert commit_calls, "expected at least one git commit call"
    for commit_call in commit_calls:
        # --allow-empty is reserved for the idempotent path; never present
        # when the script auto-bumps because the rewrite produces a real
        # diff and the script should fail loudly if it does not.
        assert "--allow-empty" not in commit_call, (
            f"--allow-empty must not be used on the auto-bump path: {commit_call!r}"
        )


def test_main_returns_8_when_tag_already_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-check: an existing v0.4.0 tag causes main() to return 8 with a
    structured message and no raw CalledProcessError.
    """
    import release as _release

    # Pre-staged surfaces so the script reaches the commit/tag block.
    pyproject_text = '[project]\nname = "lies"\nversion = "0.4.0"\n'
    init_text = '__version__ = "0.4.0"\n'
    changelog_text = (
        "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-08-02\n\n## [0.2.0] - 2026-07-29\n"
    )
    (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    (tmp_path / "src" / "lies").mkdir(parents=True)
    (tmp_path / "src" / "lies" / "__init__.py").write_text(init_text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")

    sha = "a" * 40

    def fake_check_output(args: list[str], *args_: object, **kwargs: object) -> bytes:
        key = tuple(args)
        mapping: dict[tuple[str, ...], bytes | Exception] = {
            ("git", "status", "--porcelain"): b"",
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): b"main",
            ("git", "rev-parse", "HEAD"): sha.encode("utf-8"),
            ("git", "ls-remote", "origin", "main"): (f"{sha}\trefs/heads/main\n".encode()),
            ("git", "rev-parse", "--show-toplevel"): str(tmp_path).encode("utf-8"),
            ("git", "log", "HEAD", "-z", "--pretty=format:%s%n%b%x00"): (
                b"feat: pre-staged release\x00"
            ),
            # The pre-check `git tag --list v0.4.0` returns the existing tag.
            ("git", "tag", "--list", "v0.4.0"): b"v0.4.0\n",
        }
        if key in mapping:
            value = mapping[key]
            if isinstance(value, Exception):
                raise value
            return value
        return b""

    def fake_check_output_with_describe_raise(
        args: list[str], *args_: object, **kwargs: object
    ) -> bytes:
        if tuple(args) == ("git", "describe", "--tags", "--abbrev=0"):
            raise subprocess.CalledProcessError(128, args)
        return fake_check_output(args, *args_, **kwargs)

    calls: list[list[str]] = []

    def fake_check_call(args: list[str], *args_: object, **kwargs: object) -> int:
        calls.append(list(args))
        return 0

    monkeypatch.setattr(_release.subprocess, "check_output", fake_check_output_with_describe_raise)
    monkeypatch.setattr(_release.subprocess, "check_call", fake_check_call)
    monkeypatch.setattr(sys, "argv", ["release.py"])

    # Must NOT raise; must return 8.
    rc = _release.main()
    assert rc == 8, f"main() returned {rc}; expected 8 for existing tag"
    captured = capsys.readouterr()
    # Structured stderr message per the spec's error table.
    assert "v0.4.0 exists" in captured.err
    assert "BUMP=" in captured.err
    # The pre-check must short-circuit before the tag is created.
    assert ["git", "tag", "-a", "v0.4.0", "-m", "Release v0.4.0"] not in calls
    # The commit does run (idempotent path), but no tag creation follows.
    assert ["git", "commit", "--allow-empty", "-m", "chore(release): v0.4.0"] in calls
