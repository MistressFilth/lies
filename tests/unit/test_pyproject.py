from pathlib import Path

import tomllib


def test_pyproject_has_runtime_deps() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    for needle in ("pymupdf", "ahocorasick_rs", "beautifulsoup4"):
        assert any(d.startswith(needle) for d in deps), f"missing {needle} in {deps}"


def test_pyproject_has_dev_deps() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    for needle in ("vcrpy", "pytest-snapshot", "freezegun"):
        assert any(d.startswith(needle) for d in dev), f"missing {needle} in {dev}"
