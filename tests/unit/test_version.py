import tomllib

from lies import __version__


def test_version_consistent_with_pyproject() -> None:
    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["version"] == __version__


def test_version_is_0_28_0() -> None:
    assert __version__ == "0.28.0"
