"""Pre-commit hook: every ``lies <cmd>`` reference in the MCP
orientation payload resolves against the live Typer registry.

Targets:

- ``src/lies/mcp/instructions.md``
- ``src/lies/mcp/prompts/*.md``

Regex:

    ^lies [a-z][a-z0-9-]+(( [a-z][a-z0-9-]+){1,2})?$

The two-segment allowance catches ``lies query format``,
``lies catalog render``, etc.

Exit codes:

- 0: every reference resolves.
- 1: at least one reference is unresolved (printed to stderr).
- 2: Typer registry failed to load (e.g. import error).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = REPO_ROOT / "src" / "lies" / "mcp"
INSTRUCTIONS_PATH = MCP_ROOT / "instructions.md"
PROMPTS_DIR = MCP_ROOT / "prompts"

COMMAND_RE = re.compile(
    r"^lies [a-z][a-z0-9-]+(( [a-z][a-z0-9-]+){1,2})?$",
    re.MULTILINE,
)


def _targets() -> list[Path]:
    files: list[Path] = []
    if INSTRUCTIONS_PATH.exists():
        files.append(INSTRUCTIONS_PATH)
    if PROMPTS_DIR.exists():
        files.extend(sorted(PROMPTS_DIR.glob("*.md")))
    return files


def _references_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {m.group(0) for m in COMMAND_RE.finditer(text)}


def _all_typer_commands() -> set[str]:
    """Enumerate every ``lies <cmd>`` reachable via the Typer tree."""
    from lies.cli import app  # local import: Typer registry lives here

    out: set[str] = set()

    def _walk(node, prefix: tuple[str, ...]) -> None:
        for sub in getattr(node, "registered_groups", []):
            seg = "lies " + " ".join((*prefix, sub.name))
            out.add(seg)
            _walk(sub, (*prefix, sub.name))
        for cmd in getattr(node, "registered_commands", []):
            out.add("lies " + " ".join((*prefix, cmd.name)))

    _walk(app, ())
    return out


def main() -> int:
    refs: dict[str, list[Path]] = {}
    for path in _targets():
        for ref in _references_in(path):
            refs.setdefault(ref, []).append(path)

    if not refs:
        return 0

    try:
        registered = _all_typer_commands()
    except Exception as exc:  # noqa: BLE001
        print(f"check_lies_commands: failed to load Typer registry: {exc}", file=sys.stderr)
        return 2

    unresolved = sorted(set(refs) - registered)
    if not unresolved:
        return 0

    print("check_lies_commands: unresolved `lies <cmd>` references:", file=sys.stderr)
    for ref in unresolved:
        sources = ", ".join(p.relative_to(REPO_ROOT).as_posix() for p in refs[ref])
        print(f"  {ref}  ({sources})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
