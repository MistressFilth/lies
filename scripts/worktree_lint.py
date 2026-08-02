"""Seven-invariants checker for the bare-repo-with-worktrees layout."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def _run(args: list[str]) -> str:
    try:
        result = subprocess.check_output(args, stderr=subprocess.DEVNULL)
        return result.decode("utf-8").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _parse_worktrees(porcelain: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def lint(bare_dir: Path) -> list[str]:
    violations: list[str] = []
    porcelain = _run(["git", "-C", str(bare_dir), "worktree", "list", "--porcelain"])
    worktrees = _parse_worktrees(porcelain)
    bare_resolved = bare_dir.resolve()
    if not re.fullmatch(r"\w+\.git", bare_resolved.name):
        violations.append(
            f"invariant 1 violated: bare dir {bare_dir} does not match {{name}}.git pattern"
        )

    for wt in worktrees:
        wt_path = Path(wt.get("worktree", ""))
        if not wt_path:
            continue
        if wt_path.resolve() == bare_resolved:
            violations.append(f"invariant 4 violated: bare dir {wt_path} has checked-out branch")
            continue

        try:
            wt_path.relative_to(bare_resolved.parent)
            is_sibling = True
        except ValueError:
            is_sibling = False
        if not is_sibling or wt_path.parent.resolve() != bare_resolved.parent:
            violations.append(
                f"invariant 2 violated: worktree {wt_path} is not a direct sibling of "
                f"{bare_resolved}; nested under {wt_path.parent}"
            )
            continue

        branch_ref = wt.get("branch", "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref.startswith("refs/heads/") else ""
        dir_name = wt_path.name
        if not branch:
            violations.append(f"invariant 3 violated: {wt_path} has detached HEAD (no branch)")
            continue
        if dir_name != branch:
            violations.append(
                f"invariant 3 violated: dir {dir_name!r} != branch {branch!r} at {wt_path}"
            )

        upstream = _run([
            "git", "-C", str(wt_path),
            "config", "--get", f"branch.{branch}.merge",
        ])
        if upstream and upstream != f"refs/heads/{branch}":
            violations.append(
                f"invariant 7 violated: branch {branch} upstream is {upstream!r}, "
                f"expected refs/heads/{branch}"
            )
        remote = _run([
            "git", "-C", str(wt_path),
            "config", "--get", f"branch.{branch}.remote",
        ])
        if remote and remote != "origin":
            violations.append(
                f"invariant 7 violated: branch {branch} remote is {remote!r}, expected origin"
            )

    origin_url = _run(["git", "-C", str(bare_dir), "config", "--get", "remote.origin.url"])
    if not origin_url:
        violations.append(f"invariant 5 violated: {bare_dir} has no remote.origin.url")
    elif not origin_url.startswith("https://github.com/"):
        violations.append(
            f"invariant 5 violated: remote.origin.url is {origin_url!r}, "
            f"expected https://github.com/{{owner}}/{{repo}}.git"
        )

    fetch_refspec = _run(["git", "-C", str(bare_dir), "config", "--get", "remote.origin.fetch"])
    if not fetch_refspec:
        violations.append(
            f"invariant 6 violated: {bare_dir} has no remote.origin.fetch refspec"
        )
    elif "refs/remotes/origin/" in fetch_refspec:
        violations.append(
            f"invariant 6 violated: remote.origin.fetch refspec is {fetch_refspec!r}, "
            f"expected direct +refs/heads/*:refs/heads/*"
        )

    return violations


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <bare-repo-dir>", file=sys.stderr)
        return 2
    bare = Path(sys.argv[1]).resolve()
    if not bare.is_dir():
        print(f"not a directory: {bare}", file=sys.stderr)
        return 2
    violations = lint(bare)
    if not violations:
        print(f"worktree layout clean: {bare}")
        return 0
    print(f"worktree layout has {len(violations)} violation(s):", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
