"""SemVer auto-bump, CHANGELOG split, tag, and push.

Designed to be invoked from `make release`. Idempotent: if the
version surfaces already match the target, only CHANGELOG + tag +
commit + push run.

Bump precedence (highest wins):
  1. `BREAKING CHANGE:` footer or `!` after type/scope → major
  2. `feat:` → minor
  3. `fix:` / `refactor:` / `perf:` → patch
  4. anything else → noop (exits 0 with message)

Override: pass `--bump major|minor|patch` or set `BUMP=`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------- bump detection ----------

_BREAKING_RE = re.compile(r"^[a-z]+(\([^)]+\))?!:")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING CHANGE:", re.MULTILINE)
_MINOR_RE = re.compile(r"^feat(\([^)]+\))?:")
_PATCH_RE = re.compile(r"^(fix|refactor|perf)(\([^)]+\))?:")


def detect_bump(commits: list[str]) -> str:
    """Return 'major' | 'minor' | 'patch' | 'noop' from commit subjects + bodies."""
    text = "\n".join(commits)
    if _BREAKING_FOOTER_RE.search(text):
        return "major"
    for subject in commits:
        if _BREAKING_RE.match(subject):
            return "major"
    for subject in commits:
        if _MINOR_RE.match(subject):
            return "minor"
    for subject in commits:
        if _PATCH_RE.match(subject):
            return "patch"
    return "noop"


# ---------- version parsing + rewriting ----------

# Python's stdlib `re` requires fixed-width look-behind assertions. Keep the
# surrounding assignment in a capture group so spacing remains flexible while
# the version itself is still the only substituted text.
_PYPROJECT_VERSION_RE = re.compile(
    r'(?m)^(?P<prefix>\s*version\s*=\s*")(?P<version>\d+\.\d+\.\d+)(?P<suffix>")'
)
# Anchor on `__version__` (not line start) so indented definitions still match.
_INIT_VERSION_RE = re.compile(
    r'(?m)(?P<prefix>\b__version__\s*=\s*")(?P<version>\d+\.\d+\.\d+)(?P<suffix>")'
)


def parse_version(pyproject_text: str, init_text: str) -> tuple[str, str]:
    """Return (pyproject_version, init_version)."""
    py_match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    init_match = _INIT_VERSION_RE.search(init_text)
    if not py_match or not init_match:
        raise ValueError("could not parse version from pyproject.toml or src/lies/__init__.py")
    return py_match.group("version"), init_match.group("version")


def _bump_version(version: str, kind: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind: {kind!r}")


def rewrite_version(pyproject_text: str, init_text: str, new_version: str) -> tuple[str, str]:
    """Rewrite version strings in both files."""
    py_match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    init_match = _INIT_VERSION_RE.search(init_text)
    if py_match is None or init_match is None:
        raise ValueError("could not parse version from pyproject.toml or src/lies/__init__.py")
    new_py = _PYPROJECT_VERSION_RE.sub(
        lambda match: f"{match.group('prefix')}{new_version}{match.group('suffix')}",
        pyproject_text,
        count=1,
    )
    new_init = _INIT_VERSION_RE.sub(
        lambda match: f"{match.group('prefix')}{new_version}{match.group('suffix')}",
        init_text,
        count=1,
    )
    if new_py == pyproject_text or new_init == init_text:
        raise ValueError(f"failed to rewrite version to {new_version}")
    return new_py, new_init


# ---------- CHANGELOG split ----------

_UNRELEASED_RE = re.compile(
    r"(?P<header>^## \[Unreleased\][^\n]*\n)(?P<rest>.*?)(?=^## \[|\Z)",
    re.DOTALL | re.MULTILINE,
)


def split_changelog(changelog_text: str, new_version: str, today: str) -> str:
    """Move entries from [Unreleased] into a new dated [X.Y.Z] heading.

    Idempotent: if [Unreleased] has no entries, just append the new
    heading below it.
    """
    match = _UNRELEASED_RE.search(changelog_text)
    if match is None:
        raise ValueError("CHANGELOG.md missing ## [Unreleased] heading")
    header = match.group("header")
    rest = match.group("rest")
    new_heading = f"## [{new_version}] - {today}\n"
    if rest.strip():
        replacement = header + "\n" + new_heading + "\n" + rest.rstrip() + "\n\n"
    else:
        replacement = header + "\n" + new_heading + "\n"
    return changelog_text[: match.start()] + replacement + changelog_text[match.end() :]


# ---------- CLI ----------


def _preflight() -> None:
    """Assert release preconditions.

    Raises SystemExit on a failed precondition. Propagates
    ``subprocess.CalledProcessError`` if a git invocation itself fails
    (e.g. binary missing, network error mid-fetch).
    """
    status = subprocess.check_output(["git", "status", "--porcelain"]).decode("utf-8")
    if status.strip():
        print("release: working tree dirty; commit or stash first", file=sys.stderr)
        sys.exit(2)
    branch = (
        subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        .decode("utf-8")
        .strip()
    )
    if branch != "main":
        print(f"release: on branch {branch!r}; release only runs on main", file=sys.stderr)
        sys.exit(3)
    # Query the remote HEAD without touching local refs. This repo uses a
    # direct fetch refspec (+refs/heads/*:refs/heads/*); `git fetch origin
    # main` would try to update local refs/heads/main, which is checked
    # out and rejected with "refusing to fetch into checked-out branch".
    # `git ls-remote` only queries the remote and never updates local refs.
    try:
        ls_remote = subprocess.check_output(
            ["git", "ls-remote", "origin", "main"], stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as exc:
        print(f"release: git ls-remote failed: {exc}", file=sys.stderr)
        sys.exit(4)
    remote_sha = ""
    for line in ls_remote.decode("utf-8").splitlines():
        head, _, _ = line.partition("\t")
        if head:
            remote_sha = head
            break
    if not remote_sha:
        print("release: git ls-remote returned no SHA for origin/main", file=sys.stderr)
        sys.exit(4)
    local = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()
    if local != remote_sha:
        print(
            "release: local main is not in sync with origin/main; pull or push first",
            file=sys.stderr,
        )
        sys.exit(5)


def _collect_commits_since(last_tag: str) -> list[str]:
    """Return commit subjects + bodies since `last_tag` (or all if empty).

    Uses ``-z`` so git terminates each record with a NUL byte; this
    avoids delimiter collisions with any text in commit bodies.
    """
    range_arg = f"{last_tag}..HEAD" if last_tag else "HEAD"
    log = subprocess.check_output(
        ["git", "log", range_arg, "-z", "--pretty=format:%s%n%b%x00"]
    ).decode("utf-8")
    return [c.strip() for c in log.split("\x00") if c.strip()]


def _last_tag() -> str:
    try:
        return (
            subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"])
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Cut a SemVer release.")
    parser.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        default=None,
        help="Override bump detection (or set BUMP)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without applying (or set RELEASE_DRY_RUN)",
    )
    args = parser.parse_args()

    _preflight()

    last_tag = _last_tag()
    commits = _collect_commits_since(last_tag)

    bump_kind = args.bump or os.environ.get("BUMP")
    if bump_kind is None:
        bump_kind = detect_bump(commits)
    if bump_kind not in {"major", "minor", "patch", "noop"}:
        print(f"release: invalid bump kind {bump_kind!r}", file=sys.stderr)
        return 2
    if bump_kind == "noop":
        print("release: no qualifying commits since last tag")
        return 0

    repo_root = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    )
    pyproject_path = repo_root / "pyproject.toml"
    init_path = repo_root / "src" / "lies" / "__init__.py"
    changelog_path = repo_root / "CHANGELOG.md"

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    init_text = init_path.read_text(encoding="utf-8")
    current_py, current_init = parse_version(pyproject_text, init_text)
    if current_py != current_init:
        print(
            f"release: version mismatch pyproject={current_py} init={current_init}", file=sys.stderr
        )
        return 4
    current_version = current_py

    target_version = _bump_version(current_version, bump_kind)
    print(f"release: {current_version} -> {target_version} ({bump_kind})")

    dry_run = args.dry_run or os.environ.get("RELEASE_DRY_RUN") in {"1", "true", "yes"}
    if dry_run:
        print("release: --dry-run set; no changes written")
        return 0

    # Detect idempotent pre-staged release: CHANGELOG already carries a
    # dated [current_version] heading. The operator prepared the surfaces
    # by hand; we just tag the existing version without rewriting.
    changelog_text = changelog_path.read_text(encoding="utf-8")
    already_released = (
        f"## [{current_version}] - " in changelog_text
        or f"## [{current_version}] -" in changelog_text  # date may be empty
    )
    if already_released:
        print(
            f"release: {current_version} already staged in CHANGELOG; tagging only"
        )
    else:
        # Rewrite version surfaces if needed.
        if target_version != current_version:
            new_py, new_init = rewrite_version(
                pyproject_text, init_text, target_version
            )
            pyproject_path.write_text(new_py, encoding="utf-8")
            init_path.write_text(new_init, encoding="utf-8")
        # Split CHANGELOG.
        today = _dt.datetime.now(tz=_dt.timezone.utc).date().isoformat()
        changelog_text = changelog_path.read_text(encoding="utf-8")
        new_changelog = split_changelog(changelog_text, target_version, today)
        changelog_path.write_text(new_changelog, encoding="utf-8")

    # Commit, tag, push. When pre-staged, use current_version (the
    # operator's intent), not the auto-detected target.
    tag_version = current_version if already_released else target_version
    subprocess.check_call(["git", "add", "-A"])
    subprocess.check_call(["git", "commit", "--allow-empty", "-m", f"chore(release): v{tag_version}"])

    # Tag.
    tag = f"v{tag_version}"
    subprocess.check_call(["git", "tag", "-a", tag, "-m", f"Release {tag}"])

    # Push.
    subprocess.check_call(["git", "push", "origin", "main", tag])
    print(f"release: cut {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
