"""Shared flake8 result-protocol validator for pydantic_guidance plugins."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

RESULT_ADAPTER: TypeAdapter[tuple[int, int, str, type[Any]]] = TypeAdapter(
    tuple[int, int, str, type[Any]]
)
"""Validate each flake8 yield tuple against (line, col, msg, checker_type).

Module-level so the schema is built once at import. Use in each plugin's
run() like:

    yield RESULT_ADAPTER.validate_python((line, col, msg, type(self)))
"""
