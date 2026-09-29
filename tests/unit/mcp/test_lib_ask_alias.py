"""Pin the lib_ask tool rename — no live `ask` symbol survives."""


def test_import_lib_ask_succeeds() -> None:
    from lies.mcp.synth import lib_ask

    assert lib_ask is not None


def test_import_ask_raises() -> None:
    import pytest

    from lies import mcp as mcp_module  # noqa: F401  (force-import)

    with pytest.raises(ImportError):
        from lies.mcp.synth import ask  # noqa: F401
