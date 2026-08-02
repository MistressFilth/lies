from lies.builders.errors import (
    BuilderError,
    BuilderFetchFailed,
    BuilderParseError,
    BuilderUnavailable,
)


def test_builder_unavailable_carries_format() -> None:
    err = BuilderUnavailable("liquid")
    assert err.source_format == "liquid"
    assert "liquid" in str(err)
    assert isinstance(err, BuilderError)


def test_builder_fetch_failed_carries_tool() -> None:
    err = BuilderFetchFailed("pandoc", "exit 47")
    assert err.tool == "pandoc"
    assert "pandoc" in str(err)
    assert "exit 47" in str(err)
    assert isinstance(err, BuilderError)


def test_builder_parse_error_carries_optional_path() -> None:
    err = BuilderParseError("corrupt stream")
    assert err.path is None
    assert "corrupt stream" in str(err)
    err2 = BuilderParseError("corrupt stream", path="pages/p0003.md")
    assert err2.path == "pages/p0003.md"
    assert isinstance(err, BuilderError)
