from pathlib import Path

import tomllib


def test_pyproject_has_runtime_deps() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    for needle in ("pymupdf",):
        assert any(d.startswith(needle) for d in deps), f"missing {needle} in {deps}"
    # Unused runtime dependencies removed in the final code review.
    for needle in ("beautifulsoup4",):
        assert not any(d.startswith(needle) for d in deps), f"unused dep {needle} still in {deps}"


def test_pyproject_dev_excludes_unused_deps() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    for needle in ("vcrpy", "pytest-snapshot", "freezegun", "respx"):
        assert not any(d.startswith(needle) for d in dev), f"unused dep {needle} still in {dev}"
