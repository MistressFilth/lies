"""Flake8 extension for pydantic-guidance (PG) enforcement."""

from __future__ import annotations

import argparse
import ast
import os
from collections.abc import Iterator
from importlib.metadata import version
from typing import Any, Self

from pydantic_guidance._flake8_protocol import RESULT_ADAPTER
from pydantic_guidance._guidance_rules import analyze


class PGPlugin:
    """Flake8 AST checker for pydantic-guidance.

    Emits PG001-003 (hard, default-on) and PG101 (soft, opt-in via
    ``--extend-select=PG101``). Hard versus soft is purely the code number;
    flake8 ``--select`` / ``--extend-select`` controls activation and
    ``# noqa: PGxxx`` works per-line. The whole plugin is gated by
    ``[hooks].structured_data_enforcement`` read from
    ``.true-spec/project/true-spec.toml``.
    """

    name = "flake8-pydantic-guidance"
    version = version("pydantic-guidance")

    _enabled: bool = True

    def __init__(self, tree: ast.AST) -> None:
        self._tree = tree

    @classmethod
    def add_options(cls, option_manager: Any) -> None:
        """Register the shared config-root option with flake8.

        Args:
            option_manager: Flake8 option manager instance.
        """
        try:
            option_manager.add_option(
                "--pg-config-root",
                default=None,
                parse_from_config=True,
                help=(
                    "Project root for pydantic-guidance config resolution. "
                    "Default: current working directory."
                ),
            )
        except argparse.ArgumentError:
            pass

    @classmethod
    def parse_options(cls, options: Any) -> None:
        """Gate the plugin on [hooks].structured_data_enforcement.

        Args:
            options: Parsed flake8 options namespace.
        """
        from pydantic_guidance._config import read_hooks_config

        root = options.pg_config_root or os.getcwd()
        hooks = read_hooks_config(root)
        cls._enabled = hooks.structured_data_enforcement

    def run(self) -> Iterator[tuple[int, int, str, type[Self]]]:
        """Yield flake8 error tuples for each PG finding.

        Yields:
            Tuples of (line, col_offset, message, checker_type), each
            validated through ``RESULT_ADAPTER``. Yields nothing when the
            plugin is disabled (the ``_enabled`` gate guards the loop body,
            mirroring ``flake8_bid.BidContainmentPlugin.run``).
        """
        for finding in analyze(self._tree):  # type: ignore[arg-type]
            if not self._enabled:
                continue
            yield RESULT_ADAPTER.validate_python(
                (finding.line, finding.col, finding.message, type(self))
            )
