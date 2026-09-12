import tomllib

from lies import __version__


def test_version_consistent() -> None:
    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["version"] == __version__
    assert __version__ == "0.21.2"
