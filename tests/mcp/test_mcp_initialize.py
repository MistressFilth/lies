"""Smoke test: handshake response carries the instructions payload."""

from __future__ import annotations

from lies.mcp.instructions_loader import load_instructions
from lies.mcp.server import mcp


def test_mcp_server_has_instructions_attribute() -> None:
    instr = getattr(mcp, "_instructions", None) or getattr(mcp, "instructions", None)
    assert instr is not None, "FastMCP instance missing instructions field"
    assert len(instr) > 100, "instructions payload too small; expected ~200 tokens"


def test_instructions_payload_matches_loaded_file() -> None:
    instr = getattr(mcp, "_instructions", None) or getattr(mcp, "instructions", None)
    assert instr == load_instructions()


def test_instructions_payload_mentions_library_root() -> None:
    instr = getattr(mcp, "_instructions", None) or getattr(mcp, "instructions", None)
    # v0.40 instructions document the library root at
    # ``$XDG_DATA_HOME/lies/library/`` (with ``library/collections/<name>/``
    # described in the next paragraph). The pre-v0.40 string used
    # ``<slug>`` as a placeholder; the rewrite dropped it because the
    # library carries named collections, not anonymous slugs. Pin the
    # parent path so the operator-facing orientation still surfaces the
    # library location end-to-end.
    assert "$XDG_DATA_HOME/lies/library/" in instr
    assert "library/collections/<name>" in instr
