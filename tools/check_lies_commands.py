"""Pre-commit hook: every ``lies <cmd>`` reference in the MCP
orientation payload resolves against the live Typer registry.

Targets:

- ``src/lies/mcp/instructions.md``
- ``src/lies/mcp/prompts/*.md``

Regex:

    \blies [a-z][a-z0-9-]+(( [a-z][a-z0-9-]+){1,2})?\b

Word-boundary anchors (``\b...``) match ``lies <cmd>`` as a substring
anywhere it appears in the markdown — inline references like
``lies sync <collection> --source <source>``, ``lies init <name>``,
``lies lint [--fix]`` all count, not just lines whose whole content
matches the command. The two-segment allowance catches 2-segment subcommands like
``lies library new``, ``lies mcp up``, ``lies catalog render``.

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
    r"\blies [a-z][a-z0-9-]+(( [a-z][a-z0-9-]+){1,2})?\b"
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

    def _name(info) -> str:
        """Resolve a TyperInfo's command name; fall back to callback.__name__.

        Typer derives the command name from ``callback.__name__`` when the
        decorator omits an explicit ``name=``; in that case ``info.name`` is
        ``None``. Without this fallback, ``' '.join((prefix, info.name))``
        raises ``TypeError`` and the gate exits 2 instead of catching drift.
        """
        if info.name:
            return info.name
        if info.callback is not None and hasattr(info.callback, "__name__"):
            return info.callback.__name__
        return ""

    def _walk(node, prefix: tuple[str, ...]) -> None:
        # Sub-groups: recurse into each TyperInfo's typer_instance.
        for sub_info in getattr(node, "registered_groups", []):
            sub_name = _name(sub_info)
            if not sub_name:
                continue
            out.add("lies " + " ".join((*prefix, sub_name)))
            sub_typer = getattr(sub_info, "typer_instance", None)
            if sub_typer is not None:
                _walk(sub_typer, (*prefix, sub_name))
        # Direct commands: leaf-level TyperInfo; typer_instance is None here.
        for cmd_info in getattr(node, "registered_commands", []):
            cmd_name = _name(cmd_info)
            if not cmd_name:
                continue
            out.add("lies " + " ".join((*prefix, cmd_name)))

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
